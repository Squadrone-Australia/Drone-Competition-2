"""The racing arena: a rectangular cage with a sequence of ring gates.

Coordinates are metres, z up, matching the published competition layout: the
origin is the lower-left floor corner in plan view, x runs along the cage's
6 m length and y across its 4 m width. A gate is
an upright fabric ring standing on the floor on its own PVC stand; its
``yaw_deg`` is the compass heading of the ring plane's normal (0 = facing +y),
and the drone is expected to fly through along that normal. Gate list order is
pass order.
"""

import math
from dataclasses import dataclass, field

from .arena import CLEAN, Appearance

# ICONIP 2026 cage: 6 m (L) x 4 m (W) x 3 m (H). Length runs along x.
ARENA_W_M = 6.0
ARENA_D_M = 4.0
ARENA_H_M = 3.0
# Published Take off / Land pad, the same floor position for every circuit.
PAD_XY = (3.0, 3.0)

# The physical gate (Squadrone fabric ring): a printed fabric band stretched
# over a hoop, 0.70 m across the outside around a 0.50 m clear opening, so the
# band is 0.10 m wide. ``diameter_m`` is always that *outer* size — what a
# camera ranges the gate from. The drone flies through the clear opening, and
# that circle is what accuracy is scored against.
#
# The band is orange top and bottom (white "SQUADRONE.COM.AU" on the top
# segment, the Squadrone logo and strapline on the bottom one) and a black and
# white checkerboard left and right. Measured from the organisers' photographs:
# each orange segment spans about +/-40 deg either side of vertical, and the
# checker runs four squares across the band.
DEFAULT_GATE_DIAMETER_M = 0.70
DEFAULT_GATE_OPENING_M = 0.50
GATE_ORANGE_HALF_ANGLE_DEG = 40.0
GATE_CHECKER_ROWS = 4

# The PVC tube the floor stand is made of.
GATE_TUBE_M = 0.02
# By default the ring stands on the floor: a short
# mast under the ring's lowest point, a crossbar, a leg from each end of it
# down to the floor, and a foot across the gate plane under each leg so the rig
# cannot tip through its own opening. Plain grey PVC with white fittings — the
# stand is in shot below every gate, but it is not orange.
GATE_MAST_M = 0.20
# The white fitting where the mast meets the bottom of the ring.
GATE_CLAMP_M = 0.05
# Crossbar and feet: the stand's footprint, narrower than the ring it carries,
# so the arena fit check stays a function of ``diameter_m``.
GATE_FOOT_M = 0.37
# The arena's "hanging" mount instead hangs the ring from the ceiling on two
# white cords. Each is tied to the band's outer edge this many degrees either
# side of 12 o'clock and runs straight up; the cord is a few millimetres thick.
GATE_STRING_ANGLE_DEG = 30.0
GATE_STRING_M = 0.004


@dataclass(frozen=True)
class Gate:
    x: float
    y: float
    z: float  # ring centre height above the floor
    yaw_deg: float = 0.0  # heading of the ring plane normal (0 = +y)
    diameter_m: float = DEFAULT_GATE_DIAMETER_M  # outer edge of the band
    opening_diameter_m: float = DEFAULT_GATE_OPENING_M  # clear opening, scored

    @property
    def radius_m(self) -> float:
        return self.diameter_m / 2

    @property
    def opening_radius_m(self) -> float:
        """Radius of the clear opening inside the band — what is scored."""
        return self.opening_diameter_m / 2

    @property
    def band_m(self) -> float:
        """Width of the fabric band, inner edge to outer edge."""
        return (self.diameter_m - self.opening_diameter_m) / 2

    @property
    def stand_crossbar_z(self) -> float:
        """Top of the stand: the mast carries the ring from here upwards."""
        return self.z - self.radius_m - GATE_MAST_M

    def normal(self) -> tuple[float, float]:
        """Unit normal of the ring plane in the floor plane (nx, ny)."""
        r = math.radians(self.yaw_deg)
        return (math.sin(r), math.cos(r))


@dataclass
class World:
    width_m: float = ARENA_W_M
    depth_m: float = ARENA_D_M
    height_m: float = ARENA_H_M
    gates: list = field(default_factory=list)  # list[Gate], order = pass order
    start: tuple = PAD_XY  # takeoff pad centre (x, y)
    start_heading_deg: float = 0.0  # heading the drone starts facing
    finish: tuple = PAD_XY  # landing pad centre
    name: str = "track"
    # How the room around the gates looks. Geometry and dressing are kept apart:
    # a track must match the physical cage, appearance is free to vary.
    appearance: Appearance = CLEAN
    scenery: object | None = None  # per-episode randomisation, built by arena
    difficulty: float = 1.0
    t_ref_s: float = 30.0  # reference lap time for the time score
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @property
    def start_xy(self) -> tuple:
        return self.start


DEFAULT_WEIGHTS = {"smoothness": 0.4, "accuracy": 0.3, "time": 0.3}
