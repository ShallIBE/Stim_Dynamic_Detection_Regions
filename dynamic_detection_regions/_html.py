HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>Stim dynamic detection regions</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      height: 100dvh;
      display: grid;
      grid-template-rows: minmax(0, 1fr) auto;
      color: #eee;
      background: #111;
      font: 14px system-ui, sans-serif;
    }
    #stage {
      min-height: 0;
      display: grid;
      place-items: center;
      overflow: hidden;
      background: white;
    }
    #stage svg { display: block; width: 100%; height: 100%; }
    #controls {
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 12px max(12px, env(safe-area-inset-right))
               max(12px, env(safe-area-inset-bottom))
               max(12px, env(safe-area-inset-left));
      background: #181818;
    }
    button {
      padding: 9px 15px;
      border: 0;
      border-radius: 8px;
      font: inherit;
      font-weight: 700;
    }
    button:disabled { opacity: 0.5; }
    input { width: 100%; }
    #tick { min-width: 145px; text-align: right; font-variant-numeric: tabular-nums; }
  </style>
</head>
<body>
  <div id="stage"></div>
  <div id="controls">
    <button id="play">Play</button>
    <input id="seek" type="range" min="0" step="0.001">
    <span id="tick"></span>
  </div>
  <script id="dynamic-detection-regions-data" type="application/json">"""


HTML_TAIL = r"""</script>
<script>
'use strict';

const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';
const HAS_SOURCE = 1;
const HAS_TARGET = 2;
const GEOMETRY_CHANGED = 4;
const STYLE_CHANGED = 8;

const data = JSON.parse(document.getElementById('dynamic-detection-regions-data').textContent);
const stage = document.getElementById('stage');
const seek = document.getElementById('seek');
const tickLabel = document.getElementById('tick');
const playButton = document.getElementById('play');
const intervalCount = data.ticks.length - 1;

seek.max = String(intervalCount);

let position = 0;
let playing = Boolean(data.autoplay) && intervalCount > 0;
let previousFrameTime = 0;
let mountedKind = '';
let mountedIndex = -1;
let activeRegions = [];

function findDetector(svg, detectorId) {
  const prefix = `slice:${detectorId}:`;
  for (const group of svg.querySelectorAll('g[id]')) {
    if (group.id.startsWith(prefix)) return group;
  }
  return null;
}

function parseDetectorId(group) {
  const match = /^slice:(\d+):/.exec(group.id);
  return match ? Number(match[1]) : null;
}

function remapIds(group, prefix) {
  const elements = [group, ...group.querySelectorAll('[id]')];
  const mapping = new Map();
  for (const element of elements) {
    if (element.id) mapping.set(element.id, `${prefix}-${element.id}`);
  }
  for (const element of [group, ...group.querySelectorAll('*')]) {
    if (element.id && mapping.has(element.id)) element.id = mapping.get(element.id);
    for (const attribute of [...element.attributes]) {
      let value = attribute.value.replace(
          /url\((['"]?)#([^)'"]+)\1\)/g,
          (whole, quote, id) => `url(${quote}#${mapping.get(id) || id}${quote})`);
      if ((attribute.name === 'href' || attribute.name === 'xlink:href') && value.startsWith('#')) {
        value = `#${mapping.get(value.slice(1)) || value.slice(1)}`;
      }
      if (value !== attribute.value) element.setAttribute(attribute.name, value);
    }
  }
}

function replaceBoundariesWithPaths(group) {
  let outline = null;
  for (const element of group.querySelectorAll('path,circle')) {
    if (element.getAttribute('stroke') === 'black' && element.getAttribute('fill') === 'none') {
      outline = element;
    }
  }
  if (!outline) throw new Error(`Detector ${group.id} has no recognizable outline.`);

  const circle = outline.tagName.toLowerCase() === 'circle';
  const geometry = circle
      ? [outline.getAttribute('cx'), outline.getAttribute('cy'), outline.getAttribute('r')]
      : [outline.getAttribute('d')];
  const paths = [];
  for (const element of group.querySelectorAll('path,circle')) {
    const matches = circle
        ? element.tagName.toLowerCase() === 'circle' &&
          element.getAttribute('cx') === geometry[0] &&
          element.getAttribute('cy') === geometry[1] &&
          element.getAttribute('r') === geometry[2]
        : element.tagName.toLowerCase() === 'path' && element.getAttribute('d') === geometry[0];
    if (!matches) continue;
    const path = document.createElementNS(SVG_NAMESPACE, 'path');
    for (const attribute of [...element.attributes]) {
      if (!['d', 'cx', 'cy', 'r'].includes(attribute.name)) {
        path.setAttribute(attribute.name, attribute.value);
      }
    }
    element.replaceWith(path);
    paths.push(path);
  }
  return paths;
}

function decodePoints(encoded) {
  if (!encoded) return new Float32Array();
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) bytes[index] = binary.charCodeAt(index);
  return new Float32Array(bytes.buffer);
}

function interpolatedPath(values, offset, count, alpha) {
  const targetOffset = offset + count * 2;
  let path = '';
  for (let point = 0; point < count; point++) {
    const index = offset + point * 2;
    const x = values[index] + (values[targetOffset + point * 2] - values[index]) * alpha;
    const y = values[index + 1] +
        (values[targetOffset + point * 2 + 1] - values[index + 1]) * alpha;
    path += `${point ? 'L' : 'M'}${x.toFixed(4)},${y.toFixed(4)}`;
  }
  return path + 'Z';
}

function mountExact(index) {
  if (mountedKind === 'exact' && mountedIndex === index) return;
  stage.innerHTML = data.ticks[index].exact;
  mountedKind = 'exact';
  mountedIndex = index;
  activeRegions = [];
}

function copyMissingDefinitions(sourceSvg, targetSvg) {
  const sourceDefinitions = [...sourceSvg.children].find(
      child => child.tagName.toLowerCase() === 'defs');
  if (!sourceDefinitions) return;
  let targetDefinitions = [...targetSvg.children].find(
      child => child.tagName.toLowerCase() === 'defs');
  if (!targetDefinitions) {
    targetDefinitions = document.createElementNS(SVG_NAMESPACE, 'defs');
    targetSvg.insertBefore(targetDefinitions, targetSvg.firstChild);
  }
  const existingIds = new Set([...targetSvg.querySelectorAll('[id]')].map(element => element.id));
  for (const child of sourceDefinitions.children) {
    if (!child.id || !existingIds.has(child.id)) {
      targetDefinitions.appendChild(document.importNode(child, true));
      if (child.id) existingIds.add(child.id);
    }
  }
}

function findOutline(group) {
  let outline = null;
  for (const element of group.querySelectorAll('path,circle')) {
    if (element.getAttribute('stroke') === 'black' && element.getAttribute('fill') === 'none') {
      outline = element;
    }
  }
  if (!outline) throw new Error(`Detector ${group.id} has no recognizable outline.`);
  return outline;
}

function mainFillOpacity(group) {
  for (const element of group.querySelectorAll('path,circle')) {
    if (element.getAttribute('stroke') === 'none' && element.getAttribute('fill') !== 'none') {
      const value = Number(element.getAttribute('fill-opacity') || '1');
      if (Number.isFinite(value)) return value;
    }
  }
  return 1;
}

function placeDyingRegions(sourceSvg, targetSvg, dyingRegions, renderedRegions) {
  if (!dyingRegions.size) return;
  const sourceOrder = [];
  for (const group of sourceSvg.querySelectorAll('g[id]')) {
    const id = parseDetectorId(group);
    if (id !== null) sourceOrder.push(id);
  }
  const qubitDots = targetSvg.querySelector('#qubit_dots');
  for (let index = 0; index < sourceOrder.length; index++) {
    const id = sourceOrder[index];
    if (!dyingRegions.has(id)) continue;
    let anchor = null;
    for (let next = index + 1; next < sourceOrder.length; next++) {
      const nextId = sourceOrder[next];
      if (dyingRegions.has(nextId)) continue;
      anchor = renderedRegions.get(nextId) || findDetector(targetSvg, nextId);
      if (anchor) break;
    }
    const group = renderedRegions.get(id);
    if (group) targetSvg.insertBefore(group, anchor || qubitDots);
  }
}

function mountInterval(index) {
  if (mountedKind === 'interval' && mountedIndex === index) return;

  stage.innerHTML = data.ticks[index + 1].exact;
  const targetSvg = stage.querySelector('svg');
  const sourceSvg = new DOMParser().parseFromString(
      data.ticks[index].exact, 'image/svg+xml').documentElement;
  copyMissingDefinitions(sourceSvg, targetSvg);

  const payload = data.ticks[index].interval;
  const values = decodePoints(payload.points);
  activeRegions = [];
  const dyingRegions = new Set();
  const renderedRegions = new Map();

  for (const [detectorId, flags, offset, count] of payload.records) {
    const hasSource = Boolean(flags & HAS_SOURCE);
    const hasTarget = Boolean(flags & HAS_TARGET);
    const geometryChanged = Boolean(flags & GEOMETRY_CHANGED);
    const styleChanged = Boolean(flags & STYLE_CHANGED);
    let sourceGroup = hasSource ? findDetector(sourceSvg, detectorId) : null;
    let targetGroup = hasTarget ? findDetector(targetSvg, detectorId) : null;

    if (sourceGroup) {
      sourceGroup = document.importNode(sourceGroup, true);
      remapIds(sourceGroup, `interval-${index}-detector-${detectorId}-source`);
    }

    const wrapper = document.createElementNS(SVG_NAMESPACE, 'g');
    wrapper.id = `detector:${detectorId}`;
    if (targetGroup) {
      targetGroup.replaceWith(wrapper);
    } else {
      const anchor = targetSvg.querySelector('#qubit_dots') || targetSvg.firstChild;
      targetSvg.insertBefore(wrapper, anchor);
      dyingRegions.add(detectorId);
    }
    renderedRegions.set(detectorId, wrapper);

    let sourceLayer = null;
    let targetLayer = null;
    let sourcePaths = [];
    let targetPaths = [];
    let outline = null;
    let targetFillOpacity = 1;
    if (hasSource && hasTarget && styleChanged) {
      targetFillOpacity = mainFillOpacity(targetGroup);
      sourceLayer = document.createElementNS(SVG_NAMESPACE, 'g');
      targetLayer = document.createElementNS(SVG_NAMESPACE, 'g');
      sourceLayer.appendChild(sourceGroup);
      targetLayer.appendChild(targetGroup);
      wrapper.append(sourceLayer, targetLayer);
    } else if (hasTarget) {
      wrapper.appendChild(targetGroup);
    } else {
      wrapper.appendChild(sourceGroup);
    }

    if (geometryChanged) {
      if (sourceGroup && (styleChanged || !hasTarget)) {
        sourcePaths = replaceBoundariesWithPaths(sourceGroup);
      }
      if (targetGroup) targetPaths = replaceBoundariesWithPaths(targetGroup);
    }
    if (hasSource && hasTarget && styleChanged) {
      const sourceOutline = findOutline(sourceGroup);
      const targetOutline = findOutline(targetGroup);
      sourceOutline.setAttribute('opacity', '0');
      targetOutline.setAttribute('opacity', '0');
      outline = targetOutline.cloneNode(true);
      outline.removeAttribute('opacity');
      wrapper.appendChild(outline);
    }
    activeRegions.push({
      flags,
      offset,
      count,
      wrapper,
      sourceLayer,
      targetLayer,
      sourcePaths,
      targetPaths,
      outline,
      targetFillOpacity,
      values,
    });
  }
  placeDyingRegions(sourceSvg, targetSvg, dyingRegions, renderedRegions);
  mountedKind = 'interval';
  mountedIndex = index;
}

function drawInterval(index, alpha) {
  mountInterval(index);
  const eased = alpha * alpha * (3 - 2 * alpha);
  for (const region of activeRegions) {
    if (region.count) {
      const path = interpolatedPath(region.values, region.offset, region.count, eased);
      for (const element of region.sourcePaths) element.setAttribute('d', path);
      for (const element of region.targetPaths) element.setAttribute('d', path);
      if (region.outline) region.outline.setAttribute('d', path);
    }
    const hasSource = Boolean(region.flags & HAS_SOURCE);
    const hasTarget = Boolean(region.flags & HAS_TARGET);
    const styleChanged = Boolean(region.flags & STYLE_CHANGED);
    if (hasSource && hasTarget && styleChanged) {
      const denominator = 1 - eased * region.targetFillOpacity;
      const sourceOpacity = eased >= 1 ? 0 : (1 - eased) / Math.max(denominator, 1e-9);
      region.sourceLayer.setAttribute('opacity', String(sourceOpacity));
      region.targetLayer.setAttribute('opacity', String(eased));
    } else if (!hasSource || !hasTarget) {
      region.wrapper.setAttribute('opacity', String(hasTarget ? eased : 1 - eased));
    }
  }
}

function show(nextPosition) {
  position = Math.max(0, Math.min(intervalCount, nextPosition));
  seek.value = String(position);
  const nearest = Math.round(position);
  const exact = Math.abs(position - nearest) < 1e-8 || position === intervalCount;
  if (exact) {
    mountExact(nearest);
  } else {
    const interval = Math.floor(position);
    drawInterval(interval, position - interval);
  }
  tickLabel.textContent = `diagram tick ${(data.startTick + position).toFixed(3)}`;
}

function animate(now) {
  if (!playing) return;
  if (!previousFrameTime) previousFrameTime = now;
  const nextPosition = Math.min(
      intervalCount,
      position + (now - previousFrameTime) / 1000 / data.secondsPerTick);
  previousFrameTime = now;
  show(nextPosition);
  if (nextPosition >= intervalCount) {
    if (data.loop) {
      requestAnimationFrame(restart);
    } else {
      playing = false;
      playButton.textContent = 'Play';
    }
  } else {
    requestAnimationFrame(animate);
  }
}

function restart(now) {
  if (!playing) return;
  show(0);
  previousFrameTime = now;
  requestAnimationFrame(animate);
}

playButton.onclick = () => {
  playing = !playing;
  playButton.textContent = playing ? 'Pause' : 'Play';
  previousFrameTime = 0;
  if (position >= intervalCount) show(0);
  if (playing) requestAnimationFrame(animate);
};

seek.oninput = () => {
  playing = false;
  playButton.textContent = 'Play';
  show(Number(seek.value));
};

playButton.disabled = intervalCount === 0;
playButton.textContent = playing ? 'Pause' : 'Play';
show(0);
if (playing) requestAnimationFrame(animate);
</script>
</body>
</html>
"""
