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
      grid-template-columns: auto auto 1fr auto;
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
    <button id="save-gif">Save GIF</button>
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
const gifButton = document.getElementById('save-gif');
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

class ByteWriter {
  constructor() {
    this.chunks = [];
    this.buffer = new Uint8Array(65536);
    this.length = 0;
  }

  byte(value) {
    if (this.length === this.buffer.length) this.flush();
    this.buffer[this.length++] = value;
  }

  word(value) {
    this.byte(value & 255);
    this.byte((value >>> 8) & 255);
  }

  bytes(values) {
    let offset = 0;
    while (offset < values.length) {
      if (this.length === this.buffer.length) this.flush();
      const count = Math.min(values.length - offset, this.buffer.length - this.length);
      this.buffer.set(values.subarray(offset, offset + count), this.length);
      this.length += count;
      offset += count;
    }
  }

  text(value) {
    for (let index = 0; index < value.length; index++) this.byte(value.charCodeAt(index));
  }

  flush() {
    if (!this.length) return;
    this.chunks.push(this.buffer.slice(0, this.length));
    this.length = 0;
  }

  blob() {
    this.flush();
    return new Blob(this.chunks, {type: 'image/gif'});
  }
}

function gifPalette() {
  const colors = [
    [0, 0, 0],
    [255, 255, 255],
    [255, 64, 64],
    [89, 255, 122],
    [77, 166, 255],
    [170, 170, 170],
    [17, 17, 17],
    [24, 24, 24],
  ];
  const levels = [0, 85, 170, 255];
  for (const red of levels) {
    for (const green of levels) {
      for (const blue of levels) colors.push([red, green, blue]);
    }
  }
  for (let index = 0; index < 55; index++) {
    const value = Math.round(index * 255 / 54);
    colors.push([value, value, value]);
  }
  colors.push([0, 0, 0]);
  return new Uint8Array(colors.flat());
}

const GIF_PALETTE = gifPalette();
const GIF_TRANSPARENT = 127;
let gifLookup = null;

function getGifLookup() {
  if (gifLookup) return gifLookup;
  gifLookup = new Uint8Array(32768);
  for (let red = 0; red < 32; red++) {
    for (let green = 0; green < 32; green++) {
      for (let blue = 0; blue < 32; blue++) {
        const actualRed = Math.min(255, red * 8 + 4);
        const actualGreen = Math.min(255, green * 8 + 4);
        const actualBlue = Math.min(255, blue * 8 + 4);
        let best = 0;
        let bestDistance = Infinity;
        for (let index = 0; index < 127; index++) {
          const offset = index * 3;
          const deltaRed = actualRed - GIF_PALETTE[offset];
          const deltaGreen = actualGreen - GIF_PALETTE[offset + 1];
          const deltaBlue = actualBlue - GIF_PALETTE[offset + 2];
          const distance = deltaRed * deltaRed + deltaGreen * deltaGreen + deltaBlue * deltaBlue;
          if (distance < bestDistance) {
            bestDistance = distance;
            best = index;
          }
        }
        gifLookup[(red << 10) | (green << 5) | blue] = best;
      }
    }
  }
  return gifLookup;
}

class GifEncoder {
  constructor(width, height) {
    this.width = width;
    this.height = height;
    this.writer = new ByteWriter();
    this.writer.text('GIF89a');
    this.writer.word(width);
    this.writer.word(height);
    this.writer.byte(0xf6);
    this.writer.byte(1);
    this.writer.byte(0);
    this.writer.bytes(GIF_PALETTE);
    this.writer.byte(0x21);
    this.writer.byte(0xff);
    this.writer.byte(11);
    this.writer.text('NETSCAPE2.0');
    this.writer.byte(3);
    this.writer.byte(1);
    this.writer.word(0);
    this.writer.byte(0);
  }

  frame(pixels, left, top, width, height, delay) {
    this.writer.byte(0x21);
    this.writer.byte(0xf9);
    this.writer.byte(4);
    this.writer.byte(5);
    this.writer.word(Math.max(1, Math.min(65535, delay)));
    this.writer.byte(GIF_TRANSPARENT);
    this.writer.byte(0);
    this.writer.byte(0x2c);
    this.writer.word(left);
    this.writer.word(top);
    this.writer.word(width);
    this.writer.word(height);
    this.writer.byte(0);
    this.writePixels(pixels, left, top, width, height);
  }

  writePixels(pixels, left, top, width, height) {
    const writer = this.writer;
    const dictionary = new Map();
    const clearCode = 128;
    const endCode = 129;
    let nextCode = 130;
    let codeSize = 8;
    let bitBuffer = 0;
    let bitCount = 0;
    const block = new Uint8Array(255);
    let blockLength = 0;

    writer.byte(7);
    const writeByte = value => {
      block[blockLength++] = value;
      if (blockLength === 255) {
        writer.byte(255);
        writer.bytes(block);
        blockLength = 0;
      }
    };
    const writeCode = code => {
      bitBuffer |= code << bitCount;
      bitCount += codeSize;
      while (bitCount >= 8) {
        writeByte(bitBuffer & 255);
        bitBuffer >>>= 8;
        bitCount -= 8;
      }
    };
    const pixelAt = index => {
      const x = index % width;
      const y = (index - x) / width;
      const source = (top + y) * this.width + left + x;
      return pixels[source];
    };

    writeCode(clearCode);
    const pixelCount = width * height;
    let prefix = pixelAt(0);
    for (let index = 1; index < pixelCount; index++) {
      const value = pixelAt(index);
      const key = (prefix << 8) | value;
      const found = dictionary.get(key);
      if (found !== undefined) {
        prefix = found;
        continue;
      }
      writeCode(prefix);
      if (nextCode < 4096) {
        dictionary.set(key, nextCode++);
        if (nextCode === (1 << codeSize) + 1 && codeSize < 12) codeSize++;
      } else {
        writeCode(clearCode);
        dictionary.clear();
        nextCode = 130;
        codeSize = 8;
      }
      prefix = value;
    }
    writeCode(prefix);
    writeCode(endCode);
    if (bitCount) writeByte(bitBuffer & 255);
    if (blockLength) {
      writer.byte(blockLength);
      writer.bytes(block.subarray(0, blockLength));
    }
    writer.byte(0);
  }

  finish() {
    this.writer.byte(0x3b);
    return this.writer.blob();
  }
}

function indexedFrame(imageData, previous) {
  const rgba = imageData.data;
  const lookup = getGifLookup();
  const pixels = new Uint8Array(imageData.width * imageData.height);
  const patch = new Uint8Array(pixels.length);
  if (previous) patch.fill(GIF_TRANSPARENT);
  let left = imageData.width;
  let top = imageData.height;
  let right = -1;
  let bottom = -1;
  for (let index = 0; index < pixels.length; index++) {
    const offset = index * 4;
    const key = ((rgba[offset] >> 3) << 10) |
        ((rgba[offset + 1] >> 3) << 5) |
        (rgba[offset + 2] >> 3);
    const value = lookup[key];
    pixels[index] = value;
    if (previous && value === previous[index]) continue;
    patch[index] = value;
    const x = index % imageData.width;
    const y = (index - x) / imageData.width;
    left = Math.min(left, x);
    top = Math.min(top, y);
    right = Math.max(right, x);
    bottom = Math.max(bottom, y);
  }
  if (right < left) return {pixels, patch, left: 0, top: 0, width: 1, height: 1};
  return {pixels, patch, left, top, width: right - left + 1, height: bottom - top + 1};
}

function gifPlan(scale) {
  if (!intervalCount) return [{position: 0, delay: 10}];
  const frames = [];
  const nominal = Math.max(2, Math.round(data.secondsPerTick * 20));
  for (let interval = 0; interval < intervalCount; interval++) {
    const payload = data.ticks[interval].interval;
    const values = decodePoints(payload.points);
    let maximumMotion = 0;
    let needsFade = false;
    for (const [detectorId, flags, offset, count] of payload.records) {
      if ((flags & STYLE_CHANGED) || !(flags & HAS_SOURCE) || !(flags & HAS_TARGET)) {
        needsFade = true;
      }
      const targetOffset = offset + count * 2;
      for (let point = 0; point < count; point++) {
        const source = offset + point * 2;
        const target = targetOffset + point * 2;
        const dx = values[target] - values[source];
        const dy = values[target + 1] - values[source + 1];
        maximumMotion = Math.max(maximumMotion, Math.hypot(dx, dy) * scale);
      }
    }
    const totalDelay = Math.max(2, Math.round(data.secondsPerTick * 100));
    let steps = needsFade ? nominal : Math.max(2, Math.ceil(maximumMotion / 2));
    steps = Math.min(nominal, totalDelay, steps);
    const baseDelay = Math.floor(totalDelay / steps);
    let remainder = totalDelay - baseDelay * steps;
    for (let step = 0; step < steps; step++) {
      frames.push({
        position: interval + step / steps,
        delay: baseDelay + (remainder-- > 0 ? 1 : 0),
      });
    }
  }
  frames.push({position: intervalCount, delay: 4});
  return frames;
}

async function drawSvg(context, svg, width, height) {
  const copy = svg.cloneNode(true);
  copy.setAttribute('width', String(width));
  copy.setAttribute('height', String(height));
  const source = new XMLSerializer().serializeToString(copy);
  const url = URL.createObjectURL(new Blob([source], {type: 'image/svg+xml'}));
  const image = new Image();
  image.src = url;
  try {
    await image.decode();
  } finally {
    URL.revokeObjectURL(url);
  }
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.fillStyle = 'white';
  context.fillRect(0, 0, width, height);
  context.drawImage(image, 0, 0, width, height);
}

async function saveGif() {
  const exportStarted = performance.now();
  const originalPosition = position;
  const resume = playing;
  playing = false;
  playButton.textContent = 'Play';
  gifButton.disabled = true;
  try {
    show(0);
    const svg = stage.querySelector('svg');
    const box = svg.viewBox.baseVal;
    const width = Math.max(480, Math.min(720, Math.round(box.width)));
    const height = Math.max(1, Math.round(width * box.height / box.width));
    const plan = gifPlan(width / box.width);
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', {alpha: false, willReadFrequently: true});
    const encoder = new GifEncoder(width, height);
    let previous = null;
    for (let index = 0; index < plan.length; index++) {
      show(plan[index].position);
      await drawSvg(context, stage.querySelector('svg'), width, height);
      const frame = indexedFrame(context.getImageData(0, 0, width, height), previous);
      encoder.frame(
          frame.patch, frame.left, frame.top, frame.width, frame.height, plan[index].delay);
      previous = frame.pixels;
      if (index % 8 === 7 || index + 1 === plan.length) {
        gifButton.textContent = `Saving ${Math.round((index + 1) * 100 / plan.length)}%`;
        await new Promise(resolve => setTimeout(resolve, 0));
      }
    }
    const blob = encoder.finish();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `dynamic_detection_regions_${data.startTick}-${data.startTick + intervalCount}.gif`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    gifButton.dataset.lastBytes = String(blob.size);
    gifButton.dataset.lastMilliseconds = String(performance.now() - exportStarted);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    console.error(error);
    alert(`GIF export failed: ${error.message || error}`);
  } finally {
    show(originalPosition);
    gifButton.textContent = 'Save GIF';
    gifButton.disabled = false;
    playing = resume && intervalCount > 0;
    playButton.textContent = playing ? 'Pause' : 'Play';
    previousFrameTime = 0;
    if (playing) requestAnimationFrame(animate);
  }
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

gifButton.onclick = saveGif;

playButton.disabled = intervalCount === 0;
playButton.textContent = playing ? 'Pause' : 'Play';
show(0);
if (playing) requestAnimationFrame(animate);
</script>
</body>
</html>
"""
