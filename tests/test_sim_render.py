"""The simulated camera view: gates on a neutral cage, honestly projected."""

import math

import numpy as np

from comp2.sim import arena
from comp2.sim import render as renderer
from comp2.sim.render import (
    CHECK_DARK_BGR,
    CHECK_LIGHT_BGR,
    INTRINSICS,
    RING_ORANGE_BGR,
    STAND_BGR,
    draw_minimap,
    render,
)
from comp2.sim.scene import Camera
from comp2.sim.world import GATE_MAST_M, Gate, World


def world_with_gate(x=1.5, y=3.0, z=1.5, yaw=0.0):
    return World(
        width_m=4.0,
        depth_m=6.0,
        height_m=3.0,
        gates=[Gate(x, y, z, yaw)],
        start=(1.5, 0.7),
        finish=(1.5, 5.3),
    )


def empty_cage():
    return World(
        width_m=4.0,
        depth_m=6.0,
        height_m=3.0,
        gates=[],
        start=(1.5, 0.7),
        finish=(1.5, 5.3),
    )


def is_colour(img, bgr):
    return np.all(img == np.array(bgr, np.uint8), axis=-1)


def frame_pixels(img):
    """How many pixels are the ring's exact orange."""
    return int(is_colour(img, RING_ORANGE_BGR).sum())


def test_a_gate_ahead_is_visible():
    img = render(world_with_gate(), 1.5, 1.0, 1.5, 0.0)
    assert frame_pixels(img) > 100


def test_gate_behind_is_not_drawn():
    img = render(
        world_with_gate(y=0.9), 1.5, 3.0, 1.5, 0.0
    )  # gate behind, looking north
    assert frame_pixels(img) == 0


def test_gate_opening_shows_the_room_without_a_synthetic_number():
    gate = render(world_with_gate(), 1.5, 1.0, 1.5, 0.0)
    room = render(empty_cage(), 1.5, 1.0, 1.5, 0.0)
    assert np.array_equal(gate[235:245, 315:325], room[235:245, 315:325])


def test_apparent_size_grows_as_the_drone_nears():
    far = render(world_with_gate(y=5.0), 1.5, 0.7, 1.5, 0.0)
    near = render(world_with_gate(y=5.0), 1.5, 4.0, 1.5, 0.0)
    assert frame_pixels(near) > frame_pixels(far) > 0


def test_an_edge_on_gate_is_thinner_than_a_face_on_gate():
    face_on = render(world_with_gate(yaw=0.0), 1.5, 1.0, 1.5, 0.0)
    edge_on = render(world_with_gate(yaw=90.0), 1.5, 1.0, 1.5, 0.0)
    assert frame_pixels(edge_on) < frame_pixels(face_on)


def rig_view(z=1.2):
    """A gate and the camera far enough back that the whole rig is in shot."""
    world = world_with_gate(z=z)
    eye = (1.5, 0.0, z)
    img = render(world, *eye, 0.0)
    cam = Camera.at(INTRINSICS, *eye, 0.0, 640, 480)
    stand = is_colour(img, STAND_BGR)
    return world, img, stand, cam


def row_of(cam, x, y, z):
    """Which image row a world point lands on."""
    return round(float(cam.project(cam.to_camera([(x, y, z)]))[0][1]))


def pixel_of(cam, x, y, z):
    """Which (column, row) a world point lands on."""
    u, v = cam.project(cam.to_camera([(x, y, z)]))[0]
    return round(float(u)), round(float(v))


def test_a_gate_is_carried_from_below_and_hangs_from_nothing():
    world, img, _orange, cam = rig_view()
    gate = world.gates[0]
    top = row_of(cam, gate.x, gate.y, gate.z + gate.radius_m)
    room = render(empty_cage(), 1.5, 0.0, gate.z, 0.0)
    above = slice(0, top - 2)
    assert np.array_equal(img[above], room[above]), "something hangs above the gate"


def test_the_stand_runs_from_the_ring_down_to_the_floor():
    world, _img, stand, cam = rig_view()
    gate = world.gates[0]
    assert gate.stand_crossbar_z == gate.z - gate.radius_m - GATE_MAST_M
    for part, z in (
        ("mast", gate.stand_crossbar_z + GATE_MAST_M / 2),
        ("crossbar", gate.stand_crossbar_z),
        ("legs", gate.stand_crossbar_z / 2),
        ("feet", 0.0),
    ):
        row = row_of(cam, gate.x, gate.y, z)
        assert stand[row - 2 : row + 3].any(), f"no tube at the {part}"


def test_the_stand_is_not_orange():
    """Only the ring's own segments carry the orange a detector keys on: below
    the band's lowest point there is stand and floor, nothing orange."""
    world, img, _stand, cam = rig_view()
    gate = world.gates[0]
    bottom = row_of(cam, gate.x, gate.y, gate.z - gate.radius_m)
    assert is_colour(img[bottom - 12 : bottom], RING_ORANGE_BGR).any(), "no orange"
    assert not is_colour(img[bottom + 3 :], RING_ORANGE_BGR).any()


def band_patch(img, cam, gate, deg, half=4):
    """Pixels around the band's mid-radius at `deg` clockwise from 12 o'clock,
    seen from the approach side of a gate facing +y."""
    r = (gate.radius_m + gate.opening_radius_m) / 2
    a = math.radians(deg)
    u, v = pixel_of(cam, gate.x + r * math.sin(a), gate.y, gate.z + r * math.cos(a))
    return img[v - half : v + half + 1, u - half : u + half + 1]


def face_on():
    world = world_with_gate()
    eye = (1.5, 1.8, 1.5)
    cam = Camera.at(INTRINSICS, *eye, 0.0, 640, 480)
    return world.gates[0], render(world, *eye, 0.0), cam


def test_the_band_is_orange_top_and_bottom_and_checkered_either_side():
    gate, img, cam = face_on()
    for deg in (37, 143, 217, 323):  # clear of the print, inside the segment
        patch = band_patch(img, cam, gate, deg)
        assert is_colour(patch, RING_ORANGE_BGR).any(), f"no orange at {deg} deg"
    for deg in (60, 90, 120, 240, 270, 300):
        patch = band_patch(img, cam, gate, deg, half=8)
        assert not is_colour(patch, RING_ORANGE_BGR).any(), f"orange at {deg} deg"
        assert is_colour(patch, CHECK_DARK_BGR).any(), f"no dark square at {deg} deg"
        assert is_colour(patch, CHECK_LIGHT_BGR).any(), f"no light square at {deg} deg"


def test_the_orange_carries_white_print():
    """The web address over the top and the name under the bottom, as on the
    real fabric — a detector keyed on solid orange meets the lettering."""
    gate, img, cam = face_on()
    for deg in (0, 180):
        patch = band_patch(img, cam, gate, deg, half=10)
        white = np.all(patch > 230, axis=-1)
        assert white.any(), f"no print at {deg} deg"


def test_a_ring_straddling_the_camera_is_drawn_with_polygons():
    """Part of the ring behind the camera has no homography; the polygon
    fallback clips it at the near plane instead of dropping the gate."""
    world = world_with_gate(y=3.0)
    img = render(world, 1.35, 2.9, 1.5, 45.0)
    cam = Camera.at(INTRINSICS, 1.35, 2.9, 1.5, 45.0, 640, 480)
    gate = world.gates[0]
    corners = cam.to_camera(
        [(gate.x + u, gate.y, gate.z + v) for u, v in ((-0.35, 0.35), (0.35, 0.35))]
    )
    assert corners[0][2] < 0 < corners[1][2], "the view does not straddle the ring"
    # what is in view is the right-hand checker, close up
    checker = is_colour(img, CHECK_DARK_BGR) | is_colour(img, CHECK_LIGHT_BGR)
    assert checker.sum() > 1000


def test_the_polygon_fallback_covers_what_the_texture_does():
    gate, _img, cam = face_on()
    pal = renderer._palette(renderer.CLEAN)
    warped = np.zeros((480, 640, 3), np.uint8)
    polygons = np.zeros((480, 640, 3), np.uint8)
    renderer._draw_ring(warped, cam, gate, pal)
    renderer._draw_ring_sectors(polygons, cam, gate, pal)
    a, b = warped.any(axis=-1), polygons.any(axis=-1)
    assert (a ^ b).sum() < 0.05 * a.sum()


def test_the_room_is_actually_drawn():
    """Guards against the projection silently clipping everything away."""
    img = render(empty_cage(), 1.5, 3.0, 1.0, 20.0)
    assert len(np.unique(img.reshape(-1, 3), axis=0)) > 4


def test_the_view_changes_as_the_drone_moves_with_no_gate_in_sight():
    """Perspective, not paint: forward motion must be visible on bare scenery."""
    near_wall = render(empty_cage(), 1.5, 5.0, 1.0, 0.0)
    mid_room = render(empty_cage(), 1.5, 1.0, 1.0, 0.0)
    assert not np.array_equal(near_wall, mid_room)


def test_scenery_never_contains_the_frame_colour():
    """Teams detect the gates with their own OpenCV: the cage must not paint
    frame-coloured pixels of its own."""
    world = empty_cage()
    for x in (0.3, 1.5, 2.7):
        for y in (0.5, 3.0, 5.5):
            for heading in range(0, 360, 45):
                img = render(world, x, y, 1.2, heading)
                assert frame_pixels(img) == 0, (
                    f"scenery frame-coloured at {x},{y} hdg {heading}"
                )


def test_the_sensor_frame_has_no_minimap():
    """render() is the sensor; the minimap goes on the display copy only."""
    world = world_with_gate()
    sensor = render(world, 1.5, 1.0, 1.5, 0.0)
    display = draw_minimap(sensor, world, 1.5, 1.0, 0.0)
    assert not np.array_equal(sensor, display)  # the overlay exists
    assert sensor is not display  # and was not drawn in place


def test_minimap_draws_gates_and_pads():
    world = world_with_gate()
    blank = np.zeros((480, 640, 3), np.uint8)
    out = draw_minimap(blank, world, 1.5, 1.0, 0.0)
    assert not np.array_equal(out, blank)


# --- hanging mount --------------------------------------------------------


def hung_world(z=1.2):
    world = world_with_gate(z=z)
    world.appearance = arena.compose("hung", mount="hanging")
    return world


def test_a_hanging_gate_has_cords_to_the_ceiling_and_no_stand():
    world = hung_world()
    eye = (1.5, 0.0, 1.2)
    img = render(world, *eye, 0.0)
    cam = Camera.at(INTRINSICS, *eye, 0.0, 640, 480)
    gate = world.gates[0]
    assert not is_colour(img, STAND_BGR).any(), "a hanging gate still has a stand"
    r = gate.radius_m
    for side in (-1, 1):
        # a point on the cord, a little above where it meets the band
        u, v = pixel_of(
            cam,
            gate.x + side * r * math.sin(math.radians(30)),
            gate.y,
            gate.z + r * math.cos(math.radians(30)) + 0.25,
        )
        patch = img[v - 1 : v + 2, u - 1 : u + 2]
        # a one-pixel anti-aliased line: near-white against a mid-grey wall
        assert (patch.min(axis=-1) > 235).any(), (
            f"no cord above the {'right' if side > 0 else 'left'} of the ring"
        )


def test_a_standing_gate_has_no_cords():
    world = world_with_gate(z=1.2)
    world.appearance = arena.compose("stood", mount="stand")
    img = render(world, 1.5, 0.0, 1.2, 0.0)
    gate = world.gates[0]
    cam = Camera.at(INTRINSICS, 1.5, 0.0, 1.2, 0.0, 640, 480)
    r = gate.radius_m
    u, v = pixel_of(cam, gate.x - r / 2, gate.y, gate.z + r * 0.87 + 0.25)
    assert not (img[v - 1 : v + 2, u - 1 : u + 2].min(axis=-1) > 235).any()
    assert is_colour(img, STAND_BGR).any()


def test_mount_is_a_menu_entry_the_cockpit_can_pick_and_randomise():
    assert set(arena.catalog()["libraries"]["mount"]) == {"stand", "hanging"}
    assert arena.CAGE.mount == "stand"
    seen = {arena.randomize(seed).mount for seed in range(20)}
    assert seen == {"stand", "hanging"}
    try:
        arena.compose(mount="floating")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown mount was accepted")
