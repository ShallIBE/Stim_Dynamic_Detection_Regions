from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET


_SLICE_ID_RE = re.compile(r"^slice:(?P<detector>\d+):(?P<coords>.*):(?P<boundary>\d+)$")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class DetectorRegion:
    detector_id: int
    coordinate_token: str
    boundary_index: int
    element: ET.Element


@dataclass(frozen=True)
class StimSvgFrame:
    tick: int
    raw_svg: str
    root_attributes: dict[str, str]
    detectors: dict[int, DetectorRegion]

    @property
    def view_box(self) -> tuple[float, float, float, float]:
        values = tuple(float(v) for v in self.root_attributes["viewBox"].split())
        if len(values) != 4:
            raise ValueError(f"Expected four viewBox values, got {values!r}")
        return values  # type: ignore[return-value]


def parse_detector_group_id(value: str) -> tuple[int, str, int] | None:
    match = _SLICE_ID_RE.fullmatch(value)
    if match is None:
        return None
    return (
        int(match.group("detector")),
        match.group("coords"),
        int(match.group("boundary")),
    )


def parse_stim_svg_frame(svg: str, *, tick: int) -> StimSvgFrame:
    root = ET.fromstring(svg)
    if local_name(root.tag) != "svg":
        raise ValueError("Stim diagram did not produce an SVG root")

    detectors: dict[int, DetectorRegion] = {}
    for child in root:
        element_id = child.attrib.get("id", "")
        parsed_id = parse_detector_group_id(element_id)
        if local_name(child.tag) == "g" and parsed_id is not None:
            detector_id, coords, boundary_index = parsed_id
            if detector_id in detectors:
                raise ValueError(f"Duplicate detector group D{detector_id} at tick {tick}")
            detectors[detector_id] = DetectorRegion(
                detector_id=detector_id,
                coordinate_token=coords,
                boundary_index=boundary_index,
                element=child,
            )

    return StimSvgFrame(
        tick=tick,
        raw_svg=svg,
        root_attributes=dict(root.attrib),
        detectors=detectors,
    )


def get_exact_stim_frame(circuit: object, tick: int) -> StimSvgFrame:
    """Gets and semantically parses Stim's exact SVG for one tick boundary."""
    svg = str(circuit.diagram("detslice-with-ops-svg", tick=tick))  # type: ignore[attr-defined]
    return parse_stim_svg_frame(svg, tick=tick)
