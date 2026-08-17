from __future__ import annotations

import math
import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import numpy as np

from ._stim_svg import local_name


_PATH_TOKEN_RE = re.compile(
    r"[MLCZmlcz]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
)


@dataclass(frozen=True)
class BoundaryShape:
    tag: str
    path_data: str | None = None
    cx: float | None = None
    cy: float | None = None
    radius: float | None = None


def parse_stim_path_cubics(path_data: str) -> np.ndarray:
    """Converts Stim's absolute M/L/C/Z path subset into exact cubic segments."""
    tokens = _PATH_TOKEN_RE.findall(path_data.replace(",", " "))
    index = 0
    command: str | None = None
    current: np.ndarray | None = None
    start: np.ndarray | None = None
    segments: list[np.ndarray] = []

    def is_command(token: str) -> bool:
        return len(token) == 1 and token.isalpha()

    def read_numbers(count: int) -> list[float]:
        nonlocal index
        if index + count > len(tokens) or any(is_command(v) for v in tokens[index : index + count]):
            raise ValueError(f"Malformed SVG path near token {index}: {path_data!r}")
        result = [float(v) for v in tokens[index : index + count]]
        index += count
        return result

    def append_line(end: np.ndarray) -> None:
        nonlocal current
        assert current is not None
        delta = end - current
        segments.append(
            np.stack([current, current + delta / 3.0, current + delta * (2.0 / 3.0), end])
        )
        current = end

    while index < len(tokens):
        if is_command(tokens[index]):
            command = tokens[index]
            index += 1
            if command in "mlcz":
                raise ValueError(f"Relative SVG command {command!r} is not expected")
            if command == "Z":
                if current is not None and start is not None and not np.allclose(current, start):
                    append_line(start.copy())
                command = None
                continue
        if command is None:
            raise ValueError(f"Missing SVG path command in {path_data!r}")
        if command == "M":
            current = np.asarray(read_numbers(2), dtype=np.float64)
            start = current.copy()
            command = "L"
        elif command == "L":
            append_line(np.asarray(read_numbers(2), dtype=np.float64))
        elif command == "C":
            if current is None:
                raise ValueError("Cubic command before move command")
            values = np.asarray(read_numbers(6), dtype=np.float64).reshape(3, 2)
            segments.append(np.vstack([current, values]))
            current = values[-1]
        else:
            raise ValueError(f"Unsupported SVG path command {command!r}")
    if not segments:
        raise ValueError(f"Path has no drawable segments: {path_data!r}")
    return np.asarray(segments, dtype=np.float64)


def _split_cubic(segment: np.ndarray, t: float) -> tuple[np.ndarray, np.ndarray]:
    p01 = segment[0] * (1 - t) + segment[1] * t
    p12 = segment[1] * (1 - t) + segment[2] * t
    p23 = segment[2] * (1 - t) + segment[3] * t
    p012 = p01 * (1 - t) + p12 * t
    p123 = p12 * (1 - t) + p23 * t
    point = p012 * (1 - t) + p123 * t
    return np.stack([segment[0], p01, p012, point]), np.stack([point, p123, p23, segment[3]])


def _subdivide_cubic(segment: np.ndarray, count: int) -> list[np.ndarray]:
    if count < 1:
        raise ValueError("A cubic needs at least one output segment")
    result: list[np.ndarray] = []
    remainder = segment
    for remaining in range(count, 1, -1):
        first, remainder = _split_cubic(remainder, 1.0 / remaining)
        result.append(first)
    result.append(remainder)
    return result


def _cubic_control_polygon_lengths(segments: np.ndarray) -> np.ndarray:
    return np.sum(np.linalg.norm(np.diff(segments, axis=1), axis=2), axis=1)


def _allocate_subdivisions(segments: np.ndarray, count: int) -> np.ndarray:
    segment_count = len(segments)
    if count < segment_count:
        raise ValueError(f"Cannot preserve {segment_count} exact segments using only {count}")
    lengths = _cubic_control_polygon_lengths(segments)
    remaining = count - segment_count
    if remaining == 0:
        return np.ones(segment_count, dtype=np.int64)
    total = float(np.sum(lengths))
    weights = lengths / total if total > 1e-12 else np.full(segment_count, 1 / segment_count)
    exact = weights * remaining
    extra = np.floor(exact).astype(np.int64)
    leftover = remaining - int(np.sum(extra))
    if leftover:
        order = np.argsort(-(exact - extra), kind="stable")
        extra[order[:leftover]] += 1
    return extra + 1


def boundary_to_cubics(group: ET.Element, segment_count: int) -> np.ndarray:
    """Normalizes a boundary into a compact, geometry-preserving cubic loop."""
    shape = find_boundary_shape(group)
    if shape.tag == "circle":
        assert shape.cx is not None and shape.cy is not None and shape.radius is not None
        if segment_count < 4:
            raise ValueError("A circle needs at least four cubic segments")
        angles = np.arange(segment_count, dtype=np.float64) * (2 * math.pi / segment_count)
        delta = 2 * math.pi / segment_count
        handle = (4.0 / 3.0) * math.tan(delta / 4.0) * shape.radius
        points = np.column_stack(
            [shape.cx + shape.radius * np.cos(angles), shape.cy + shape.radius * np.sin(angles)]
        )
        tangents = np.column_stack([-np.sin(angles), np.cos(angles)])
        ends = np.roll(points, -1, axis=0)
        end_tangents = np.roll(tangents, -1, axis=0)
        return np.stack(
            [points, points + handle * tangents, ends - handle * end_tangents, ends],
            axis=1,
        )
    assert shape.path_data is not None
    raw = parse_stim_path_cubics(shape.path_data)
    allocation = _allocate_subdivisions(raw, segment_count)
    pieces: list[np.ndarray] = []
    for segment, count in zip(raw, allocation):
        pieces.extend(_subdivide_cubic(segment, int(count)))
    return np.asarray(pieces, dtype=np.float64)


def align_closed_cubics(
    source: np.ndarray, target: np.ndarray
) -> tuple[np.ndarray, bool, int, float]:
    if source.shape != target.shape or source.ndim != 3 or source.shape[1:] != (4, 2):
        raise ValueError(f"Cubic curve shapes differ: {source.shape} versus {target.shape}")
    best: tuple[float, bool, int, np.ndarray] | None = None
    count = len(source)
    # Cubic normalization intentionally keeps this count small (normally 16
    # or 32). Materializing all rotations is faster here than dispatching four
    # tiny FFTs per detector.
    rotation_indices = (
        np.arange(count, dtype=np.int64)[None, :]
        - np.arange(count, dtype=np.int64)[:, None]
    ) % count
    for reversed_orientation, candidate in (
        (False, target),
        (True, target[::-1, ::-1, :]),
    ):
        rotated_starts = candidate[rotation_indices, 0, :]
        errors = np.mean(
            np.sum((source[None, :, 0, :] - rotated_starts) ** 2, axis=2),
            axis=1,
        )
        shift = int(np.argmin(errors))
        shifted = candidate[rotation_indices[shift]].copy()
        error = float(errors[shift])
        if best is None or error < best[0]:
            best = (error, reversed_orientation, shift, shifted)
    assert best is not None
    error, reversed_orientation, shift, shifted = best
    return shifted, reversed_orientation, shift, error


def find_boundary_shape(group: ET.Element) -> BoundaryShape:
    candidates: list[ET.Element] = []
    for element in group.iter():
        tag = local_name(element.tag)
        if tag not in {"path", "circle"}:
            continue
        if element.attrib.get("stroke") == "black" and element.attrib.get("fill") == "none":
            candidates.append(element)
    if not candidates:
        raise ValueError(f"Detector group {group.attrib.get('id')} has no black outline")
    element = candidates[-1]
    tag = local_name(element.tag)
    if tag == "path":
        return BoundaryShape(tag="path", path_data=element.attrib["d"])
    return BoundaryShape(
        tag="circle",
        cx=float(element.attrib["cx"]),
        cy=float(element.attrib["cy"]),
        radius=float(element.attrib["r"]),
    )
