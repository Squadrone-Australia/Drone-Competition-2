"""The simulated drone's camera view.

Everything drawn here is projected through :mod:`comp2.sim.scene` using the
Tello's own camera model, so the cage is geometrically honest: a gate two
metres away really is two metres away, and its pixel radius inverts to its true
range with the same intrinsics a team's own OpenCV code would use.

Gates are drawn as the real rig is built: an upright Squadrone fabric ring,
0.70 m across the outside around a 0.50 m opening, orange top and bottom with
white print and black-and-white checkered left and right, standing on the
floor on a PVC stand, in a neutral room. The platform ships no detector — teams write their own — so the job here
is to make the gates *detectable* the way the physical gate is: high-contrast,
consistent colour, honest geometry.
"""

import functools
import math
import random

import cv2
import numpy as np

from ..vision.camera import SIM_INTRINSICS
from .arena import CLEAN
from .scene import NEAR, Camera, draw_line, fill_quad, fill_quad_alpha, shade
from .world import (
    GATE_CHECKER_ROWS,
    GATE_CLAMP_M,
    GATE_FOOT_M,
    GATE_MAST_M,
    GATE_ORANGE_HALF_ANGLE_DEG,
    GATE_STRING_ANGLE_DEG,
    GATE_TUBE_M,
)

INTRINSICS = SIM_INTRINSICS

# --- cage dressing --------------------------------------------------------
GRID_STEP_M = 0.5
PANEL_PITCH_M = 3.0  # how often a ceiling strip light repeats
PAD_RADIUS_M = 0.30

FLOOR_BGR = (168, 158, 148)
FLOOR_GRID_BGR = (140, 129, 119)
WALL_BGR = (214, 206, 198)
WALL_TRIM_BGR = (172, 162, 152)
SKIRTING_BGR = (140, 132, 124)
CEILING_BGR = (236, 231, 226)
CEILING_PANEL_BGR = (252, 250, 248)
RING_ORANGE_BGR = (30, 75, 245)  # the fabric's orange segments, top and bottom
CHECK_DARK_BGR = (30, 22, 22)  # the checker's near-black squares
CHECK_LIGHT_BGR = (236, 230, 226)  # ... and its white ones
PRINT_BGR = (250, 250, 250)  # lettering and logo printed on the orange
STAND_BGR = (196, 198, 198)  # grey PVC tube of the floor stand
ELBOW_BGR = (238, 240, 242)  # the stand's white fittings
STRING_BGR = (248, 248, 248)  # the white cord of a ceiling-hung gate
PAD_TAKEOFF_BGR = (90, 170, 90)
PAD_LANDING_BGR = (170, 120, 60)

# Rough Lambert term per wall, keyed by the wall's inward normal.
_WALL_SHADE = {(1, 0): 1.00, (0, -1): 0.93, (-1, 0): 0.86, (0, 1): 0.79}


def render_focal(frame_width: int) -> float:
    """Focal length in pixels for a frame of this width, for callers that need
    to turn a metre of motion into a count of smeared pixels."""
    return INTRINSICS.focal_px(frame_width)


def _palette(app):
    """Resolve the colours for one render.

    Under ``CLEAN`` this reads the module constants above *at call time*, which
    is what keeps them monkeypatchable: tests swap the gate colours to stand
    in for a different rig, and they must keep biting.
    """
    if app is CLEAN:
        return {
            "floor": FLOOR_BGR,
            "grid": FLOOR_GRID_BGR,
            "grid_on": True,
            "wall": WALL_BGR,
            "trim": WALL_TRIM_BGR,
            "skirting": SKIRTING_BGR,
            "ceiling": CEILING_BGR,
            "panel": CEILING_PANEL_BGR,
            "orange": RING_ORANGE_BGR,
            "dark": CHECK_DARK_BGR,
            "light": CHECK_LIGHT_BGR,
            "print": PRINT_BGR,
            "stand": STAND_BGR,
            "elbow": ELBOW_BGR,
            "string": STRING_BGR,
            "mount": "stand",
        }
    return {
        "floor": app.floor.bgr,
        "grid": app.floor.grid_bgr,
        "grid_on": app.floor.grid,
        "wall": app.wall.bgr,
        "trim": app.wall.trim_bgr,
        "skirting": app.wall.skirting_bgr,
        "ceiling": app.wall.ceiling_bgr,
        "panel": app.wall.panel_bgr,
        "orange": app.gate.orange_bgr,
        "dark": app.gate.dark_bgr,
        "light": app.gate.light_bgr,
        "print": app.gate.print_bgr,
        "stand": app.gate.stand_bgr,
        "elbow": app.gate.elbow_bgr,
        "string": app.gate.string_bgr,
        "mount": app.mount,
    }


def render(world, x, y, z, heading, w=640, h=480):
    """The drone's camera view — and nothing else.

    This is a *sensor* frame: it must contain only what a real camera would
    see. The minimap lives in :func:`draw_minimap`, which the server composites
    onto the display copy only.
    """
    app = getattr(world, "appearance", CLEAN)
    pal = _palette(app)
    cam = Camera.at(INTRINSICS, x, y, z, heading, w, h)
    img = np.empty((h, w, 3), np.uint8)
    img[:] = pal["ceiling"]  # backdrop for any gap at the seams
    _draw_room(img, cam, world, app, pal)
    _draw_floor_boards(img, cam, world.width_m, world.depth_m, app, pal)
    # Reflections sit on the floor, so they go down after the room and before
    # anything standing on it.
    _draw_reflections(img, cam, world, app, pal)
    _draw_pads(img, cam, world)
    _draw_gates(img, cam, world, pal)
    return img


# --- room -----------------------------------------------------------------


def _draw_room(img, cam, world, app, pal):
    """The box: floor, ceiling and four walls of a width x depth x height cage.

    A netted cage has no opaque wall, so the hall outside goes down first and
    the mesh over it; an opaque one keeps the plain painted room.
    """
    width, depth, height = world.width_m, world.depth_m, world.height_m
    a, b, top = width, depth, height
    if not app.wall.opaque:
        _draw_outside(img, cam, world, app, pal)
    fill_quad(img, cam, [(0, 0, 0), (a, 0, 0), (a, b, 0), (0, b, 0)], pal["floor"])
    if pal["grid_on"]:
        _draw_floor_grid(img, cam, a, b, pal["grid"])
    if not app.wall.opaque:
        return
    fill_quad(
        img, cam, [(0, 0, top), (a, 0, top), (a, b, top), (0, b, top)], pal["ceiling"]
    )
    _draw_ceiling_panels(img, cam, a, b, top, pal)

    walls = [
        ((-1, 0), [(a, 0, 0), (a, b, 0), (a, b, top), (a, 0, top)]),  # east
        ((1, 0), [(0, b, 0), (0, 0, 0), (0, 0, top), (0, b, top)]),  # west
        ((0, -1), [(0, b, 0), (a, b, 0), (a, b, top), (0, b, top)]),  # north
        ((0, 1), [(a, 0, 0), (0, 0, 0), (0, 0, top), (a, 0, top)]),  # south
    ]
    centre = {
        (-1, 0): (a, b / 2, top / 2),
        (1, 0): (0, b / 2, top / 2),
        (0, -1): (a / 2, b, top / 2),
        (0, 1): (a / 2, 0, top / 2),
    }
    for normal, corners in sorted(walls, key=lambda wl: -cam.depth_of(centre[wl[0]])):
        _draw_wall(img, cam, corners, _WALL_SHADE[normal], top, pal)


def _draw_wall(img, cam, corners, lighting, top, pal):
    fill_quad(img, cam, corners, shade(pal["wall"], lighting))
    a, b = np.array(corners[0], float), np.array(corners[1], float)
    for height, color, thickness in (
        (0.10, pal["skirting"], 3),
        (top - 0.25, pal["trim"], 2),
    ):
        rise = np.array([0.0, 0.0, height])
        draw_line(img, cam, a + rise, b + rise, shade(color, lighting), thickness)
    for corner in (corners[0], corners[1]):  # vertical corner seam
        base = np.array(corner, float)
        draw_line(img, cam, base, base + [0, 0, top], shade(pal["trim"], lighting), 1)


def _draw_floor_grid(img, cam, width, depth, colour):
    nx = max(1, int(round(width / GRID_STEP_M)))
    ny = max(1, int(round(depth / GRID_STEP_M)))
    for i in range(nx + 1):
        t = i * width / nx
        draw_line(img, cam, (t, 0, 0), (t, depth, 0), colour, 1)
    for i in range(ny + 1):
        t = i * depth / ny
        draw_line(img, cam, (0, t, 0), (width, t, 0), colour, 1)


def _draw_ceiling_panels(img, cam, width, depth, top, pal):
    """Strip lights — parallax furniture so forward motion reads on a bare
    ceiling."""
    z = top - 0.01
    rows = max(1, int(round(depth / PANEL_PITCH_M)))
    for cx in (width / 3, 2 * width / 3):
        for j in range(rows):
            y0 = depth * (j + 0.2) / rows
            y1 = depth * (j + 0.8) / rows
            fill_quad(
                img,
                cam,
                [
                    (cx - 0.12, y0, z),
                    (cx + 0.12, y0, z),
                    (cx + 0.12, y1, z),
                    (cx - 0.12, y1, z),
                ],
                pal["panel"],
            )


# --- pads -----------------------------------------------------------------


def _draw_pads(img, cam, world):
    for (px, py), color in (
        (world.start, PAD_TAKEOFF_BGR),
        (world.finish, PAD_LANDING_BGR),
    ):
        ring = [
            (px + PAD_RADIUS_M * math.cos(a), py + PAD_RADIUS_M * math.sin(a), 0.001)
            for a in np.linspace(0, 2 * math.pi, 16, endpoint=False)
        ]
        fill_quad(img, cam, ring, color)


# --- gates ----------------------------------------------------------------


def _frame_point(g, u, v, w=0.0, mirror=False):
    """World point `u` metres across the gate and `v` up from its centre.

    The ring lies in the vertical plane whose horizontal direction is
    perpendicular to the gate normal; +u is the ring's own +lateral side, which
    is the right-hand side seen from the approach. `w` steps out of that plane
    along the normal — only the stand's feet, which brace the rig fore and aft,
    need it.
    """
    yaw = math.radians(g.yaw_deg)
    lx, ly = math.cos(yaw), -math.sin(yaw)  # lateral (in-plane horizontal)
    nx, ny = g.normal()
    z = g.z + v
    # A mirror image in the floor plane is the same point with z negated: in a
    # pinhole camera that projects exactly where the reflection appears, so the
    # reflection is the same draw calls, not a second pass over pixels.
    return (g.x + u * lx + w * nx, g.y + u * ly + w * ny, -z if mirror else z)


def _frame_rect(img, cam, g, u0, u1, v0, v1, colour, mirror=False):
    fill_quad(
        img,
        cam,
        [
            _frame_point(g, u0, v0, mirror=mirror),
            _frame_point(g, u1, v0, mirror=mirror),
            _frame_point(g, u1, v1, mirror=mirror),
            _frame_point(g, u0, v1, mirror=mirror),
        ],
        colour,
    )


def _floor_rect(img, cam, g, u0, u1, w0, w1, v, colour, mirror=False):
    """A tube lying flat on the floor, seen from any direction the drone flies."""
    fill_quad(
        img,
        cam,
        [
            _frame_point(g, u0, v, w0, mirror),
            _frame_point(g, u1, v, w0, mirror),
            _frame_point(g, u1, v, w1, mirror),
            _frame_point(g, u0, v, w1, mirror),
        ],
        colour,
    )


# The ring's face is one texture, laid out once per palette and warped onto the
# gate plane through the homography the camera induces on it — exact for a
# flat ring, and the only practical way to carry the printed lettering. Angles
# are clockwise from 12 o'clock as seen from the approach side, so the print
# reads the right way round to a drone about to fly through.
TEXTURE_PX = 512  # the ring's face, edge to edge, at the finest mip level
_STRIP_W = 2048  # the band unrolled: columns for one full turn ...
_STRIP_H = 96  # ... and rows across it, outer edge first
_STRIP_PAD = 8  # columns wrapped onto each end, so the seam interpolates
_SECTOR_STEPS = 16  # polygons per orange segment in the close-range fallback


def _side_checker_count(inner_ratio: float) -> int:
    """Squares along one checkered segment, square at the band's mid radius."""
    span = math.radians(180.0 - 2 * GATE_ORANGE_HALF_ANGLE_DEG)
    mid = (1.0 + inner_ratio) / 2
    return max(1, round(mid * span / ((1.0 - inner_ratio) / GATE_CHECKER_ROWS)))


def _text_mask(text: str, width: int, height: int, thickness: int) -> np.ndarray:
    """`text` rasterised and stretched to fill a width x height box, 0..1."""
    font = cv2.FONT_HERSHEY_DUPLEX
    (tw, th), base = cv2.getTextSize(text, font, 2.0, thickness)
    pad = 2 * thickness
    canvas = np.zeros((th + base + 2 * pad, tw + 2 * pad), np.uint8)
    cv2.putText(canvas, text, (pad, th + pad), font, 2.0, 255, thickness, cv2.LINE_AA)
    ys, xs = np.nonzero(canvas)
    canvas = canvas[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    return cv2.resize(canvas, (width, height), interpolation=cv2.INTER_AREA) / 255.0


def _logo_mask(width: int, height: int) -> np.ndarray:
    """The bottom segment's print, upright: the node-graph mark, a rule, then
    "Squadrone" over its strapline."""
    m = np.zeros((height, width), np.float64)
    nodes = [(0.30, 0.25), (0.62, 0.45), (0.20, 0.62), (0.85, 0.20), (0.55, 0.85),
             (0.90, 0.72)]  # fmt: skip
    edges = [(0, 1), (1, 2), (1, 3), (1, 4), (4, 5), (2, 4)]
    x0, x1 = 0.04 * width, 0.21 * width
    y0, y1 = 0.14 * height, 0.86 * height
    pts = [(int(x0 + u * (x1 - x0)), int(y0 + v * (y1 - y0))) for u, v in nodes]
    icon = np.zeros_like(m, np.uint8)
    for a, b in edges:
        cv2.line(icon, pts[a], pts[b], 255, 2, cv2.LINE_AA)
    for p in pts:
        cv2.circle(icon, p, max(3, height // 14), 0, -1, cv2.LINE_AA)
        cv2.circle(icon, p, max(3, height // 14), 255, 2, cv2.LINE_AA)
    rule = int(0.25 * width)
    cv2.line(icon, (rule, int(0.12 * height)), (rule, int(0.88 * height)), 255, 2)
    m = np.maximum(m, icon / 255.0)
    tx0, tx1 = int(0.29 * width), int(0.96 * width)
    word = _text_mask("Squadrone", tx1 - tx0, int(0.48 * height), 3)
    w0 = int(0.12 * height)
    m[w0 : w0 + word.shape[0], tx0:tx1] = word
    line = _text_mask(
        "Unlocking minds, defying gravity", tx1 - tx0, int(0.14 * height), 2
    )
    l0 = int(0.70 * height)
    m[l0 : l0 + line.shape[0], tx0:tx1] = line
    return m


def _band_strip(pal: tuple, inner_ratio: float) -> np.ndarray:
    """The band unrolled into a premultiplied BGRA strip.

    Column 0 is 9 o'clock and columns run clockwise, so 12 o'clock is a
    quarter of the way along and 6 o'clock three quarters; row 0 is the outer
    edge. Orange top and bottom with the print, checker either side.
    """
    orange, dark, light, ink = (np.array(c, np.float64) for c in pal)
    w, h, half = _STRIP_W, _STRIP_H, GATE_ORANGE_HALF_ANGLE_DEG
    theta = (np.arange(w) + 0.5) / w * 360.0 - 90.0
    theta = (theta + 180.0) % 360.0 - 180.0  # clockwise from 12 o'clock
    off_vertical = np.minimum(np.abs(theta), 180.0 - np.abs(theta))
    n = _side_checker_count(inner_ratio)
    square = np.floor((np.abs(theta) - half) / (180.0 - 2 * half) * n).astype(int)
    row = np.floor((np.arange(h) + 0.5) / h * GATE_CHECKER_ROWS).astype(int)
    is_light = (square[None, :] + row[:, None]) % 2 == 1
    strip = np.where(is_light[..., None], light, dark)
    strip[:, off_vertical < half] = orange

    seg = int(round(2 * half / 360.0 * w))
    # top: the web address, letter tops toward the outer edge
    text = _text_mask("SQUADRONE.COM.AU", int(0.86 * seg), int(0.46 * h), 4)
    r0 = (h - text.shape[0]) // 2
    c0 = w // 4 - text.shape[1] // 2
    region = strip[r0 : r0 + text.shape[0], c0 : c0 + text.shape[1]]
    region += (ink - region) * text[..., None]
    # bottom: upright as seen, so turned half a revolution in the strip
    logo = _logo_mask(int(0.92 * seg), h)[::-1, ::-1]
    c0 = 3 * w // 4 - logo.shape[1] // 2
    region = strip[:, c0 : c0 + logo.shape[1]]
    region += (ink - region) * logo[..., None]

    bgra = np.concatenate([strip, np.ones((h, w, 1))], axis=2).astype(np.float32)
    p = _STRIP_PAD
    return np.concatenate([bgra[:, -p:], bgra, bgra[:, :p]], axis=1)


@functools.lru_cache(maxsize=32)
def _ring_texture(pal: tuple, inner_ratio: float) -> tuple:
    """The ring's face as a premultiplied BGRA mip chain, finest first.

    Each level spans the ring's outer diameter edge to edge; outside the band
    alpha is 0, and its two edges are a pixel of soft falloff.
    """
    strip = _band_strip(pal, inner_ratio)
    n = TEXTURE_PX
    c = (np.arange(n) + 0.5) / n * 2.0 - 1.0  # in units of the outer radius
    u, v = np.meshgrid(c, -c)
    theta = np.degrees(np.arctan2(u, v))  # clockwise from 12 o'clock
    col = ((theta + 90.0) % 360.0) / 360.0 * _STRIP_W + _STRIP_PAD - 0.5
    row = (1.0 - np.hypot(u, v)) / (1.0 - inner_ratio) * _STRIP_H - 0.5
    tex = cv2.remap(
        strip,
        col.astype(np.float32),
        row.astype(np.float32),
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    levels = [tex]
    while levels[-1].shape[0] > 8:
        levels.append(cv2.pyrDown(levels[-1]))
    return tuple(levels)


def _draw_ring(img, cam, g, pal, mirror=False):
    """The fabric band, warped from its texture onto the gate plane.

    The mip level is picked so a texel is about a pixel: the checker is finer
    than the sensor past a few metres, and sampling it at full resolution would
    sparkle in a way no camera does.
    """
    r = g.radius_m
    square = [(-r, r), (r, r), (r, -r), (-r, -r)]
    pts = cam.to_camera([_frame_point(g, u, v, mirror=mirror) for u, v in square])
    if np.any(pts[:, 2] < NEAR):
        _draw_ring_sectors(img, cam, g, pal, mirror)
        return
    dst = cam.project(pts).astype(np.float32)
    x0 = max(0, int(math.floor(dst[:, 0].min())))
    y0 = max(0, int(math.floor(dst[:, 1].min())))
    x1 = min(cam.w, int(math.ceil(dst[:, 0].max())) + 1)
    y1 = min(cam.h, int(math.ceil(dst[:, 1].max())) + 1)
    if x1 <= x0 or y1 <= y0 or abs(cv2.contourArea(dst)) < 1.0:
        return  # off screen, or edge-on: a band of no thickness
    extent = max(np.ptp(dst[:, 0]), np.ptp(dst[:, 1]))
    key = tuple(
        tuple(int(c) for c in pal[k]) for k in ("orange", "dark", "light", "print")
    )
    levels = _ring_texture(key, round(g.opening_radius_m / r, 4))
    k = 0
    while k + 1 < len(levels) and levels[k + 1].shape[0] >= extent:
        k += 1
    tex = levels[k]
    n = tex.shape[0]
    src = np.float32(
        [[-0.5, -0.5], [n - 0.5, -0.5], [n - 0.5, n - 0.5], [-0.5, n - 0.5]]
    )
    homography = cv2.getPerspectiveTransform(src, dst - np.float32([x0, y0]))
    face = cv2.warpPerspective(
        tex,
        homography,
        (x1 - x0, y1 - y0),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    roi = img[y0:y1, x0:x1]
    out = roi * (1.0 - face[..., 3:]) + face[..., :3]
    roi[:] = np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _draw_ring_sectors(img, cam, g, pal, mirror=False):
    """The band as flat polygons, for when part of it is behind the camera.

    Polygons clip against the near plane where a homography cannot. No print:
    at this range the drone is flying through the ring, not reading it.
    """
    half = GATE_ORANGE_HALF_ANGLE_DEG
    r_in, r_out = g.opening_radius_m, g.radius_m
    rows = GATE_CHECKER_ROWS

    def point(deg, r):
        a = math.radians(deg)
        return _frame_point(g, r * math.sin(a), r * math.cos(a), mirror=mirror)

    def cell(a0, a1, ra, rb, colour):
        fill_quad(
            img,
            cam,
            [point(a0, ra), point(a1, ra), point(a1, rb), point(a0, rb)],
            colour,
        )

    for centre in (0.0, 180.0):
        for j in range(_SECTOR_STEPS):
            a0 = centre - half + 2 * half * j / _SECTOR_STEPS
            a1 = centre - half + 2 * half * (j + 1) / _SECTOR_STEPS
            cell(a0, a1, r_out, r_in, pal["orange"])
    n = _side_checker_count(r_in / r_out)
    for side in (1, -1):
        for i in range(n):
            a0 = half + (180.0 - 2 * half) * i / n
            a1 = half + (180.0 - 2 * half) * (i + 1) / n
            for k in range(rows):
                ra = r_out - (r_out - r_in) * k / rows
                rb = r_out - (r_out - r_in) * (k + 1) / rows
                colour = pal["light"] if (i + k) % 2 else pal["dark"]
                cell(side * a0, side * a1, ra, rb, colour)


def _draw_stand(img, cam, g, pal, mirror=False):
    """What holds the ring up: mast, crossbar, two legs, two feet.

    Grey PVC with white fittings. Nothing here is scored — but it is in shot
    below every gate, so it has to be in the sensor frame a team's detector
    reads.
    """
    hw, t = GATE_FOOT_M / 2, GATE_TUBE_M
    bottom = -g.radius_m  # the ring's lowest point, relative to its centre
    floor = -g.z
    bar = bottom - GATE_MAST_M  # underside of the crossbar's tube
    foot = GATE_FOOT_M / 2
    tube = pal["stand"]
    _frame_rect(img, cam, g, -t / 2, t / 2, bar, bottom, tube, mirror)  # mast
    _frame_rect(img, cam, g, -hw, hw, bar, bar + t, tube, mirror)  # crossbar
    for su in (-1, 1):
        leg = sorted((su * hw, su * (hw - t)))
        _frame_rect(img, cam, g, *leg, floor, bar, tube, mirror)  # leg
        # The foot lies across the gate plane, so it is a flat ribbon on the
        # floor rather than an upright tube seen edge-on from the approach.
        _floor_rect(img, cam, g, *leg, -foot, foot, floor + t, tube, mirror)
        # white tee where the leg meets its foot, and elbow at the crossbar
        _floor_rect(img, cam, g, *leg, -t, t, floor + t, pal["elbow"], mirror)
        _frame_rect(img, cam, g, *leg, bar, bar + t, pal["elbow"], mirror)
    # white tee joining the mast to the middle of the crossbar
    _frame_rect(img, cam, g, -t, t, bar, bar + t, pal["elbow"], mirror)
    # the clamp gripping the bottom of the ring; the ring is drawn after the
    # stand, so the band's lower edge stays one clean arc over it
    _frame_rect(img, cam, g, -t, t, bottom - GATE_CLAMP_M, bottom, pal["elbow"], mirror)


def _draw_strings(img, cam, g, pal, ceiling_z, mirror=False):
    """What holds the ring up when it hangs: two white cords to the ceiling.

    Each runs straight up from the ring's outer edge, 30 degrees either side of
    12 o'clock, to the ceiling. A cord is thinner than a pixel at most working
    distances, so it is a one-pixel line — which is also what keeps it there
    when the drone looks at it edge-on.
    """
    r = g.radius_m
    u = r * math.sin(math.radians(GATE_STRING_ANGLE_DEG))
    v0 = r * math.cos(math.radians(GATE_STRING_ANGLE_DEG))
    v1 = ceiling_z - g.z
    for su in (-1, 1):
        draw_line(
            img,
            cam,
            _frame_point(g, su * u, v0, mirror=mirror),
            _frame_point(g, su * u, v1, mirror=mirror),
            pal["string"],
            1,
        )


def _draw_support(img, cam, g, pal, ceiling_z, mirror=False):
    """Whatever carries the ring: the floor stand, or strings from the ceiling."""
    if pal["mount"] == "hanging":
        _draw_strings(img, cam, g, pal, ceiling_z, mirror)
    else:
        _draw_stand(img, cam, g, pal, mirror)


def _mix(a, b, t):
    """Blend BGR `a` toward `b` by t."""
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b, strict=True))


def _draw_reflections(img, cam, world, app, pal):
    """The gates mirrored in a glossy floor.

    This is the single largest source of false positives in the real arena: on
    the varnished floor about 93% of the pixels a warm colour key selects are
    below the horizon, and a detector that has only ever seen a matt floor walks
    straight into them. Drawn as the gate geometry reflected through z=0, dimmed
    toward the floor colour by the floor's gloss, so it is one extra pass of
    quads that are already being drawn.
    """
    gloss = app.floor.gloss
    if gloss <= 0:
        return
    wet = dict(pal)
    # Reflections are never as bright as the thing reflected; the floor's own
    # colour shows through in proportion to how matt it is.
    for key in ("orange", "dark", "light", "print", "stand", "string"):
        wet[key] = _mix(pal["floor"], pal[key], gloss)
    wet["elbow"] = _mix(pal["floor"], pal["elbow"], gloss * 0.8)
    order = sorted(world.gates, key=lambda g: -math.hypot(g.x - cam.x, g.y - cam.y))
    for g in order:
        _draw_support(img, cam, g, wet, world.height_m, mirror=True)
        _draw_ring(img, cam, g, wet, mirror=True)


def _draw_floor_boards(img, cam, width, depth, app, pal):
    """Board seams, so the floor carries texture instead of reading as paint."""
    pitch = app.floor.plank_m
    if pitch <= 0:
        return
    n = int(width / pitch)
    for i in range(1, n + 1):
        t = i * pitch
        draw_line(img, cam, (t, 0, 0), (t, depth, 0), app.floor.plank_bgr, 1)


def _draw_gates(img, cam, world, pal):
    order = sorted(world.gates, key=lambda g: -math.hypot(g.x - cam.x, g.y - cam.y))
    for g in order:
        _draw_support(img, cam, g, pal, world.height_m)
        _draw_ring(img, cam, g, pal)


# --- display-only overlay -------------------------------------------------


def draw_minimap(img, world, x, y, heading, size=140, pad=10):
    """Debug inset — display only, composited by the server onto the display
    copy of the frame, never the sensor frame.

    Gates appear as short oriented segments (the ring seen from above) with
    their order number beside them.
    """
    out = img.copy()
    s = size / max(world.width_m, world.depth_m)
    mw, mh = int(world.width_m * s), int(world.depth_m * s)
    x0, y0 = out.shape[1] - mw - pad, pad
    cv2.rectangle(out, (x0, y0), (x0 + mw, y0 + mh), (255, 255, 255), -1)
    cv2.rectangle(out, (x0, y0), (x0 + mw, y0 + mh), (60, 60, 60), 2)

    def to_px(wx, wy):
        return (int(x0 + wx * s), int(y0 + (world.depth_m - wy) * s))

    for (px, py), color in (
        (world.start, (90, 170, 90)),
        (world.finish, (170, 120, 60)),
    ):
        cv2.circle(out, to_px(px, py), 5, color, -1)
    for i, g in enumerate(world.gates):
        yaw = math.radians(g.yaw_deg)
        lx, ly = math.cos(yaw), -math.sin(yaw)
        r = g.radius_m
        a = to_px(g.x - r * lx, g.y - r * ly)
        b = to_px(g.x + r * lx, g.y + r * ly)
        cv2.line(out, a, b, (0, 110, 240), 2)
        gx, gy = to_px(g.x, g.y)
        cv2.putText(
            out,
            str(i + 1),
            (gx + 4, gy - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            (40, 40, 40),
            1,
            cv2.LINE_AA,
        )
    hr = math.radians(heading)
    px, py = to_px(x, y)
    tip = (int(px + 10 * math.sin(hr)), int(py - 10 * math.cos(hr)))
    cv2.circle(out, (px, py), 5, (200, 120, 0), -1)
    cv2.line(out, (px, py), tip, (200, 120, 0), 2)
    return out


# --- outside the cage -----------------------------------------------------
# Ordering, and why it is safe without a depth buffer: the camera always flies
# *inside* the cage, so every hall surface is further away than every net plane,
# which is further than everything standing on the cage floor. Painting
# hall -> props -> people -> net -> cage interior is therefore exactly
# back-to-front. It stops being true for anything placed inside the cage, so
# props belong outside it.

NET_MIN_PITCH_PX = 2.0  # below this a strand is finer than the sampling grid
NET_MAX_STRANDS = 700  # per frame, across every visible plane

# Each cage side, as (name, origin, u_axis, v_axis) with u along the wall and v
# up it, both unit vectors, and the origin at the wall's lower-left corner
# looking from inside.


def _walls(width, depth, height):
    return (
        ("north", (0.0, depth, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), width, height),
        ("south", (width, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0), width, height),
        ("east", (width, depth, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0), depth, height),
        ("west", (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), depth, height),
    )


def _at(origin, u_axis, v_axis, u, v, out=0.0, normal=(0.0, 0.0, 0.0)):
    return tuple(
        origin[i] + u_axis[i] * u + v_axis[i] * v + normal[i] * out for i in range(3)
    )


def _outward(u_axis, v_axis):
    """Unit normal pointing out of the cage, for pushing the hall behind a wall."""
    n = np.cross(u_axis, v_axis)
    return tuple(-n / (np.linalg.norm(n) or 1.0))


def _wall_in_view(cam, wall):
    """True if any corner of this cage side is in front of the near plane."""
    _name, origin, u_axis, v_axis, span, height = wall
    corners = [
        _at(origin, u_axis, v_axis, u, v)
        for u, v in ((0.0, 0.0), (span, 0.0), (span, height), (0.0, height))
    ]
    return bool((cam.to_camera(corners)[:, 2] >= NEAR).any())


def _draw_hall_wall(img, cam, wall, app, depth_m):
    """The room outside one cage side: its far wall, floor strip and ceiling."""
    _name, origin, u_axis, v_axis, span, height = wall
    out = _outward(u_axis, v_axis)
    hall = app.hall
    far = depth_m  # along `out`, which already points out of the cage
    # Widen beyond the cage so the hall fills the view at oblique angles.
    pad = depth_m + 1.0
    corners = [
        _at(origin, u_axis, v_axis, -pad, 0.0, far, out),
        _at(origin, u_axis, v_axis, span + pad, 0.0, far, out),
        _at(origin, u_axis, v_axis, span + pad, height + 1.5, far, out),
        _at(origin, u_axis, v_axis, -pad, height + 1.5, far, out),
    ]
    fill_quad(img, cam, corners, hall.wall_bgr)
    # The hall floor, running from the cage wall out to its own wall.
    fill_quad(
        img,
        cam,
        [
            _at(origin, u_axis, v_axis, -pad, 0.0, 0.0, out),
            _at(origin, u_axis, v_axis, span + pad, 0.0, 0.0, out),
            _at(origin, u_axis, v_axis, span + pad, 0.0, far, out),
            _at(origin, u_axis, v_axis, -pad, 0.0, far, out),
        ],
        hall.floor_bgr,
    )


def _draw_prop(img, cam, wall, prop, app, depth_m, rng):
    """One object standing against the hall wall, a few flat quads."""
    _name, origin, u_axis, v_axis, span, _height = wall
    out = _outward(u_axis, v_axis)
    far = depth_m - 0.02  # just in front of the hall wall

    def quad(u0, u1, v0, v1, colour, offset=0.0):
        fill_quad(
            img,
            cam,
            [
                _at(origin, u_axis, v_axis, u0, v0, far + offset, out),
                _at(origin, u_axis, v_axis, u1, v0, far + offset, out),
                _at(origin, u_axis, v_axis, u1, v1, far + offset, out),
                _at(origin, u_axis, v_axis, u0, v1, far + offset, out),
            ],
            colour,
        )

    u, v, w, h = prop.u, prop.v, prop.w, prop.h
    kind = prop.kind
    if kind in ("doorway", "board", "monitor"):
        quad(u, u + w, v, v + h, prop.bgr)
        if kind == "board":  # a few sheets of paper pinned to it
            for i in range(4):
                pu = u + 0.08 + i * (w - 0.16) / 4
                quad(pu, pu + 0.12, v + 0.12, v + 0.30, (245, 245, 242), 0.01)
    elif kind in ("table", "bench"):
        quad(u, u + w, v + h - 0.04, v + h, prop.bgr)  # top
        for lu in (u + 0.05, u + w - 0.12):
            quad(lu, lu + 0.06, v, v + h - 0.04, (90, 92, 96))  # legs
    elif kind == "chair":
        quad(u, u + w, v + 0.42, v + 0.48, prop.bgr)  # seat
        quad(u, u + w, v + 0.48, v + h, prop.bgr)  # back
        for lu in (u + 0.03, u + w - 0.08):
            quad(lu, lu + 0.04, v, v + 0.42, (70, 70, 74))
    elif kind == "case":
        quad(u, u + w, v, v + h, prop.bgr)


def _draw_person(img, cam, wall, person, depth_m):
    """A spectator outside the net: legs, torso, head, as three flat quads.

    Three quads is enough through a mesh at four metres, and keeps a crowd from
    costing more than the cage it stands around.
    """
    _name, origin, u_axis, v_axis, _span, _height = wall
    out = _outward(u_axis, v_axis)
    u, stature, shirt, trousers, skin, standoff = person
    far = depth_m - standoff

    def quad(u0, u1, v0, v1, colour):
        fill_quad(
            img,
            cam,
            [
                _at(origin, u_axis, v_axis, u0, v0, far, out),
                _at(origin, u_axis, v_axis, u1, v0, far, out),
                _at(origin, u_axis, v_axis, u1, v1, far, out),
                _at(origin, u_axis, v_axis, u0, v1, far, out),
            ],
            colour,
        )

    shoulder = 0.24 * stature / 1.75
    hip = 0.44 * stature
    neck = 0.84 * stature
    quad(u - shoulder * 0.8, u + shoulder * 0.8, 0.0, hip, trousers)
    quad(u - shoulder, u + shoulder, hip, neck, shirt)
    quad(u - 0.09, u + 0.09, neck, neck + 0.22, skin)


def _draw_light(img, cam, world, app, depth_m):
    """The bright opening — a doorway or window — outside the net.

    In the photographs this is the brightest thing in shot by a wide margin, and
    it is what the camera's auto-exposure chases, so it changes how the gate
    reads as much as the gate's own colour does.
    """
    light = app.light
    if not light.wall:
        return
    walls = {w[0]: w for w in _walls(world.width_m, world.depth_m, world.height_m)}
    wall = walls.get(light.wall)
    if wall is None:
        return
    _name, origin, u_axis, v_axis, _span, _height = wall
    out = _outward(u_axis, v_axis)
    far = depth_m - 0.01
    glow = tuple(min(255, int(c * max(0.2, light.intensity))) for c in light.bgr)
    fill_quad(
        img,
        cam,
        [
            _at(origin, u_axis, v_axis, light.u, light.v, far, out),
            _at(origin, u_axis, v_axis, light.u + light.w, light.v, far, out),
            _at(origin, u_axis, v_axis, light.u + light.w, light.v + light.h, far, out),
            _at(origin, u_axis, v_axis, light.u, light.v + light.h, far, out),
        ],
        glow,
    )


def _draw_net(img, cam, wall, net, budget):
    """Black mesh across one cage side.

    A strand is thinner than a pixel at any useful range, so drawing it as a
    solid black line is wrong twice over: it puts far more contrast into the
    frame than a real net does, and it hides what is behind it. What a sensor
    actually records is *partial coverage* — each pixel darkened in proportion
    to how much strand falls across it. So the strands are drawn into a coverage
    mask and composited once, scaled by the fraction of a pixel a strand really
    occupies, and the sub-pixel remainder becomes an even veil.

    Antialiasing stays on here because it is what turns a hairline into partial
    coverage; the moire still arrives, from the strand spacing beating against
    the pixel grid.
    """
    _name, origin, u_axis, v_axis, span, height = wall
    corners = [
        _at(origin, u_axis, v_axis, 0.0, 0.0),
        _at(origin, u_axis, v_axis, span, 0.0),
        _at(origin, u_axis, v_axis, span, height),
        _at(origin, u_axis, v_axis, 0.0, height),
    ]
    centre = _at(origin, u_axis, v_axis, span / 2, height / 2)
    depth = cam.depth_of(centre)
    if depth <= NEAR:
        return budget
    pitch_px = cam.focal * net.pitch_m / depth
    strand_px = cam.focal * net.strand_m / depth
    # How much of each cell a strand covers: two strands per cell, both axes.
    coverage = min(0.9, 2.0 * strand_px / max(pitch_px, 1e-6))
    step = 1
    while pitch_px * step < NET_MIN_PITCH_PX and step <= 64:
        step *= 2
    if step > 64:
        # Finer than the sampling grid however we subsample it: all veil.
        fill_quad_alpha(img, cam, corners, net.bgr, net.opacity * coverage)
        return budget
    # The strands we do draw carry their share; the rest stays a flat veil.
    drawn = 1.0 / step
    fill_quad_alpha(img, cam, corners, net.bgr, net.opacity * coverage * (1 - drawn))
    if budget <= 0:
        return budget
    pitch = net.pitch_m * step
    n_u, n_v = int(span / pitch), int(height / pitch)
    # Build every strand's endpoints in one array and project them together.
    # Drawing them one at a time costs more in Python and numpy overhead than
    # the rasteriser spends on the whole mesh.
    ends = []
    for i in range(1, n_u + 1):
        if len(ends) // 2 >= budget:
            break
        u = i * pitch
        ends.append(_at(origin, u_axis, v_axis, u, 0.0))
        ends.append(_at(origin, u_axis, v_axis, u, height))
    for i in range(1, n_v + 1):
        if len(ends) // 2 >= budget:
            break
        v = i * pitch
        ends.append(_at(origin, u_axis, v_axis, 0.0, v))
        ends.append(_at(origin, u_axis, v_axis, span, v))
    if not ends:
        return budget
    budget -= len(ends) // 2
    cam_pts = cam.to_camera(ends)
    depths = cam_pts[:, 2].reshape(-1, 2)
    # A strand with an end behind the camera is dropped rather than clipped: one
    # missing hairline at the very edge of frame is invisible among hundreds,
    # and clipping each one individually is what made this slow.
    keep = (depths >= NEAR).all(axis=1)
    if not keep.any():
        return budget
    screen = cam.project(cam_pts[np.repeat(keep, 2)])
    segments = np.round(screen).astype(np.int32).reshape(-1, 2, 2)
    # Off-screen coordinates are clamped rather than clipped: fillPoly-scale
    # integers would overflow, and a segment's visible part is unchanged.
    limit = 4 * max(cam.w, cam.h)
    np.clip(segments, -limit, limit, out=segments)
    mask = np.zeros(img.shape[:2], np.uint8)
    cv2.polylines(mask, list(segments), False, 255, 1, cv2.LINE_AA)
    # Composite the whole mesh in one pass, at the strands' true coverage, and
    # only over the rows and columns this wall actually reaches.
    alpha = float(np.clip(net.opacity * coverage * step, 0.0, 1.0))
    if alpha > 0:
        rows, cols = np.nonzero(mask.any(1))[0], np.nonzero(mask.any(0))[0]
        if len(rows) and len(cols):
            y0, y1 = int(rows[0]), int(rows[-1]) + 1
            x0, x1 = int(cols[0]), int(cols[-1]) + 1
            roi = img[y0:y1, x0:x1]
            a = (mask[y0:y1, x0:x1].astype(np.float32) / 255.0 * alpha)[:, :, None]
            tint = np.empty_like(roi)
            tint[:] = net.bgr
            np.copyto(roi, (roi * (1 - a) + tint * a).astype(np.uint8))
    return budget


def _crowd(app, world, rng):
    """Place the spectators once, per episode, not per frame.

    Rendering must not touch the RNG: several tests render the same pose twice
    and compare the frames pixel for pixel.
    """
    people = {}
    if app.crowd.count <= 0 or app.hall is None:
        return people
    names = [w[0] for w in _walls(world.width_m, world.depth_m, world.height_m)]
    spans = dict(
        zip(
            names,
            [world.width_m, world.width_m, world.depth_m, world.depth_m],
            strict=True,
        )
    )
    for _ in range(app.crowd.count):
        wall = rng.choice(names)
        people.setdefault(wall, []).append(
            (
                rng.uniform(-0.5, spans[wall] + 0.5),
                rng.uniform(1.55, 1.9),  # stature
                rng.choice(app.crowd.palette),  # shirt
                rng.choice(app.crowd.palette),  # trousers
                rng.choice(app.crowd.skin),
                rng.uniform(0.1, 0.9),  # how far in front of the hall wall
            )
        )
    return people


def build_scenery(app, world, seed=0):
    """Materialise one episode's randomised dressing.

    Everything variable is decided here and frozen, so a repeated render of the
    same pose is identical and a given seed reproduces a venue exactly.
    """
    rng = random.Random(seed ^ app.seed)
    return {"crowd": _crowd(app, world, rng)}


def _draw_outside(img, cam, world, app, pal):
    """The hall beyond the netting, then the netting itself."""
    walls = _walls(world.width_m, world.depth_m, world.height_m)
    depth_m = app.hall.depth_m if app.hall else 2.0
    # Far to near, so the painter's order is right without a depth test.
    # Skip a side the camera cannot see at all. Each one carries the hall's
    # whole furniture list, so culling the wall behind you is worth about a
    # quarter of the frame's cost for nothing in return.
    visible = [w for w in walls if _wall_in_view(cam, w)]
    ordered = sorted(
        visible, key=lambda w: -cam.depth_of(_at(w[1], w[2], w[3], w[4] / 2, w[5] / 2))
    )
    scenery = getattr(world, "scenery", None) or {}
    crowd = scenery.get("crowd", {})
    for wall in ordered:
        if app.hall is not None:
            _draw_hall_wall(img, cam, wall, app, depth_m)
            for prop in app.hall.props:
                _draw_prop(img, cam, wall, prop, app, depth_m, None)
        for person in crowd.get(wall[0], ()):
            _draw_person(img, cam, wall, person, depth_m)
    _draw_light(img, cam, world, app, depth_m)
    if app.net is not None:
        budget = NET_MAX_STRANDS
        for wall in ordered:
            budget = _draw_net(img, cam, wall, app.net, budget)
