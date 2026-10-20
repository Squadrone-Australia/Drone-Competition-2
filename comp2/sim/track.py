"""Track definitions: the official published layouts, as editable JSON files.

A track file fully describes one circuit — arena extents, takeoff and landing
pads, the ordered gate list, the difficulty weight and the scoring constants.
Organisers author these to match the physical cage; teams get the same files,
because the competition publishes every layout before race day.

**These are absolute arena coordinates**, and unlike the previous competition
they are deliberately public: an open-loop choreographed run is a legitimate
strategy when the track is published. What stays out of reach is *live*
arena-absolute pose — the drone (real or simulated) never reports where it is
as a sensor, so :mod:`comp2.api` and the squadrone client remain body-frame
relative.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from .world import (
    DEFAULT_GATE_DIAMETER_M,
    DEFAULT_GATE_OPENING_M,
    DEFAULT_WEIGHTS,
    Gate,
    World,
)

TRACKS_DIR = Path(__file__).parent.parent / "tracks"
# Ring bottom off the floor. It is also what leaves room for the rig's own
# stand: the mast under the ring is GATE_MAST_M, so anything that clears this
# has legs left over.
MIN_GATE_CLEARANCE_M = 0.5


class GateSpec(BaseModel):
    x: float
    y: float
    z: float
    yaw_deg: float = 0.0
    # outer and clear diameters of the ring (the Squadrone gate: 0.70 / 0.50 m);
    # the band needs some width, or there is no gate around the opening
    diameter_m: float = Field(default=DEFAULT_GATE_DIAMETER_M, gt=0.0, le=2.0)
    opening_diameter_m: float = Field(default=DEFAULT_GATE_OPENING_M, gt=0.0)

    @model_validator(mode="after")
    def check_band(self):
        if self.opening_diameter_m >= self.diameter_m:
            raise ValueError("opening_diameter_m must be smaller than diameter_m")
        return self


class ArenaSpec(BaseModel):
    width_m: float = Field(default=6.0, gt=0.0)  # x, the cage's 6 m length
    depth_m: float = Field(default=4.0, gt=0.0)  # y, the cage's 4 m width
    height_m: float = Field(default=3.0, gt=0.0)


class TrackConfig(BaseModel):
    name: str
    description: str = ""
    difficulty: float = Field(default=1.0, gt=0.0)
    arena: ArenaSpec = ArenaSpec()
    takeoff: tuple[float, float]
    # heading the drone starts on, clockwise from +y (the brief: facing Gate 1)
    takeoff_heading_deg: float = 0.0
    landing: tuple[float, float]
    gates: list[GateSpec] = Field(min_length=1)
    t_ref_s: float = Field(default=30.0, gt=0.0)
    weights: dict[str, float] = dict(DEFAULT_WEIGHTS)

    @model_validator(mode="after")
    def check_geometry(self):
        a = self.arena
        for label, (px, py) in (("takeoff", self.takeoff), ("landing", self.landing)):
            if not (0 <= px <= a.width_m and 0 <= py <= a.depth_m):
                raise ValueError(f"{label} pad ({px:g}, {py:g}) is outside the arena")
        for i, g in enumerate(self.gates):
            # the ring occupies its outer edge, not just the opening
            r = half_h = g.diameter_m / 2
            if not (r <= g.x <= a.width_m - r and r <= g.y <= a.depth_m - r):
                raise ValueError(
                    f"gate {i + 1} at ({g.x:g}, {g.y:g}) does not fit the arena"
                )
            if g.z - half_h < MIN_GATE_CLEARANCE_M:
                raise ValueError(
                    f"gate {i + 1} ring bottom is below {MIN_GATE_CLEARANCE_M:g} m clearance"
                )
            if g.z + half_h > a.height_m:
                raise ValueError(
                    f"gate {i + 1} does not fit under the {a.height_m:g} m ceiling"
                )
        missing = set(DEFAULT_WEIGHTS) - set(self.weights)
        if missing:
            raise ValueError(f"weights missing {sorted(missing)}")
        return self

    def build(self) -> World:
        return World(
            width_m=self.arena.width_m,
            depth_m=self.arena.depth_m,
            height_m=self.arena.height_m,
            gates=[
                Gate(g.x, g.y, g.z, g.yaw_deg, g.diameter_m, g.opening_diameter_m)
                for g in self.gates
            ],
            start=tuple(self.takeoff),
            finish=tuple(self.landing),
            start_heading_deg=self.takeoff_heading_deg % 360.0,
            name=self.name,
            difficulty=self.difficulty,
            t_ref_s=self.t_ref_s,
            weights=dict(self.weights),
        )


def _track_files(directory: Path | None = None) -> list[Path]:
    d = directory or TRACKS_DIR
    return sorted(d.glob("*.json")) if d.is_dir() else []


def load_file(path: Path) -> TrackConfig:
    return TrackConfig.model_validate(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def catalog(directory: Path | None = None) -> list[dict]:
    """What the frontend's track picker offers, in filename order."""
    out = []
    for path in _track_files(directory):
        cfg = load_file(path)
        out.append(
            {
                "id": path.stem,
                "name": cfg.name,
                "description": cfg.description,
                "difficulty": cfg.difficulty,
                "gates": len(cfg.gates),
            }
        )
    return out


def names(directory: Path | None = None) -> list[str]:
    return [p.stem for p in _track_files(directory)]


def build(name: str, directory: Path | None = None) -> World:
    """The world for the track file ``<name>.json``."""
    d = directory or TRACKS_DIR
    path = d / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"unknown track: {name}")
    return load_file(path).build()
