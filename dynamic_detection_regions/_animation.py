from __future__ import annotations

import base64
import io
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import numpy as np
import stim

from ._html import HTML_HEAD, HTML_TAIL


_NUMBER_TOKEN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_PATH_TOKEN_RE = re.compile(rf"[A-Za-z]|{_NUMBER_TOKEN}")
_URL_REFERENCE_RE = re.compile(r"url\((['\"]?)#([^)'\"]+)\1\)")

_HAS_SOURCE = 1
_HAS_TARGET = 2
_GEOMETRY_CHANGED = 4
_STYLE_CHANGED = 8


@dataclass(frozen=True)
class _Boundary:
    kind: str
    path: str | None = None
    center_x: float | None = None
    center_y: float | None = None
    radius: float | None = None


@dataclass(frozen=True)
class _Region:
    boundary: _Boundary
    style: bytes


@dataclass(frozen=True)
class _Frame:
    view_box: tuple[float, float, float, float]
    regions: dict[int, _Region]
    has_visible_region: bool = False


@dataclass(frozen=True)
class _BuildResult:
    content: bytes
    seconds: float


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_slice_id(value: str) -> int | None:
    """Returns the detector index from a Stim detector-slice group ID."""
    if not value.startswith("slice:"):
        return None
    fields = value.split(":")
    if len(fields) not in (3, 4) or not fields[1].isdigit() or not fields[-1].isdigit():
        raise RuntimeError(
            f"Unrecognized Stim detector-slice id {value!r} under Stim {stim.__version__}."
        )
    return int(fields[1])


def _find_boundary(group: ET.Element) -> _Boundary:
    outline: ET.Element | None = None
    for element in group.iter():
        if (
            _local_name(element.tag) in ("circle", "path")
            and element.attrib.get("stroke") == "black"
            and element.attrib.get("fill") == "none"
        ):
            outline = element
    if outline is None:
        raise RuntimeError(
            f"Stim detector group {group.attrib.get('id')!r} has no recognizable outline."
        )
    if _local_name(outline.tag) == "path":
        try:
            return _Boundary(kind="path", path=outline.attrib["d"])
        except KeyError as ex:
            raise RuntimeError(
                "Stim emitted a detector outline path without a d attribute."
            ) from ex
    try:
        return _Boundary(
            kind="circle",
            center_x=float(outline.attrib["cx"]),
            center_y=float(outline.attrib["cy"]),
            radius=float(outline.attrib["r"]),
        )
    except (KeyError, ValueError) as ex:
        raise RuntimeError("Stim emitted a malformed detector outline circle.") from ex


def _matches_boundary(element: ET.Element, boundary: _Boundary) -> bool:
    if boundary.kind == "path":
        return _local_name(element.tag) == "path" and element.attrib.get("d") == boundary.path
    return (
        _local_name(element.tag) == "circle"
        and element.attrib.get("cx") is not None
        and element.attrib.get("cy") is not None
        and element.attrib.get("r") is not None
        and float(element.attrib["cx"]) == boundary.center_x
        and float(element.attrib["cy"]) == boundary.center_y
        and float(element.attrib["r"]) == boundary.radius
    )


def _style_signature(group: ET.Element, boundary: _Boundary) -> bytes:
    """Canonicalizes incidental SVG IDs and removes only boundary geometry."""
    elements = list(group.iter())
    id_map: dict[str, str] = {}
    for index, element in enumerate(elements):
        old_id = element.attrib.get("id")
        if old_id is not None:
            new_id = "slice" if index == 0 else f"id{len(id_map)}"
            id_map[old_id] = new_id

    def signature(element: ET.Element) -> tuple[object, ...]:
        matches_boundary = _matches_boundary(element, boundary)
        attributes: list[tuple[str, str]] = []
        for key, value in element.attrib.items():
            if matches_boundary and key in ("cx", "cy", "r", "d"):
                continue
            if key == "id":
                value = id_map[value]
            else:
                value = _URL_REFERENCE_RE.sub(
                    lambda match: f"url(#{id_map.get(match.group(2), match.group(2))})",
                    value,
                )
            attributes.append((key, value))
        if matches_boundary:
            attributes.append(("d", "BOUNDARY"))
        return (
            "path" if matches_boundary else _local_name(element.tag),
            tuple(sorted(attributes)),
            tuple(signature(child) for child in element),
        )

    return repr(signature(group)).encode()


def _parse_frame(svg: str) -> _Frame:
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as ex:
        raise RuntimeError("Stim returned malformed SVG.") from ex
    if _local_name(root.tag) != "svg":
        raise RuntimeError("Stim diagram did not return an SVG root.")
    try:
        values = tuple(float(v) for v in root.attrib["viewBox"].split())
    except (KeyError, ValueError) as ex:
        raise RuntimeError("Stim SVG has no valid viewBox.") from ex
    if len(values) != 4:
        raise RuntimeError(f"Stim SVG viewBox has {len(values)} values instead of 4.")

    regions: dict[int, _Region] = {}
    has_observable_term = False
    for element in root.iter():
        element_id = element.attrib.get("id", "")
        if element_id.startswith("obs-term:"):
            has_observable_term = True
        if _local_name(element.tag) != "g":
            continue
        detector_id = _parse_slice_id(element_id)
        if detector_id is None:
            continue
        if detector_id in regions:
            raise RuntimeError(f"Stim SVG contains detector D{detector_id} more than once.")
        boundary = _find_boundary(element)
        regions[detector_id] = _Region(
            boundary=boundary,
            style=_style_signature(element, boundary),
        )
    view_box = (values[0], values[1], values[2], values[3])
    return _Frame(
        view_box=view_box,
        regions=regions,
        has_visible_region=bool(regions) or has_observable_term,
    )


def _tokenize_path(path: str) -> list[str]:
    tokens: list[str] = []
    end = 0
    for match in _PATH_TOKEN_RE.finditer(path):
        if path[end : match.start()].strip(" ,\t\r\n"):
            raise RuntimeError(f"Unsupported Stim SVG path syntax: {path!r}")
        tokens.append(match.group())
        end = match.end()
    if path[end:].strip(" ,\t\r\n"):
        raise RuntimeError(f"Unsupported Stim SVG path syntax: {path!r}")
    return tokens


def _path_cubics(path: str) -> np.ndarray:
    """Converts Stim's absolute M/L/C/Z path subset into cubic segments."""
    tokens = _tokenize_path(path)
    index = 0
    command: str | None = None
    current: np.ndarray | None = None
    start: np.ndarray | None = None
    segments: list[np.ndarray] = []

    def read(count: int) -> np.ndarray:
        nonlocal index
        chunk = tokens[index : index + count]
        if len(chunk) != count or any(len(token) == 1 and token.isalpha() for token in chunk):
            raise RuntimeError(f"Malformed Stim SVG path: {path!r}")
        index += count
        return np.asarray([float(token) for token in chunk], dtype=np.float64)

    def append_line(end: np.ndarray) -> None:
        nonlocal current
        if current is None:
            raise RuntimeError(f"Line before move in Stim SVG path: {path!r}")
        delta = end - current
        segments.append(
            np.stack((current, current + delta / 3, current + delta * (2 / 3), end))
        )
        current = end

    while index < len(tokens):
        token = tokens[index]
        if len(token) == 1 and token.isalpha():
            command = token
            index += 1
            if command not in ("M", "L", "C", "Z"):
                raise RuntimeError(f"Unsupported Stim SVG path command {command!r}.")
            if command == "Z":
                if current is not None and start is not None and not np.allclose(current, start):
                    append_line(start.copy())
                command = None
                continue
        if command == "M":
            if start is not None:
                raise RuntimeError(
                    "Multiple subpaths are not supported in a Stim detector outline."
                )
            current = read(2)
            start = current.copy()
            command = "L"
        elif command == "L":
            append_line(read(2))
        elif command == "C":
            if current is None:
                raise RuntimeError(f"Cubic before move in Stim SVG path: {path!r}")
            controls = read(6).reshape(3, 2)
            segments.append(np.vstack((current, controls)))
            current = controls[-1]
        else:
            raise RuntimeError(f"Malformed Stim SVG path: {path!r}")
    if not segments:
        raise RuntimeError(f"Stim SVG path has no drawable segments: {path!r}")
    if current is None or start is None or not np.allclose(current, start):
        raise RuntimeError("Stim detector outline path is not closed.")
    return np.asarray(segments)


def _sample_count(boundary: _Boundary) -> int:
    raw_count = 4 if boundary.kind == "circle" else len(_path_cubics(boundary.path or ""))
    requested = max(64, raw_count * 4)
    return 1 << (requested - 1).bit_length()


def _flatten_cubic(segment: np.ndarray) -> np.ndarray:
    """Flattens a cubic until its polyline length has converged."""
    x0, y0 = float(segment[0, 0]), float(segment[0, 1])
    x1, y1 = float(segment[1, 0]), float(segment[1, 1])
    x2, y2 = float(segment[2, 0]), float(segment[2, 1])
    x3, y3 = float(segment[3, 0]), float(segment[3, 1])
    initial_length = (
        math.hypot(x1 - x0, y1 - y0)
        + math.hypot(x2 - x1, y2 - y1)
        + math.hypot(x3 - x2, y3 - y2)
    )
    tolerance = max(1e-4, initial_length * 1e-5)
    points = [(x0, y0)]
    stack = [(x0, y0, x1, y1, x2, y2, x3, y3, 0)]
    while stack:
        x0, y0, x1, y1, x2, y2, x3, y3, depth = stack.pop()
        chord = math.hypot(x3 - x0, y3 - y0)
        polygon = (
            math.hypot(x1 - x0, y1 - y0)
            + math.hypot(x2 - x1, y2 - y1)
            + math.hypot(x3 - x2, y3 - y2)
        )
        if polygon - chord <= tolerance or depth >= 12:
            points.append((x3, y3))
            continue

        x01, y01 = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        x12, y12 = (x1 + x2) * 0.5, (y1 + y2) * 0.5
        x23, y23 = (x2 + x3) * 0.5, (y2 + y3) * 0.5
        x012, y012 = (x01 + x12) * 0.5, (y01 + y12) * 0.5
        x123, y123 = (x12 + x23) * 0.5, (y12 + y23) * 0.5
        xm, ym = (x012 + x123) * 0.5, (y012 + y123) * 0.5
        next_depth = depth + 1
        stack.append((xm, ym, x123, y123, x23, y23, x3, y3, next_depth))
        stack.append((x0, y0, x01, y01, x012, y012, xm, ym, next_depth))
    return np.asarray(points, dtype=np.float64)


def _sample_boundary(boundary: _Boundary, count: int) -> np.ndarray:
    if boundary.kind == "circle":
        assert boundary.center_x is not None
        assert boundary.center_y is not None
        assert boundary.radius is not None
        angle = np.arange(count, dtype=np.float64) * (2 * math.pi / count)
        return np.column_stack(
            (
                boundary.center_x + boundary.radius * np.cos(angle),
                boundary.center_y + boundary.radius * np.sin(angle),
            )
        )

    cubics = _path_cubics(boundary.path or "")
    pieces = [_flatten_cubic(segment) for segment in cubics]
    dense = np.concatenate((pieces[0], *(piece[1:] for piece in pieces[1:])))
    distances = np.linalg.norm(np.diff(dense, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(distances)))
    total = float(cumulative[-1])
    if total <= 1e-12:
        return np.broadcast_to(dense[0], (count, 2)).copy()
    targets = np.arange(count, dtype=np.float64) * (total / count)
    return np.column_stack(
        (
            np.interp(targets, cumulative, dense[:, 0]),
            np.interp(targets, cumulative, dense[:, 1]),
        )
    )


def _align_points(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 2:
        raise ValueError(f"Closed curve shapes differ: {source.shape} versus {target.shape}")
    source_fft = np.fft.fft(source, axis=0)
    best_score = -math.inf
    best: np.ndarray | None = None
    for candidate in (target, target[::-1]):
        correlation = np.fft.ifft(
            source_fft * np.conjugate(np.fft.fft(candidate, axis=0)),
            axis=0,
        ).real.sum(axis=1)
        shift = int(np.argmax(correlation))
        score = float(correlation[shift])
        if score > best_score:
            best_score = score
            best = np.roll(candidate, shift, axis=0).copy()
    assert best is not None
    return best


def _centroid(points: np.ndarray) -> np.ndarray:
    following = np.roll(points, -1, axis=0)
    cross = points[:, 0] * following[:, 1] - following[:, 0] * points[:, 1]
    area_twice = float(np.sum(cross))
    if abs(area_twice) < 1e-9:
        return np.mean(points, axis=0)
    return np.sum((points + following) * cross[:, None], axis=0) / (3 * area_twice)


class _BoundaryCache:
    def __init__(self, max_entries: int = 8192, max_array_bytes: int = 64 * 1024 * 1024):
        if max_entries < 0:
            raise ValueError("max_entries must be non-negative")
        if max_array_bytes < 0:
            raise ValueError("max_array_bytes must be non-negative")
        self.max_entries = max_entries
        self.max_array_bytes = max_array_bytes
        self._samples: OrderedDict[tuple[_Boundary, int], np.ndarray] = OrderedDict()
        self._counts: OrderedDict[_Boundary, int] = OrderedDict()
        self._alignments: OrderedDict[tuple[_Boundary, _Boundary, int], np.ndarray] = OrderedDict()
        self._array_lru: OrderedDict[tuple[str, object], None] = OrderedDict()
        self._array_bytes = 0

    def _touch_array(self, kind: str, key: object) -> None:
        self._array_lru.move_to_end((kind, key))

    def _discard_array(self, kind: str, key: object) -> None:
        if kind == "sample":
            value = self._samples.pop(key, None)
        else:
            value = self._alignments.pop(key, None)
        if value is not None:
            self._array_bytes -= value.nbytes
        self._array_lru.pop((kind, key), None)

    def _store_array(self, kind: str, key: object, value: np.ndarray) -> None:
        if value.nbytes > self.max_array_bytes:
            return
        if kind == "sample":
            cache = self._samples
        else:
            cache = self._alignments
        cache[key] = value
        self._array_bytes += value.nbytes
        self._array_lru[(kind, key)] = None

        if len(cache) > self.max_entries:
            self._discard_array(kind, next(iter(cache)))
        while self._array_bytes > self.max_array_bytes:
            oldest_kind, oldest_key = next(iter(self._array_lru))
            self._discard_array(oldest_kind, oldest_key)

    def count(self, boundary: _Boundary) -> int:
        value = self._counts.get(boundary)
        if value is not None:
            self._counts.move_to_end(boundary)
            return value
        value = _sample_count(boundary)
        self._counts[boundary] = value
        if len(self._counts) > self.max_entries:
            self._counts.popitem(last=False)
        return value

    def get(self, boundary: _Boundary, count: int) -> np.ndarray:
        key = (boundary, count)
        value = self._samples.get(key)
        if value is not None:
            self._samples.move_to_end(key)
            self._touch_array("sample", key)
            return value
        value = _sample_boundary(boundary, count)
        self._store_array("sample", key, value)
        return value

    def align(self, source: _Boundary, target: _Boundary, count: int) -> np.ndarray:
        key = (source, target, count)
        value = self._alignments.get(key)
        if value is not None:
            self._alignments.move_to_end(key)
            self._touch_array("alignment", key)
            return value
        value = _align_points(self.get(source, count), self.get(target, count))
        self._store_array("alignment", key, value)
        return value


def _encode_interval(source: _Frame, target: _Frame, cache: _BoundaryCache) -> dict[str, object]:
    records: list[list[int]] = []
    chunks: list[np.ndarray] = []
    offset = 0
    for detector_id in sorted(source.regions.keys() | target.regions.keys()):
        source_region = source.regions.get(detector_id)
        target_region = target.regions.get(detector_id)
        geometry_changed = (
            source_region is None
            or target_region is None
            or source_region.boundary != target_region.boundary
        )
        style_changed = (
            source_region is None
            or target_region is None
            or source_region.style != target_region.style
        )
        if not geometry_changed and not style_changed:
            continue

        flags = 0
        if source_region is not None:
            flags |= _HAS_SOURCE
        if target_region is not None:
            flags |= _HAS_TARGET
        if geometry_changed:
            flags |= _GEOMETRY_CHANGED
        if style_changed:
            flags |= _STYLE_CHANGED

        count = 0
        if geometry_changed:
            count = max(
                cache.count(region.boundary)
                for region in (source_region, target_region)
                if region is not None
            )
            source_points = (
                cache.get(source_region.boundary, count) if source_region is not None else None
            )
            target_points = (
                cache.get(target_region.boundary, count) if target_region is not None else None
            )
            if source_points is None:
                assert target_points is not None
                source_points = np.broadcast_to(
                    _centroid(target_points), target_points.shape
                ).copy()
            elif target_points is None:
                target_points = np.broadcast_to(
                    _centroid(source_points), source_points.shape
                ).copy()
            else:
                target_points = cache.align(source_region.boundary, target_region.boundary, count)
            packed = np.concatenate((source_points.reshape(-1), target_points.reshape(-1))).astype(
                "<f4", copy=False
            )
            chunks.append(packed)
            next_offset = offset + len(packed)
        else:
            next_offset = offset

        records.append([detector_id, flags, offset, count])
        offset = next_offset

    packed_bytes = np.concatenate(chunks).astype("<f4", copy=False).tobytes() if chunks else b""
    return {
        "records": records,
        "points": base64.b64encode(packed_bytes).decode("ascii"),
    }


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")


class _FrameSource:
    """The only adapter between the animation compiler and Stim."""

    def __init__(self, circuit: stim.Circuit, filter_coords: object):
        self._circuit = circuit
        self._filter_coords = filter_coords
        self._terminal_tick = circuit.num_ticks
        self._terminal_circuit: stim.Circuit | None = None

    def svg_at(self, tick: int) -> str:
        circuit = self._circuit
        if tick == self._terminal_tick:
            if self._terminal_circuit is None:
                self._terminal_circuit = self._circuit + stim.Circuit("TICK")
            circuit = self._terminal_circuit
        return str(
            circuit.diagram(
                "detslice-with-ops-svg",
                tick=tick,
                filter_coords=self._filter_coords,
            )
        )


def _write(stream: io.BytesIO, text: str) -> None:
    stream.write(text.encode("utf-8"))


def _render_frame(frame_source: _FrameSource, tick: int) -> tuple[str, _Frame]:
    svg = frame_source.svg_at(tick)
    return svg, _parse_frame(svg)


def _trim_to_visible_lifetime(
    frame_source: _FrameSource,
    ticks: range,
) -> tuple[range, dict[int, tuple[str, _Frame]]]:
    """Finds the first and last visible filtered region without assuming continuity."""
    known: dict[int, tuple[str, _Frame]] = {}
    previous: tuple[str, _Frame] | None = None
    first_visible: int | None = None
    for tick in ticks:
        rendered = _render_frame(frame_source, tick)
        if rendered[1].has_visible_region:
            first_visible = tick
            known[tick] = rendered
            if previous is not None:
                known[tick - 1] = previous
            break
        previous = rendered

    if first_visible is None:
        raise ValueError("filter_coords did not select any detector or observable region")

    following: tuple[str, _Frame] | None = None
    last_visible: int | None = None
    for tick in range(ticks.stop - 1, first_visible - 1, -1):
        rendered = known.get(tick)
        if rendered is None:
            rendered = _render_frame(frame_source, tick)
        if rendered[1].has_visible_region:
            last_visible = tick
            known[tick] = rendered
            if following is not None:
                known[tick + 1] = following
            break
        following = rendered

    assert last_visible is not None
    start = max(ticks.start, first_visible - 1)
    stop = min(ticks.stop, last_visible + 2)
    return range(start, stop), known


def compile_animation(
    circuit: stim.Circuit,
    *,
    ticks: range,
    filter_coords: object,
    trim_to_filtered_lifetime: bool,
    seconds_per_tick: float,
    autoplay: bool,
    loop: bool,
) -> _BuildResult:
    """Compiles exact Stim SVG keyframes and vector transitions in memory."""
    started = time.perf_counter()
    frame_source = _FrameSource(circuit, filter_coords)
    known: dict[int, tuple[str, _Frame]] = {}
    if trim_to_filtered_lifetime:
        ticks, known = _trim_to_visible_lifetime(frame_source, ticks)

    def frame_at(tick: int) -> tuple[str, _Frame]:
        rendered = known.pop(tick, None)
        return rendered if rendered is not None else _render_frame(frame_source, tick)

    boundary_cache = _BoundaryCache()
    stream = io.BytesIO()
    _write(stream, HTML_HEAD)
    _write(
        stream,
        '{"version":1,"startTick":'
        + str(ticks.start)
        + ',"secondsPerTick":'
        + _json(seconds_per_tick)
        + ',"autoplay":'
        + _json(autoplay)
        + ',"loop":'
        + _json(loop)
        + ',"ticks":[',
    )

    current_svg, current = frame_at(ticks.start)
    for index, tick in enumerate(range(ticks.start, ticks.stop - 1)):
        target_svg, target = frame_at(tick + 1)
        if current.view_box != target.view_box and (current.regions or target.regions):
            raise RuntimeError(
                f"Stim SVG viewBox changed from {current.view_box} to {target.view_box}."
            )
        interval = _encode_interval(current, target, boundary_cache)
        if index:
            _write(stream, ",")
        _write(stream, '{"exact":')
        _write(stream, _json(current_svg))
        _write(stream, ',"interval":')
        _write(stream, _json(interval))
        _write(stream, "}")
        current_svg = target_svg
        current = target

    if ticks.stop - ticks.start > 1:
        _write(stream, ",")
    _write(stream, _json({"exact": current_svg}))
    _write(stream, "]}")
    _write(stream, HTML_TAIL)
    return _BuildResult(content=stream.getvalue(), seconds=time.perf_counter() - started)
