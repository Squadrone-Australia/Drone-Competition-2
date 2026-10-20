"""The Python pathway: Drone, Session, ScriptRun."""

import threading
import time

import numpy as np
import pytest

from comp2.api import (
    Drone,
    EmergencyStop,
    ScriptRun,
    Session,
    bind_session,
    clear_session,
    current_session,
)
from comp2.drone.mock import MockDrone


@pytest.fixture(autouse=True)
def clean_session():
    clear_session()
    yield
    clear_session()


def bound(drone=None):
    s = Session(drone=drone or MockDrone())
    bind_session(s)
    return s


# --- construction -----------------------------------------------------------


def test_drone_picks_up_the_bound_session():
    s = bound()
    d = Drone()
    assert d._d is s.drone


def test_drone_standalone_uses_a_mock_without_a_session():
    d = Drone()
    d.takeoff()
    assert ("takeoff",) in d._d.log


def test_drone_standalone_sim_flies():
    d = Drone(sim=True)
    d.takeoff()
    d.forward(50)
    assert d.height > 0


# --- discrete flight --------------------------------------------------------


def test_moves_and_turns_reach_the_adapter():
    s = bound()
    d = Drone()
    d.takeoff()
    d.forward(50)
    d.turn_right(90)
    d.left(30)
    d.land()
    assert s.drone.log == [
        ("takeoff",),
        ("move", "forward", 50),
        ("rotate", "cw", 90),
        ("move", "left", 30),
        ("land",),
    ]


def test_out_of_range_moves_clamp_with_a_warning():
    s = bound()
    warnings = []
    s.emit = warnings.append
    d = Drone()
    d.takeoff()
    d.forward(5)  # under the 20 cm floor
    d.turn_left(720)
    assert ("move", "forward", 20) in s.drone.log
    assert ("rotate", "ccw", 360) in s.drone.log
    assert len(warnings) == 2


# --- racing primitives ------------------------------------------------------


def test_fly_sends_the_rc_setpoint():
    s = bound()
    d = Drone()
    d.fly(lr=10, fb=60, ud=-20, yaw=5)
    assert ("send_rc", 10, 60, -20, 5) in s.drone.log


def test_fly_with_seconds_auto_hovers():
    s = bound()
    d = Drone()
    d.fly(fb=80, seconds=0.01)
    assert s.drone.log[-1] == ("send_rc", 0, 0, 0, 0)


def test_fly_clamps_the_sticks():
    s = bound()
    d = Drone()
    d.fly(fb=250)
    assert ("send_rc", 0, 100, 0, 0) in s.drone.log


def test_hover_centres_the_sticks():
    s = bound()
    Drone().hover()
    assert s.drone.log == [("send_rc", 0, 0, 0, 0)]


def test_go_snaps_sub_minimum_axes_to_zero():
    s = bound()
    d = Drone()
    d.go(100, 10, 0, speed=60)  # y=10 is under the 20 cm floor
    assert ("go_xyz", 100, 0, 0, 60) in s.drone.log


def test_curve_reaches_the_adapter():
    s = bound()
    Drone().curve(50, 50, 0, 100, 0, 0, speed=80)
    assert ("curve_xyz", 50, 50, 0, 100, 0, 0, 80) in s.drone.log


def test_frame_returns_the_session_frame():
    frame = np.zeros((480, 640, 3), np.uint8)
    s = Session(drone=MockDrone(), get_frame=lambda: frame)
    bind_session(s)
    assert Drone().frame() is frame


def test_show_detections_publishes_outlines_in_frame_fractions():
    shown = []
    s = Session(
        drone=MockDrone(),
        get_frame=lambda: np.zeros((480, 640, 3), np.uint8),
        show_overlay=shown.append,
    )
    bind_session(s)
    Drone().show_detections([([(160, 120), (480, 360)], "gate 1.10 m")])
    (marker,) = shown[0]
    assert marker.points == ((0.25, 0.25), (0.75, 0.75))
    assert marker.label == "gate 1.10 m"


def test_show_detections_measures_against_the_image_the_team_analysed():
    shown = []
    s = Session(
        drone=MockDrone(),
        get_frame=lambda: np.zeros((480, 640, 3), np.uint8),
        show_overlay=shown.append,
    )
    bind_session(s)
    Drone().show_detections([[(80, 60), (240, 180)]], size=(240, 320))
    assert shown[0][0].points == ((0.25, 0.25), (0.75, 0.75))


def test_show_detections_before_the_stream_starts_draws_nothing():
    shown = []
    s = Session(drone=MockDrone(), show_overlay=shown.append)
    bind_session(s)
    Drone().show_detections([[(0, 0), (10, 10)]])
    assert shown == []


def test_showing_nothing_clears_the_overlay():
    shown = []
    s = Session(
        drone=MockDrone(),
        get_frame=lambda: np.zeros((480, 640, 3), np.uint8),
        show_overlay=shown.append,
    )
    bind_session(s)
    Drone().show_detections(())
    assert shown == [()]


# --- emergency stop ---------------------------------------------------------


def test_every_call_checks_the_stop_flag():
    s = bound()
    d = Drone()
    s.stop.set()
    for call in (
        d.takeoff,
        d.hover,
        lambda: d.forward(50),
        lambda: d.fly(fb=50),
        lambda: d.go(100),
        d.frame,
        lambda: d.show_detections(()),
    ):
        with pytest.raises(EmergencyStop):
            call()
    assert s.drone.log == []  # nothing reached the adapter


def test_wait_is_cut_short_by_the_stop_flag():
    s = bound()
    d = Drone()
    threading.Timer(0.05, s.stop.set).start()
    t0 = time.perf_counter()
    with pytest.raises(EmergencyStop):
        d.wait(5)
    assert time.perf_counter() - t0 < 2


def test_emergency_stop_is_not_swallowed_by_except_exception():
    s = bound()
    d = Drone()
    s.stop.set()
    with pytest.raises(EmergencyStop):
        try:
            d.takeoff()
        except Exception:  # a script author's broad except
            pass


# --- ScriptRun --------------------------------------------------------------


async def test_scriptrun_runs_a_callable_and_reports_done():
    drone = MockDrone()
    events = []
    run = ScriptRun(drone, events.append)

    def script():
        d = Drone()
        d.takeoff()
        d.land()

    reason, _ = await run.run(script)
    assert reason == "done"
    assert ("takeoff",) in drone.log and ("land",) in drone.log
    assert events[-1]["type"] == "finished"


async def test_scriptrun_reports_errors_with_the_exception():
    run = ScriptRun(MockDrone(), lambda ev: None)

    def script():
        raise ValueError("boom")

    reason, detail = await run.run(script)
    assert reason == "error" and "boom" in detail


async def test_scriptrun_stop_lands_and_zeroes_the_sticks():
    drone = MockDrone()
    run = ScriptRun(drone, lambda ev: None)
    started = threading.Event()

    def script():
        d = Drone()
        d.takeoff()
        d.fly(fb=80)  # setpoint left applied
        started.set()
        while True:
            d.wait(0.05)  # consults the stop flag

    import asyncio

    task = asyncio.get_running_loop().create_task(run.run(script))
    await asyncio.to_thread(started.wait, 2)
    run.request_stop()
    reason, _ = await task
    assert reason == "stopped"
    assert ("send_rc", 0, 0, 0, 0) in drone.log  # teardown centred the sticks
    assert ("land",) in drone.log


async def test_scriptrun_session_is_cleared_after_the_run():
    run = ScriptRun(MockDrone(), lambda ev: None)
    await run.run(lambda: None)
    assert current_session() is None


# --- ScriptRun from a file on disk ------------------------------------------


def write_script(tmp_path, source, name="lap.py"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


async def test_scriptrun_flies_a_script_file(tmp_path):
    drone = MockDrone()
    path = write_script(
        tmp_path,
        "from comp2.api import Drone\n"
        "d = Drone()\n"
        "d.takeoff()\n"
        "d.forward(50)\n"
        "d.land()\n",
    )
    run = ScriptRun(drone, lambda ev: None, path=path)
    reason, detail = await run.run()
    assert (reason, detail) == ("done", "")
    assert ("takeoff",) in drone.log and ("move", "forward", 50) in drone.log


async def test_script_output_is_mirrored_to_the_browser(tmp_path):
    events = []
    path = write_script(tmp_path, "print('battery ok')\n")
    await ScriptRun(MockDrone(), events.append, path=path).run()
    text = "".join(
        e["text"] for e in events if e["type"] == "output" and e["stream"] == "stdout"
    )
    assert "battery ok" in text


async def test_script_traceback_names_the_authors_line_not_ours(tmp_path):
    events = []
    path = write_script(tmp_path, "x = 1\ny = x / 0\n")
    reason, detail = await ScriptRun(MockDrone(), events.append, path=path).run()
    assert reason == "error" and "ZeroDivisionError" in detail
    trace = "".join(
        e["text"] for e in events if e["type"] == "output" and e["stream"] == "stderr"
    )
    assert "lap.py" in trace and "line 2" in trace
    assert "y = x / 0" in trace
    assert "runpy" not in trace and "_worker" not in trace


async def test_script_syntax_error_is_an_error_not_a_crash(tmp_path):
    path = write_script(tmp_path, "def (:\n")
    reason, detail = await ScriptRun(MockDrone(), lambda ev: None, path=path).run()
    assert reason == "error" and "SyntaxError" in detail


async def test_scriptrun_without_a_path_says_so():
    run = ScriptRun(MockDrone(), lambda ev: None)
    reason, detail = await run.run()
    assert reason == "error" and "needs a path" in detail


async def test_scriptrun_abandons_a_loop_that_ignores_the_stop_flag():
    run = ScriptRun(MockDrone(), lambda ev: None, grace_s=0.1)
    started = threading.Event()
    keep_going = threading.Event()
    keep_going.set()

    def stubborn():
        started.set()
        while keep_going.is_set():  # never touches the drone
            time.sleep(0.01)

    import asyncio

    task = asyncio.get_running_loop().create_task(run.run(stubborn))
    await asyncio.to_thread(started.wait, 2)
    run.request_stop()
    reason, detail = await task
    keep_going.clear()
    assert reason == "abandoned"
    assert "did not stop" in detail


@pytest.mark.parametrize("exit_early", [False, True])
async def test_successful_script_retires_its_rc_owner(exit_early):
    drone = MockDrone()
    run = ScriptRun(drone, lambda ev: None)
    captured = []

    def script():
        d = Drone()
        captured.append(d)
        d.takeoff()
        d.fly(fb=80)
        if exit_early:
            raise SystemExit(0)

    reason, _ = await run.run(script)
    assert reason == "done"
    assert drone.log[-2:] == [("send_rc", 0, 0, 0, 0), ("land",)]
    with pytest.raises(EmergencyStop):
        captured[0].fly(fb=80)
