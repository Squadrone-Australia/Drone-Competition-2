import threading
import time

import pytest

from comp2.sim.drone import SimDrone
from comp2.sim.world import (
    DEFAULT_GATE_DIAMETER_M,
    DEFAULT_GATE_OPENING_M,
    Gate,
    World,
)


def empty_world():
    return World(
        width_m=4.0,
        depth_m=4.0,
        height_m=4.0,
        gates=[],
        start=(2.0, 2.0),
        finish=(2.0, 2.0),
    )


def drone():
    return SimDrone(world=empty_world(), delay=0)


def sample_while(d, run, interval=0.005):
    """Poll the pose from a second thread while ``run()`` flies the drone.

    This is how the video loop and the browser's 3D view see the drone: mid
    command, from another thread.
    """
    seen = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            seen.append((d.x, d.y, d.z, d.heading, d.roll, d.pitch))
            time.sleep(interval)

    t = threading.Thread(target=poll)
    t.start()
    try:
        run()
    finally:
        stop.set()
        t.join()
    return seen


def test_starts_landed_on_the_takeoff_pad():
    d = drone()
    assert (d.x, d.y, d.z, d.flying) == (2.0, 2.0, 0.0, False)


def test_default_track_loads_from_the_json_catalog():
    d = SimDrone(delay=0)
    assert d.world.name == "Circuit 1 - Oval"
    assert len(d.world.gates) == 3
    assert (d.world.width_m, d.world.depth_m, d.world.height_m) == (6.0, 4.0, 3.0)


def test_move_before_takeoff_raises():
    with pytest.raises(RuntimeError):
        drone().move("forward", 50)


def test_pose_math_rotate_then_move():
    d = drone()
    d.takeoff()
    d.rotate("cw", 90)  # now facing +x
    d.move("forward", 100)
    assert round(d.x, 2) == 3.0 and round(d.y, 2) == 2.0
    d.rotate("ccw", 90)  # back to +y
    d.move("left", 50)
    assert round(d.x, 2) == 2.5


def test_clamped_at_walls():
    d = drone()
    d.takeoff()
    d.move("forward", 500)
    assert d.y == 3.8  # 0.2 m margin


def test_altitude_respects_the_cage_ceiling():
    d = drone()
    d.takeoff()
    d.move("up", 500)
    assert d.z == pytest.approx(4.0 - 0.3)


def test_takeoff_land_altitude_and_frame_shape():
    d = drone()
    d.takeoff()
    assert d.z == 1.0 and d.flying
    assert d.get_frame().shape == (480, 640, 3)
    d.land()
    assert d.z == 0.0 and not d.flying


# --- animation ------------------------------------------------------------


def animated():
    return SimDrone(world=empty_world(), delay=0.1)


def test_a_move_is_flown_not_teleported():
    d = animated()
    d.takeoff()
    seen = sample_while(d, lambda: d.move("forward", 100))
    ys = sorted({round(s[1], 2) for s in seen})
    assert len(ys) > 3, f"only saw {ys} — the drone jumped"
    assert min(ys) >= 2.0 and max(ys) <= 3.0  # never overshoots the target


def test_animation_still_lands_on_the_exact_commanded_pose():
    """Control-logic tests read drone.x straight after the call, so the end of an
    animation must be the arithmetic result, not wherever the last frame fell."""
    d = animated()
    d.takeoff()
    d.rotate("cw", 90)
    d.move("forward", 100)
    assert (round(d.x, 6), round(d.y, 6), round(d.heading, 6)) == (3.0, 2.0, 90.0)


def test_translation_tips_the_airframe_and_levels_out_again():
    d = animated()
    d.takeoff()
    seen = sample_while(d, lambda: d.move("forward", 100))
    assert max(s[5] for s in seen) > 1.0  # pitched into the move
    assert d.pitch == 0.0 and d.roll == 0.0  # level once it stops


def test_emergency_cuts_an_animation_short():
    d = animated()
    d.takeoff()
    stopper = threading.Timer(0.05, d.emergency)
    stopper.start()
    d.move("forward", 300)  # ~1.8 s if left to finish
    stopper.join()
    assert not d.flying and d.z == 0.0
    assert d.y < 3.7, "kept flying to the wall after an emergency stop"


def test_zero_delay_keeps_tests_instant():
    d = drone()
    d.takeoff()
    t0 = time.perf_counter()
    d.move("forward", 100)
    d.rotate("cw", 180)
    d.flip("b")
    d.land()
    assert time.perf_counter() - t0 < 0.1


# --- go / curve -----------------------------------------------------------


def test_go_xyz_flies_a_body_frame_offset_in_one_line():
    d = drone()
    d.takeoff()
    d.rotate("cw", 90)  # facing +x: forward = +x, left = +y
    d.go_xyz(100, 50, 30, 100)
    assert (round(d.x, 2), round(d.y, 2), round(d.z, 2)) == (3.0, 2.5, 1.3)


def test_curve_xyz_ends_on_the_second_point():
    d = drone()
    d.takeoff()
    d.curve_xyz(50, 50, 0, 100, 0, 0, 100)
    assert (round(d.x, 2), round(d.y, 2)) == (2.0, 3.0)  # forward 100 at heading 0


# --- display feeds --------------------------------------------------------


def test_pose_reports_what_the_third_person_view_needs():
    d = drone()
    d.takeoff()
    d.rotate("cw", 45)
    p = d.pose()
    assert set(p) == {"x", "y", "z", "heading", "roll", "pitch", "flying"}
    assert p["flying"] and p["heading"] == 45.0 and p["z"] == 1.0


def test_scene_describes_the_track():
    world = World(
        width_m=4.0,
        depth_m=6.0,
        height_m=3.0,
        gates=[Gate(1.5, 2.0, 1.5, 0.0)],
        start=(1.5, 0.7),
        finish=(1.5, 5.3),
        name="test",
        difficulty=1.2,
    )
    s = SimDrone(world=world, delay=0).scene()
    assert (s["width_m"], s["depth_m"], s["height_m"]) == (4.0, 6.0, 3.0)
    assert s["gates"] == [
        {
            "x": 1.5,
            "y": 2.0,
            "z": 1.5,
            "yaw_deg": 0.0,
            "diameter_m": DEFAULT_GATE_DIAMETER_M,
            "opening_diameter_m": DEFAULT_GATE_OPENING_M,
        }
    ]
    assert s["start"] == [1.5, 0.7] and s["finish"] == [1.5, 5.3]
    assert s["difficulty"] == 1.2


def test_track_catalog_and_load_track():
    d = SimDrone(delay=0)
    ids = [t["id"] for t in d.track_catalog()]
    assert ids == [
        "circuit1_oval",
        "circuit2_figure_eight",
        "circuit3_double_figure_eight",
    ]
    d.load_track("circuit2_figure_eight")
    assert d.world.difficulty == 1.2
    assert (d.x, d.y) == d.world.start_xy  # back on the new pad


# --- reset ----------------------------------------------------------------


def test_reset_returns_the_drone_to_the_takeoff_pad():
    d = drone()
    d.takeoff()
    d.rotate("cw", 120)
    d.move("forward", 90)
    d.reset()
    assert (d.x, d.y, d.z, d.heading, d.flying) == (2.0, 2.0, 0.0, 0.0, False)
    assert d.roll == 0.0 and d.pitch == 0.0


def test_reset_recovers_from_an_emergency_stop():
    d = animated()
    d.takeoff()
    d.emergency()
    d.reset()
    d.takeoff()
    assert d.flying and d.z == 1.0  # _abort cleared, so the climb ran


def test_adapters_without_an_arena_expose_no_pose_or_track():
    """Live absolute position is a simulator-only concept."""
    from comp2.drone.mock import MockDrone

    assert MockDrone().pose() is None and MockDrone().scene() is None
    assert MockDrone().track_catalog() is None
