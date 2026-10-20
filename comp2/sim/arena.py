"""What the cage *looks* like — swappable scenery, kept apart from its geometry.

A track says where the gates are; an arena says what the room around them looks
like. They are separate on purpose: a track must match the physical cage and is
organiser-authored, while appearance is venue dressing that a pilot may change
freely to see whether a detector survives it.

Everything here is pure data. :mod:`comp2.sim.render` turns it into pixels and
is the only module that imports cv2; nothing in this file knows about images.

Two presets bracket the range:

``CLEAN``
    The flat-shaded room the simulator has always drawn: no netting, no hall
    beyond it, no gloss, no camera imperfections. Deterministic, and the default
    for :func:`comp2.sim.render.render`, so geometry tests keep their pixels.

``CAGE``
    The ICONIP venue as photographed: black mesh walls, a bright hall behind
    them, a dark varnished floor that mirrors the gates, and a camera that
    vignettes and smears. This is what ``python -m comp2`` flies.

The libraries below are meant to be mixed. ``compose()`` takes one name from
each and :meth:`Appearance.randomize` picks a combination at random, which is
what the cockpit's Randomize button calls: a solution that only works on one
picture of one room should not be able to hide.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

BGR = tuple[int, int, int]


# --- component specs ------------------------------------------------------


@dataclass(frozen=True)
class FloorSpec:
    """The cage floor. ``gloss`` 0 is matt; above 0 it mirrors the gates.

    The reflection is the single biggest source of false positives in the real
    footage — about 93% of what an orange key selects there is below the
    horizon — so this is the knob that decides whether a detector is being
    tested or flattered.
    """

    bgr: BGR = (168, 158, 148)
    grid: bool = True  # the 0.5 m survey grid; no real floor has one
    grid_bgr: BGR = (140, 129, 119)
    gloss: float = 0.0
    plank_m: float = 0.0  # 0 = featureless; else board pitch
    plank_bgr: BGR = (60, 66, 92)


@dataclass(frozen=True)
class WallSpec:
    """The cage boundary itself, behind the netting."""

    bgr: BGR = (214, 206, 198)
    trim_bgr: BGR = (172, 162, 152)
    skirting_bgr: BGR = (140, 132, 124)
    ceiling_bgr: BGR = (236, 231, 226)
    panel_bgr: BGR = (252, 250, 248)
    opaque: bool = True  # False: the wall is netting and the hall shows through


@dataclass(frozen=True)
class NetSpec:
    """Black mesh between the camera and everything outside the cage.

    ``pitch_m`` is the cell size. At the Tello's focal length a 2 cm cell
    projects to about 3 px at 3 m and 1.5 px at 6 m — right at the sensor's
    Nyquist limit, which is why the real footage moires.
    """

    pitch_m: float = 0.02
    strand_m: float = 0.004
    bgr: BGR = (18, 18, 18)
    # Calibrated against the footage: at 0.30 the rendered mesh carries the same
    # high-frequency energy as the real one (Laplacian variance ~220 at 640 px).
    # Higher values look denser to the eye but put far more contrast into the
    # frame than the camera actually records.
    opacity: float = 0.30


@dataclass(frozen=True)
class PropSpec:
    """One object standing outside the net, in the plane of a hall wall.

    ``u`` runs along the wall and ``v`` up it, both metres; ``kind`` picks the
    shape :mod:`comp2.sim.render` draws.
    """

    kind: str
    u: float
    v: float
    w: float
    h: float
    bgr: BGR = (200, 200, 200)


@dataclass(frozen=True)
class HallSpec:
    """The room the cage stands in, drawn beyond the net."""

    depth_m: float = 3.0  # how far the hall wall sits outside the cage
    wall_bgr: BGR = (178, 180, 176)
    floor_bgr: BGR = (73, 80, 110)
    props: tuple[PropSpec, ...] = ()


@dataclass(frozen=True)
class CrowdSpec:
    """People outside the net. Each is a billboard in the hall wall's plane.

    Clothing colours are drawn per person from ``palette`` so a crowd reads as
    several different people rather than one repeated cut-out.
    """

    count: int = 0
    palette: tuple[BGR, ...] = (
        (52, 46, 44),
        (128, 96, 64),
        (60, 60, 150),
        (180, 170, 160),
        (44, 92, 60),
        (150, 140, 200),
    )
    skin: tuple[BGR, ...] = ((150, 175, 205), (96, 122, 160), (70, 90, 125))


@dataclass(frozen=True)
class LightSpec:
    """A strong light source outside the net — a doorway or a window.

    In the photographs this is the brightest thing in shot by a wide margin and
    is what drives the camera's auto-exposure, so it changes how the gate looks
    as much as the gate's own colour does.
    """

    wall: str = ""  # "" = no source; else north/south/east/west
    u: float = 0.0
    v: float = 0.0
    w: float = 1.0
    h: float = 2.1
    bgr: BGR = (252, 252, 250)
    intensity: float = 1.0


@dataclass(frozen=True)
class GateAppearance:
    """The ring's own colours, and its stand's.

    The fabric's orange is a red-orange (OpenCV hue ~4, saturation ~205 in the
    organisers' photographs) that reads very differently in sun and under a
    hall's lights, which is exactly why a detector has to cope with both.
    """

    orange_bgr: BGR = (30, 75, 245)
    dark_bgr: BGR = (30, 22, 22)
    light_bgr: BGR = (236, 230, 226)
    print_bgr: BGR = (250, 250, 250)
    stand_bgr: BGR = (196, 198, 198)
    elbow_bgr: BGR = (238, 240, 242)
    string_bgr: BGR = (248, 248, 248)  # the white cord of a hanging gate


@dataclass(frozen=True)
class ImagingSpec:
    """Everything the camera does to the picture after the optics form it."""

    vignette: float = 0.0  # 0 = none; 0.22 matches the measured corner falloff
    exposure: float = 0.0  # amplitude of the slow auto-exposure wander
    white_balance: float = 0.0
    noise: float = 0.0
    motion_blur: float = 0.0
    jpeg_quality: int = 0  # 0 = no compression stage
    bloom: float = 0.0


@dataclass(frozen=True)
class Appearance:
    """One complete look, assembled from the libraries below."""

    name: str = "clean"
    floor: FloorSpec = field(default_factory=FloorSpec)
    wall: WallSpec = field(default_factory=WallSpec)
    net: NetSpec | None = None
    hall: HallSpec | None = None
    crowd: CrowdSpec = field(default_factory=CrowdSpec)
    light: LightSpec = field(default_factory=LightSpec)
    gate: GateAppearance = field(default_factory=GateAppearance)
    imaging: ImagingSpec = field(default_factory=ImagingSpec)
    # How the rings are held up; one of MOUNTS. Appearance only — the ring is in
    # the same place either way.
    mount: str = "stand"
    seed: int = 0

    @property
    def is_clean(self) -> bool:
        """True when nothing here changes what the renderer used to draw."""
        return self.net is None and self.hall is None and self.floor.gloss == 0


# --- component libraries --------------------------------------------------
# Each library is a menu. The cockpit offers one dropdown per library plus a
# Randomize button that takes one entry from each, so a detector cannot be
# quietly tuned to a single fixed picture of a single room.

FLOORS: dict[str, FloorSpec] = {
    # The featureless grey the simulator has always drawn, kept so that CLEAN
    # reproduces exactly.
    "studio_grey": FloorSpec(),
    # The measured ICONIP floor: dark varnish that mirrors the gates back.
    "varnished_wood": FloorSpec(
        bgr=(73, 80, 110),
        grid=False,
        gloss=0.45,
        plank_m=0.18,
        plank_bgr=(62, 68, 95),
    ),
    "pale_parquet": FloorSpec(
        bgr=(150, 170, 190),
        grid=False,
        gloss=0.28,
        plank_m=0.12,
        plank_bgr=(138, 158, 178),
    ),
    "grey_sports": FloorSpec(bgr=(96, 98, 96), grid=False, gloss=0.10),
    "blue_vinyl": FloorSpec(bgr=(120, 92, 66), grid=False, gloss=0.35),
    "matt_concrete": FloorSpec(bgr=(132, 134, 136), grid=False, gloss=0.05),
}

NETS: dict[str, NetSpec | None] = {
    "none": None,
    "black_mesh": NetSpec(),
    "fine_black": NetSpec(pitch_m=0.012, strand_m=0.003, opacity=0.62),
    "coarse_black": NetSpec(pitch_m=0.035, strand_m=0.006, opacity=0.45),
    "dark_green": NetSpec(bgr=(38, 54, 34), opacity=0.5),
}


def _school_hall() -> tuple[PropSpec, ...]:
    """A hall like the photographed one: doors, notice boards, tables, chairs."""
    return (
        PropSpec("doorway", 1.2, 0.0, 0.95, 2.10, (248, 248, 246)),
        PropSpec("board", 3.6, 1.15, 1.10, 0.80, (150, 148, 145)),
        PropSpec("board", 5.2, 1.20, 0.70, 0.55, (156, 154, 150)),
        PropSpec("table", 4.4, 0.0, 1.50, 0.74, (222, 224, 226)),
        PropSpec("table", 0.4, 0.0, 1.20, 0.74, (214, 216, 218)),
        PropSpec("chair", 2.6, 0.0, 0.45, 0.85, (48, 48, 52)),
        PropSpec("chair", 6.0, 0.0, 0.45, 0.85, (44, 44, 48)),
        PropSpec("case", 5.6, 0.0, 0.55, 0.40, (40, 42, 46)),
    )


def _gymnasium() -> tuple[PropSpec, ...]:
    return (
        PropSpec("board", 1.0, 0.9, 2.40, 1.30, (170, 172, 168)),
        PropSpec("doorway", 4.8, 0.0, 1.60, 2.20, (240, 242, 240)),
        PropSpec("bench", 2.6, 0.0, 2.00, 0.42, (120, 150, 178)),
        PropSpec("chair", 6.2, 0.0, 0.45, 0.85, (60, 60, 64)),
    )


def _lab() -> tuple[PropSpec, ...]:
    return (
        PropSpec("table", 1.0, 0.0, 1.80, 0.80, (206, 208, 210)),
        PropSpec("table", 3.4, 0.0, 1.80, 0.80, (206, 208, 210)),
        PropSpec("monitor", 1.4, 0.80, 0.50, 0.32, (32, 32, 34)),
        PropSpec("monitor", 3.8, 0.80, 0.50, 0.32, (32, 32, 34)),
        PropSpec("board", 5.6, 1.10, 1.20, 0.85, (146, 148, 150)),
        PropSpec("doorway", 0.2, 0.0, 0.90, 2.05, (246, 246, 244)),
    )


HALLS: dict[str, HallSpec | None] = {
    "none": None,
    "school_hall": HallSpec(props=_school_hall()),
    "gymnasium": HallSpec(
        depth_m=4.5,
        wall_bgr=(196, 198, 192),
        floor_bgr=(120, 140, 165),
        props=_gymnasium(),
    ),
    "lab": HallSpec(
        depth_m=2.4,
        wall_bgr=(206, 206, 204),
        floor_bgr=(110, 112, 114),
        props=_lab(),
    ),
    "dark_curtains": HallSpec(
        depth_m=2.0, wall_bgr=(44, 42, 48), floor_bgr=(58, 60, 66), props=()
    ),
    "bare_wall": HallSpec(depth_m=2.0, props=()),
}

CROWDS: dict[str, CrowdSpec] = {
    "empty": CrowdSpec(count=0),
    "few": CrowdSpec(count=2),
    "several": CrowdSpec(count=4),
    "busy": CrowdSpec(count=7),
}

LIGHTS: dict[str, LightSpec] = {
    "none": LightSpec(),
    "doorway_left": LightSpec(wall="north", u=1.2, w=0.95, h=2.10),
    "doorway_right": LightSpec(wall="north", u=5.0, w=0.95, h=2.10),
    "window_east": LightSpec(wall="east", u=1.6, v=0.9, w=1.8, h=1.3),
    "window_west": LightSpec(wall="west", u=2.2, v=0.9, w=1.8, h=1.3),
    "wide_doors": LightSpec(wall="south", u=2.6, w=2.20, h=2.30),
}

IMAGING: dict[str, ImagingSpec] = {
    "ideal": ImagingSpec(),
    # Fitted to the dataset: 22% corner falloff, visible smear, JPEG mush.
    "tello": ImagingSpec(
        vignette=0.13,
        exposure=0.10,
        white_balance=0.04,
        noise=3.0,
        motion_blur=1.0,
        jpeg_quality=82,
        bloom=0.35,
    ),
    "harsh": ImagingSpec(
        vignette=0.34,
        exposure=0.22,
        white_balance=0.09,
        noise=6.0,
        motion_blur=1.8,
        jpeg_quality=62,
        bloom=0.6,
    ),
}

GATES: dict[str, GateAppearance] = {
    "fabric_vivid": GateAppearance(),
    # Under the hall's lights: the fabric darker and duller, the white checker
    # squares grey rather than white.
    "fabric_indoor": GateAppearance(
        orange_bgr=(40, 58, 172),
        dark_bgr=(34, 28, 30),
        light_bgr=(190, 186, 184),
        print_bgr=(200, 200, 198),
        stand_bgr=(160, 162, 162),
        elbow_bgr=(206, 208, 210),
        string_bgr=(214, 214, 214),
    ),
    # As photographed in sunlight: a hotter orange and a bluish white.
    "fabric_sunlit": GateAppearance(
        orange_bgr=(36, 74, 250),
        dark_bgr=(22, 15, 16),
        light_bgr=(240, 225, 218),
        print_bgr=(245, 245, 245),
        stand_bgr=(210, 212, 212),
        elbow_bgr=(245, 246, 248),
        string_bgr=(252, 252, 252),
    ),
}

# How the ring is held: on its PVC floor stand, or hung from the ceiling on two
# white strings. The names are the values the renderer and the 3D view switch on.
MOUNTS: dict[str, str] = {"stand": "stand", "hanging": "hanging"}

LIBRARIES = {
    "floor": FLOORS,
    "net": NETS,
    "hall": HALLS,
    "crowd": CROWDS,
    "light": LIGHTS,
    "imaging": IMAGING,
    "gate": GATES,
    "mount": MOUNTS,
}


def compose(
    name: str = "custom",
    *,
    floor: str = "studio_grey",
    net: str = "none",
    hall: str = "none",
    crowd: str = "empty",
    light: str = "none",
    imaging: str = "ideal",
    gate: str = "fabric_vivid",
    mount: str = "stand",
    light_intensity: float = 1.0,
    seed: int = 0,
) -> Appearance:
    """Build an :class:`Appearance` from one name per library."""
    picked = {
        "floor": floor,
        "net": net,
        "hall": hall,
        "crowd": crowd,
        "light": light,
        "imaging": imaging,
        "gate": gate,
        "mount": mount,
    }
    for key, value in picked.items():
        if value not in LIBRARIES[key]:
            raise ValueError(f"unknown {key}: {value!r}")
    return Appearance(
        name=name,
        floor=FLOORS[floor],
        # A netted cage has no opaque wall: the hall behind it shows through.
        wall=WallSpec(opaque=NETS[net] is None),
        net=NETS[net],
        hall=HALLS[hall],
        crowd=CROWDS[crowd],
        light=replace(LIGHTS[light], intensity=light_intensity),
        gate=GATES[gate],
        imaging=IMAGING[imaging],
        mount=MOUNTS[mount],
        seed=seed,
    )


def randomize(seed: int | None = None) -> Appearance:
    """One entry from every library, plus a fresh scenery seed."""
    rng = random.Random(seed)
    return compose(
        name="random",
        floor=rng.choice([k for k in FLOORS if k != "studio_grey"]),
        net=rng.choice([k for k in NETS if k != "none"]),
        hall=rng.choice([k for k in HALLS if k != "none"]),
        crowd=rng.choice(list(CROWDS)),
        light=rng.choice(list(LIGHTS)),
        imaging=rng.choice(["tello", "harsh"]),
        gate=rng.choice(["fabric_indoor", "fabric_sunlit"]),
        mount=rng.choice(list(MOUNTS)),
        light_intensity=round(rng.uniform(0.6, 1.4), 2),
        seed=rng.randrange(1 << 30),
    )


CLEAN = Appearance(name="clean")
CAGE = compose(
    "cage",
    floor="varnished_wood",
    net="black_mesh",
    hall="school_hall",
    crowd="few",
    light="doorway_left",
    imaging="tello",
    gate="fabric_indoor",
)

PRESETS: dict[str, Appearance] = {"clean": CLEAN, "cage": CAGE}


def build(name: str) -> Appearance:
    if name not in PRESETS:
        raise ValueError(f"unknown arena: {name!r} (have {sorted(PRESETS)})")
    return PRESETS[name]


def catalog() -> dict:
    """What the cockpit's arena panel offers: every menu, and the presets."""
    return {
        "presets": sorted(PRESETS),
        "libraries": {key: sorted(lib) for key, lib in LIBRARIES.items()},
    }


# --- display feed ---------------------------------------------------------
# The cockpit's 3D view draws the same arena the camera flies through, so it
# reads the same appearance rather than keeping a stylised palette of its own.
# Colours cross the wire as CSS hex because that is what three.js wants; the
# renderer's own BGR tuples never leave Python.


def _hex(bgr) -> str:
    b, g, r = (int(max(0, min(255, c))) for c in bgr)
    return f"#{r:02x}{g:02x}{b:02x}"


def _prop_display(prop: PropSpec) -> dict:
    return {
        "kind": prop.kind,
        "u": prop.u,
        "v": prop.v,
        "w": prop.w,
        "h": prop.h,
        "colour": _hex(prop.bgr),
    }


def to_display(app: Appearance, scenery: dict | None = None) -> dict:
    """The appearance, as the browser's 3D view needs it.

    Display only, like the rest of ``scene()``: it describes how the room looks,
    never where the aircraft is, so it adds no sensing surface.
    """
    crowd = []
    for wall, people in ((scenery or {}).get("crowd", {}) or {}).items():
        for u, stature, shirt, trousers, skin, standoff in people:
            crowd.append(
                {
                    "wall": wall,
                    "u": u,
                    "stature": stature,
                    "shirt": _hex(shirt),
                    "trousers": _hex(trousers),
                    "skin": _hex(skin),
                    "standoff": standoff,
                }
            )
    return {
        "name": app.name,
        "floor": {
            "colour": _hex(app.floor.bgr),
            "grid": app.floor.grid,
            "gloss": app.floor.gloss,
            "plank_m": app.floor.plank_m,
        },
        "wall": {"opaque": app.wall.opaque, "colour": _hex(app.wall.bgr)},
        "net": None
        if app.net is None
        else {
            "pitch_m": app.net.pitch_m,
            "colour": _hex(app.net.bgr),
            "opacity": app.net.opacity,
        },
        "hall": None
        if app.hall is None
        else {
            "depth_m": app.hall.depth_m,
            "wall_colour": _hex(app.hall.wall_bgr),
            "floor_colour": _hex(app.hall.floor_bgr),
            "props": [_prop_display(p) for p in app.hall.props],
        },
        "light": None
        if not app.light.wall
        else {
            "wall": app.light.wall,
            "u": app.light.u,
            "v": app.light.v,
            "w": app.light.w,
            "h": app.light.h,
            "colour": _hex(app.light.bgr),
            "intensity": app.light.intensity,
        },
        "gate": {
            "orange": _hex(app.gate.orange_bgr),
            "dark": _hex(app.gate.dark_bgr),
            "light": _hex(app.gate.light_bgr),
            "print": _hex(app.gate.print_bgr),
            "stand": _hex(app.gate.stand_bgr),
            "elbow": _hex(app.gate.elbow_bgr),
            "string": _hex(app.gate.string_bgr),
            "mount": app.mount,
        },
        "crowd": crowd,
    }
