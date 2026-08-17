from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import stim

import numpy as np

from ._geometry import (
    BoundaryShape,
    align_closed_cubics,
    boundary_to_cubics,
    find_boundary_shape,
    parse_stim_path_cubics,
)
from ._stim_svg import DetectorRegion, StimSvgFrame, parse_stim_svg_frame


@dataclass(frozen=True)
class VectorCompileReport:
    stim_version: str
    ticks: tuple[int, int]
    tick_count: int
    seconds_per_tick: float
    duration_seconds: float
    detector_trajectories: int
    actual_curve_segments_min: int
    actual_curve_segments_max: int
    actual_curve_segments_mean: float
    raw_stim_svg_seconds: float
    parse_seconds: float
    transition_seconds: float
    transition_encoding_seconds: float
    total_compile_seconds: float
    total_over_raw_stim_ratio: float
    raw_stim_svg_bytes: int
    interpolation_data_bytes: int
    output_bytes: int
    peak_live_exact_svg_frames: int
    pre_rendered_raster_frames: int
    fps_dependent_work: bool
    periodicity_assumptions: bool
    integer_keyframes: str
    sha256: str


@dataclass(frozen=True)
class VectorDetectorTrajectory:
    detector_id: int
    source_region: DetectorRegion | None
    target_region: DetectorRegion | None
    source_cubics: np.ndarray | None
    target_cubics: np.ndarray | None
    style_changed: bool

    @property
    def curve_segment_count(self) -> int:
        return 0 if self.source_cubics is None else len(self.source_cubics)


@dataclass(frozen=True)
class VectorTransition:
    source: StimSvgFrame
    target: StimSvgFrame
    trajectories: dict[int, VectorDetectorTrajectory]


def _literal_region_signature(region: DetectorRegion) -> bytes:
    return re.sub(
        rb' id="slice:[^"]+"',
        b' id="slice:style-compare"',
        ET.tostring(region.element),
        count=1,
    )


class CubicBoundaryCache:
    """Bounded content cache; an optimization, never a periodicity assumption."""

    def __init__(self, max_entries: int = 8192):
        self.max_entries = max_entries
        self._values: OrderedDict[tuple[BoundaryShape, int], np.ndarray] = OrderedDict()
        self._segment_counts: OrderedDict[BoundaryShape, int] = OrderedDict()

    def raw_segment_count(self, shape: BoundaryShape) -> int:
        cached = self._segment_counts.get(shape)
        if cached is not None:
            self._segment_counts.move_to_end(shape)
            return cached
        if shape.tag == "circle":
            value = 4
        else:
            assert shape.path_data is not None
            value = len(parse_stim_path_cubics(shape.path_data))
        self._segment_counts[shape] = value
        if len(self._segment_counts) > self.max_entries:
            self._segment_counts.popitem(last=False)
        return value

    def get(
        self,
        region: DetectorRegion,
        segment_count: int,
        shape: BoundaryShape,
    ) -> np.ndarray:
        key = (shape, segment_count)
        cached = self._values.get(key)
        if cached is not None:
            self._values.move_to_end(key)
            return cached
        value = boundary_to_cubics(region.element, segment_count)
        self._values[key] = value
        if len(self._values) > self.max_entries:
            self._values.popitem(last=False)
        return value


def build_vector_transition(
    source: StimSvgFrame,
    target: StimSvgFrame,
    *,
    cache: CubicBoundaryCache | None = None,
) -> VectorTransition:
    """Builds compact exact-cubic trajectories without dense point sampling."""
    trajectories: dict[int, VectorDetectorTrajectory] = {}
    cache = cache or CubicBoundaryCache()
    for detector_id in sorted(source.detectors.keys() | target.detectors.keys()):
        source_region = source.detectors.get(detector_id)
        target_region = target.detectors.get(detector_id)
        source_shape = (
            find_boundary_shape(source_region.element) if source_region is not None else None
        )
        target_shape = (
            find_boundary_shape(target_region.element) if target_region is not None else None
        )
        if (
            source_region is not None
            and target_region is not None
            and source_shape == target_shape
        ):
            trajectories[detector_id] = VectorDetectorTrajectory(
                detector_id,
                source_region,
                target_region,
                None,
                None,
                _literal_region_signature(source_region) != _literal_region_signature(target_region),
            )
            continue

        raw_segment_count = max(
            (
                cache.raw_segment_count(shape)
                for shape in (source_shape, target_shape)
                if shape is not None
            ),
            default=4,
        )
        segment_count = 1 << (max(16, raw_segment_count) - 1).bit_length()
        source_cubics = (
            cache.get(source_region, segment_count, source_shape)
            if source_region is not None and source_shape is not None
            else None
        )
        target_cubics = (
            cache.get(target_region, segment_count, target_shape)
            if target_region is not None and target_shape is not None
            else None
        )
        if source_cubics is None:
            assert target_cubics is not None
            source_cubics = np.broadcast_to(
                np.mean(target_cubics[:, 0, :], axis=0), target_cubics.shape
            ).copy()
        elif target_cubics is None:
            target_cubics = np.broadcast_to(
                np.mean(source_cubics[:, 0, :], axis=0), source_cubics.shape
            ).copy()
        else:
            target_cubics, _, _, _ = align_closed_cubics(source_cubics, target_cubics)
        trajectories[detector_id] = VectorDetectorTrajectory(
            detector_id,
            source_region,
            target_region,
            source_cubics,
            target_cubics,
            True,
        )
    return VectorTransition(source, target, trajectories)


def encode_vector_interval(transition: VectorTransition) -> dict[str, object]:
    """Packs one interval into bounded Float32 geometry plus tiny records."""
    records: list[list[int]] = []
    chunks: list[np.ndarray] = []
    offset = 0
    for detector_id in sorted(transition.trajectories):
        trajectory = transition.trajectories[detector_id]
        geometry_changed = trajectory.source_cubics is not None
        if not geometry_changed and not trajectory.style_changed:
            continue
        flags = 0
        if trajectory.source_region is not None:
            flags |= 1
        if trajectory.target_region is not None:
            flags |= 2
        if geometry_changed:
            flags |= 4
        if trajectory.style_changed:
            flags |= 8
        count = trajectory.curve_segment_count
        records.append([detector_id, flags, offset, count])
        if geometry_changed:
            assert trajectory.source_cubics is not None and trajectory.target_cubics is not None
            values = np.concatenate(
                [trajectory.source_cubics.reshape(-1), trajectory.target_cubics.reshape(-1)]
            ).astype("<f4", copy=False)
            chunks.append(values)
            offset += len(values)
    packed = np.concatenate(chunks).astype("<f4", copy=False).tobytes() if chunks else b""
    return {
        "records": records,
        "float32": base64.b64encode(packed).decode("ascii"),
    }


def _json_for_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


_HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Stim detector animation</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#111;color:#eee;font:14px system-ui,sans-serif;height:100dvh;display:grid;grid-template-rows:minmax(0,1fr) auto}.stage{min-height:0;background:white;display:grid;place-items:center;overflow:hidden}.stage svg{display:block;width:100%;height:100%}.controls{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;padding:12px max(12px,env(safe-area-inset-right)) max(12px,env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-left));background:#181818}button{font:inherit;font-weight:700;border:0;border-radius:8px;padding:9px 15px}input{width:100%}.tick{font-variant-numeric:tabular-nums;min-width:82px;text-align:right}
</style></head><body>
<div id="stage" class="stage"></div>
<div class="controls"><button id="play">Play</button><input id="seek" type="range" min="0" step="0.001"><span id="tick" class="tick"></span></div>
<script id="stimanim-data" type="application/json">"""


_HTML_TAIL = r"""</script>
<script>
'use strict';
const NS='http://www.w3.org/2000/svg';
const data=JSON.parse(document.getElementById('stimanim-data').textContent);
const stage=document.getElementById('stage'),seek=document.getElementById('seek'),label=document.getElementById('tick'),play=document.getElementById('play');
const intervalCount=data.ticks.length-1,spt=data.secondsPerTick;
seek.max=String(intervalCount);
let position=0,playing=false,lastTime=0,mountedKind='',mountedIndex=-1,active=[];

function detector(svg,id){
  const prefix=`slice:${id}:`;
  for(const child of svg.children)if(child.tagName.toLowerCase()==='g'&&child.id.startsWith(prefix))return child;
  return null;
}
function remapIds(group,prefix){
  const mapping=new Map();
  for(const item of [group,...group.querySelectorAll('[id]')])if(item.id)mapping.set(item.id,`${prefix}-${item.id}`);
  for(const item of [group,...group.querySelectorAll('*')]){
    if(item.id&&mapping.has(item.id))item.id=mapping.get(item.id);
    for(const attribute of [...item.attributes]){
      let value=attribute.value.replace(/url\((['"]?)#([^)'"]+)\1\)/g,(all,q,id)=>`url(${q}#${mapping.get(id)||id}${q})`);
      if((attribute.name==='href'||attribute.name==='xlink:href')&&value.startsWith('#'))value=`#${mapping.get(value.slice(1))||value.slice(1)}`;
      if(value!==attribute.value)item.setAttribute(attribute.name,value);
    }
  }
}
function boundaryPaths(group){
  let outline=null;
  for(const item of group.querySelectorAll('path,circle'))if(item.getAttribute('stroke')==='black'&&item.getAttribute('fill')==='none')outline=item;
  if(!outline)throw new Error(`Detector ${group.id} has no outline`);
  const isCircle=outline.tagName.toLowerCase()==='circle';
  const signature=isCircle?[outline.getAttribute('cx'),outline.getAttribute('cy'),outline.getAttribute('r')]:[outline.getAttribute('d')];
  const matches=[];
  for(const item of group.querySelectorAll('path,circle')){
    const same=isCircle
      ? item.tagName.toLowerCase()==='circle'&&item.getAttribute('cx')===signature[0]&&item.getAttribute('cy')===signature[1]&&item.getAttribute('r')===signature[2]
      : item.tagName.toLowerCase()==='path'&&item.getAttribute('d')===signature[0];
    if(!same)continue;
    const path=document.createElementNS(NS,'path');
    for(const attribute of [...item.attributes])if(!['d','cx','cy','r'].includes(attribute.name))path.setAttribute(attribute.name,attribute.value);
    item.replaceWith(path);matches.push(path);
  }
  return matches;
}
function decodeFloat32(encoded){
  if(!encoded)return new Float32Array();
  const binary=atob(encoded),bytes=new Uint8Array(binary.length);
  for(let k=0;k<binary.length;k++)bytes[k]=binary.charCodeAt(k);
  return new Float32Array(bytes.buffer);
}
function pathAt(values,offset,count,eased){
  const stride=count*8,target=offset+stride;
  const value=k=>values[offset+k]+(values[target+k]-values[offset+k])*eased;
  let path=`M${value(0).toFixed(4)},${value(1).toFixed(4)}`;
  for(let segment=0;segment<count;segment++){
    const k=segment*8;
    path+=`C${value(k+2).toFixed(4)},${value(k+3).toFixed(4)} ${value(k+4).toFixed(4)},${value(k+5).toFixed(4)} ${value(k+6).toFixed(4)},${value(k+7).toFixed(4)}`;
  }
  return path+'Z';
}
function mountExact(index){
  if(mountedKind==='exact'&&mountedIndex===index)return;
  stage.innerHTML=data.ticks[index].exact;mountedKind='exact';mountedIndex=index;active=[];
}
function mountInterval(index){
  if(mountedKind==='interval'&&mountedIndex===index)return;
  stage.innerHTML=data.ticks[index+1].exact;
  const svg=stage.querySelector('svg');
  const sourceSvg=new DOMParser().parseFromString(data.ticks[index].exact,'image/svg+xml').documentElement;
  let targetDefs=[...svg.children].find(child=>child.tagName.toLowerCase()==='defs');
  const sourceDefs=[...sourceSvg.children].find(child=>child.tagName.toLowerCase()==='defs');
  if(sourceDefs){
    if(!targetDefs){targetDefs=document.createElementNS(NS,'defs');svg.insertBefore(targetDefs,svg.firstChild)}
    const existing=new Set([...svg.querySelectorAll('[id]')].map(item=>item.id));
    for(const child of sourceDefs.children)if(!child.id||!existing.has(child.id)){targetDefs.appendChild(document.importNode(child,true));if(child.id)existing.add(child.id)}
  }
  const payload=data.ticks[index].interval,values=decodeFloat32(payload.float32);
  active=[];
  for(const record of payload.records){
    const [id,flags,offset,segments]=record,hasSource=!!(flags&1),hasTarget=!!(flags&2),geometry=!!(flags&4);
    let targetGroup=hasTarget?detector(svg,id):null;
    let sourceGroup=hasSource?detector(sourceSvg,id):null;
    if(sourceGroup){sourceGroup=document.importNode(sourceGroup,true);remapIds(sourceGroup,`i${index}-d${id}-source`)}
    const wrapper=document.createElementNS(NS,'g');wrapper.id=`detector:${id}`;
    const anchor=targetGroup||svg.querySelector('#qubit_dots')||svg.firstChild;
    if(targetGroup)targetGroup.replaceWith(wrapper);else svg.insertBefore(wrapper,anchor);
    let sourceLayer=null,targetLayer=null,sourcePaths=[],targetPaths=[];
    if(hasSource&&hasTarget){
      sourceLayer=document.createElementNS(NS,'g');targetLayer=document.createElementNS(NS,'g');
      sourceLayer.appendChild(sourceGroup);targetLayer.appendChild(targetGroup);wrapper.append(sourceLayer,targetLayer);
    }else if(hasSource){wrapper.appendChild(sourceGroup)}else{wrapper.appendChild(targetGroup)}
    if(geometry){
      if(sourceGroup)sourcePaths=boundaryPaths(sourceGroup);
      if(targetGroup)targetPaths=boundaryPaths(targetGroup);
    }
    active.push({flags,offset,segments,wrapper,sourceLayer,targetLayer,sourcePaths,targetPaths,values});
  }
  mountedKind='interval';mountedIndex=index;
}
function drawInterval(index,alpha){
  mountInterval(index);const eased=alpha*alpha*(3-2*alpha);
  for(const item of active){
    if(item.segments){
      const path=pathAt(item.values,item.offset,item.segments,eased);
      for(const node of item.sourcePaths)node.setAttribute('d',path);
      for(const node of item.targetPaths)node.setAttribute('d',path);
    }
    const hasSource=!!(item.flags&1),hasTarget=!!(item.flags&2);
    if(hasSource&&hasTarget){item.sourceLayer.setAttribute('opacity',String(1-eased));item.targetLayer.setAttribute('opacity',String(eased))}
    else item.wrapper.setAttribute('opacity',String(hasTarget?eased:1-eased));
  }
}
function show(pos,forcedExact=-1){
  pos=Math.max(0,Math.min(intervalCount,pos));position=pos;seek.value=String(pos);
  const nearest=Math.round(pos),isExact=forcedExact>=0||Math.abs(pos-nearest)<1e-8||pos===intervalCount;
  if(isExact)mountExact(forcedExact>=0?forcedExact:nearest);else{const interval=Math.floor(pos);drawInterval(interval,pos-interval)}
  label.textContent=`tick ${(data.startTick+pos).toFixed(3)}`;
}
function frame(now){
  if(!playing)return;if(!lastTime)lastTime=now;
  const old=position,next=Math.min(intervalCount,position+(now-lastTime)/1000/spt);lastTime=now;
  const crossed=Math.floor(next)>Math.floor(old)&&next<intervalCount;show(next,crossed?Math.floor(next):-1);
  if(next>=intervalCount){playing=false;play.textContent='Play';return}requestAnimationFrame(frame);
}
play.onclick=()=>{playing=!playing;play.textContent=playing?'Pause':'Play';lastTime=0;if(position>=intervalCount)show(0);if(playing)requestAnimationFrame(frame)};
seek.oninput=()=>{playing=false;play.textContent='Play';show(Number(seek.value))};show(0);
</script></body></html>
"""


class _Utf8Buffer:
    def __init__(self) -> None:
        self._buffer = io.BytesIO()

    def write(self, value: str) -> None:
        self._buffer.write(value.encode("utf-8"))

    def getvalue(self) -> bytes:
        return self._buffer.getvalue()


def compile_animation_in_memory(
    circuit: stim.Circuit,
    *,
    start_tick: int,
    end_tick: int,
    seconds_per_tick: float,
) -> tuple[bytes, VectorCompileReport]:
    """Builds exact keyframes plus native vector morphs entirely in memory."""
    if not 0 <= start_tick < end_tick <= circuit.num_ticks:
        raise ValueError(f"Expected 0 <= start_tick < end_tick <= {circuit.num_ticks}")
    if seconds_per_tick <= 0:
        raise ValueError("seconds_per_tick must be positive")
    started = time.perf_counter()
    stim_seconds = parse_seconds = transition_seconds = encoding_seconds = 0.0
    raw_svg_bytes = interpolation_bytes = trajectory_count = morphed_count = segment_sum = 0
    segment_min = 1 << 60
    segment_max = 0
    cubic_cache = CubicBoundaryCache()

    # Stim 1.15 can take pathological time rendering a slice at the terminal
    # boundary of some circuits (notably circuits ending in a measurement and
    # DETECTOR declarations). Appending an empty TICK makes that same boundary
    # non-terminal without changing the circuit state or any SVG at the ticks
    # being compiled. Do this for every full-range compile instead of
    # special-casing a circuit family.
    diagram_circuit = (
        circuit + stim.Circuit("TICK")
        if end_tick == circuit.num_ticks
        else circuit
    )

    def generate(tick: int) -> str:
        nonlocal stim_seconds, raw_svg_bytes
        before = time.perf_counter()
        svg = str(diagram_circuit.diagram("detslice-with-ops-svg", tick=tick))
        stim_seconds += time.perf_counter() - before
        raw_svg_bytes += len(svg.encode("utf-8"))
        return svg

    def parse(svg: str, tick: int) -> StimSvgFrame:
        nonlocal parse_seconds
        before = time.perf_counter()
        frame = parse_stim_svg_frame(svg, tick=tick)
        parse_seconds += time.perf_counter() - before
        return frame

    descriptor = {
        "format": "stimanim-vector-v2",
        "stimVersion": stim.__version__,
        "startTick": start_tick,
        "endTick": end_tick,
        "secondsPerTick": seconds_per_tick,
        "ticks": None,
    }
    prefix = _json_for_script(descriptor)
    prefix = prefix[:-5] + "["  # Replace null} after the final `ticks` key.

    stream = _Utf8Buffer()
    stream.write(_HTML_HEAD)
    stream.write(prefix)
    current_svg = generate(start_tick)
    current = parse(current_svg, start_tick)
    for item_index, tick in enumerate(range(start_tick, end_tick)):
        target_svg = generate(tick + 1)
        target = parse(target_svg, tick + 1)
        if current.view_box != target.view_box:
            raise ValueError("Changing Stim SVG viewBoxes are not supported")

        before = time.perf_counter()
        transition = build_vector_transition(current, target, cache=cubic_cache)
        transition_seconds += time.perf_counter() - before
        counts = [
            item.curve_segment_count
            for item in transition.trajectories.values()
            if item.curve_segment_count
        ]
        trajectory_count += len(transition.trajectories)
        morphed_count += len(counts)
        segment_sum += sum(counts)
        if counts:
            segment_min = min(segment_min, min(counts))
            segment_max = max(segment_max, max(counts))

        before = time.perf_counter()
        interval_payload = encode_vector_interval(transition)
        interval_json = _json_for_script(interval_payload)
        encoding_seconds += time.perf_counter() - before
        interpolation_bytes += len(interval_json.encode("utf-8"))
        if item_index:
            stream.write(",")
        stream.write('{"exact":')
        stream.write(_json_for_script(current_svg))
        stream.write(',"interval":')
        stream.write(interval_json)
        stream.write("}")
        current_svg, current = target_svg, target

    stream.write(",")
    stream.write(_json_for_script({"exact": current_svg}))
    stream.write("]}")
    stream.write(_HTML_TAIL)
    content = stream.getvalue()

    total = time.perf_counter() - started
    digest = hashlib.sha256(content).hexdigest()
    report = VectorCompileReport(
        stim_version=stim.__version__,
        ticks=(start_tick, end_tick),
        tick_count=end_tick - start_tick,
        seconds_per_tick=seconds_per_tick,
        duration_seconds=(end_tick - start_tick) * seconds_per_tick,
        detector_trajectories=trajectory_count,
        actual_curve_segments_min=segment_min if segment_max else 0,
        actual_curve_segments_max=segment_max,
        actual_curve_segments_mean=segment_sum / morphed_count if morphed_count else 0.0,
        raw_stim_svg_seconds=stim_seconds,
        parse_seconds=parse_seconds,
        transition_seconds=transition_seconds,
        transition_encoding_seconds=encoding_seconds,
        total_compile_seconds=total,
        total_over_raw_stim_ratio=total / stim_seconds if stim_seconds else math.inf,
        raw_stim_svg_bytes=raw_svg_bytes,
        interpolation_data_bytes=interpolation_bytes,
        output_bytes=len(content),
        peak_live_exact_svg_frames=2,
        pre_rendered_raster_frames=0,
        fps_dependent_work=False,
        periodicity_assumptions=False,
        integer_keyframes="verbatim exact Stim SVG",
        sha256=digest,
    )
    return content, report
