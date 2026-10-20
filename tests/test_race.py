"""The race scorer, fed synthetic pose streams — no simulator, no hardware."""

import pytest

from comp2.sim.race import DNF, FINISHED, IDLE, RUNNING, RaceScorer
from comp2.sim.world import DEFAULT_GATE_OPENING_M

# The scored clear opening of the default ring, derived so these stay true if the
# measured gate changes again.
OPENING_R = DEFAULT_GATE_OPENING_M / 2


def scene(gates=None, finish=(1.5, 5.0), difficulty=1.0, t_ref=10.0):
    return {
        "gates": gates
        if gates is not None
        else [
            {
                "x": 1.5,
                "y": 2.0,
                "z": 1.5,
                "yaw_deg": 0.0,
                "diameter_m": 1.2,
                "opening_diameter_m": 1.0,
            }
        ],
        "finish": list(finish),
        "difficulty": difficulty,
        "t_ref_s": t_ref,
        "weights": {"smoothness": 0.4, "accuracy": 0.3, "time": 0.3},
    }


def pose(x, y, z, flying=True):
    return {"x": x, "y": y, "z": z, "heading": 0.0, "flying": flying}


def feed(scorer, points, dt=1 / 30, t0=0.0):
    """Feed a list of (x, y, z[, flying]) samples at a steady rate."""
    t = t0
    for p in points:
        flying = p[3] if len(p) > 3 else True
        scorer.sample(pose(p[0], p[1], p[2], flying), t)
        t += dt
    return t


def straight_line(y0, y1, x=1.5, z=1.5, n=60):
    return [(x, y0 + (y1 - y0) * i / (n - 1), z) for i in range(n)]


def test_idle_until_takeoff_then_running():
    s = RaceScorer(scene())
    assert s.state_name == IDLE
    s.sample(pose(1.5, 0.7, 0.0, flying=False), 0.0)
    assert s.state_name == IDLE
    s.sample(pose(1.5, 0.7, 0.5), 0.1)
    assert s.state_name == RUNNING
    assert s.state()["state"] == "running"


def test_clean_pass_through_the_centre_records_a_tiny_offset():
    s = RaceScorer(scene())
    feed(s, straight_line(0.5, 3.5))
    assert s.next_gate == 1
    assert len(s.results) == 1
    assert s.results[0]["offset_m"] == pytest.approx(0.0, abs=0.01)


def test_off_centre_pass_records_the_offset():
    s = RaceScorer(scene())
    feed(s, straight_line(0.5, 3.5, x=1.8))  # 0.3 m to the side, inside a 1.0 m opening
    assert len(s.results) == 1
    assert s.results[0]["offset_m"] == pytest.approx(0.3, abs=0.01)


def test_passing_outside_the_opening_is_not_a_pass():
    s = RaceScorer(scene())
    feed(s, straight_line(0.5, 3.5, x=2.1))  # 0.6 m off centre > 0.5 m radius
    assert s.results == []
    assert s.next_gate == 0


def test_the_opening_is_a_circle():
    """A diagonal pass is judged on its distance from the centre: 0.2 m across
    and 0.2 m up each fit a square 0.5 m opening, but together they are
    0.28 m out, past the ring's 0.25 m radius."""
    side = 0.8 * OPENING_R
    assert side < OPENING_R < side * 2**0.5
    gate = [{"x": 1.5, "y": 2.0, "z": 1.5, "yaw_deg": 0.0}]
    straight, diagonal = RaceScorer(scene(gates=gate)), RaceScorer(scene(gates=gate))
    feed(straight, straight_line(0.5, 3.5, x=1.5 + side))
    feed(diagonal, straight_line(0.5, 3.5, x=1.5 + side, z=1.5 + side))
    assert diagonal.results == []
    assert len(straight.results) == 1
    assert straight.results[0]["offset_m"] == pytest.approx(side, abs=0.01)


def test_accuracy_falls_to_zero_at_the_rim():
    gate = [{"x": 1.5, "y": 2.0, "z": 1.5, "yaw_deg": 0.0}]
    centre, wide = RaceScorer(scene(gates=gate)), RaceScorer(scene(gates=gate))
    feed(centre, straight_line(0.5, 3.5))
    halfway = OPENING_R / 2  # halfway to the band, in any direction
    feed(wide, straight_line(0.5, 3.5, z=1.5 + halfway))
    assert centre.score()["accuracy"] == pytest.approx(1.0, abs=0.01)
    assert wide.score()["accuracy"] == pytest.approx(0.5, abs=0.02)


def test_wrong_direction_crossing_is_ignored():
    s = RaceScorer(scene())
    feed(s, straight_line(3.5, 0.5))  # flying south through a north gate
    assert s.results == []


def test_gates_must_be_passed_in_order():
    gates = [
        {
            "x": 1.5,
            "y": 2.0,
            "z": 1.5,
            "yaw_deg": 0.0,
            "diameter_m": 1.2,
            "opening_diameter_m": 1.0,
        },
        {
            "x": 1.5,
            "y": 4.0,
            "z": 1.5,
            "yaw_deg": 0.0,
            "diameter_m": 1.2,
            "opening_diameter_m": 1.0,
        },
    ]
    s = RaceScorer(scene(gates=gates))
    # dodge gate 1 (offset far to the side at its plane), then through gate 2's spot
    path = [(2.6, y, 1.5) for y in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)]
    path += straight_line(3.2, 4.8)
    feed(s, path)
    assert [r["index"] for r in s.results] == []  # gate 2's crossing didn't count
    assert s.next_gate == 0


def test_fast_pass_between_samples_is_interpolated():
    s = RaceScorer(scene())
    # 1 m per sample: the gate plane is jumped over, never sampled on
    feed(s, [(1.5, 0.5, 1.5), (1.5, 1.5, 1.5), (1.5, 2.5, 1.5), (1.5, 3.5, 1.5)])
    assert len(s.results) == 1
    # crossing time interpolated between the straddling samples
    t1, t2 = 1 / 30, 2 / 30
    assert t1 < s.results[0]["time_s"] + 0 < t2 + 1e-9


def test_landing_on_the_finish_pad_after_all_gates_finishes():
    s = RaceScorer(scene())
    t = feed(s, straight_line(0.5, 4.9))
    s.sample(pose(1.5, 5.0, 0.0, flying=False), t)
    assert s.state_name == FINISHED
    st = s.state()
    assert st["state"] == "finished" and st["passed"] == 1
    assert st["score"]["total"] > 0


def test_landing_early_is_a_dnf_with_no_time_score():
    s = RaceScorer(scene())
    feed(s, straight_line(0.5, 1.0, n=10))  # never reached the gate
    s.sample(pose(1.5, 1.0, 0.0, flying=False), 1.0)
    assert s.state_name == DNF
    score = s.score()
    assert score["time"] == 0.0 and score["accuracy"] == 0.0


def test_landing_away_from_the_pad_is_a_dnf_even_with_all_gates():
    s = RaceScorer(scene(finish=(1.5, 5.0)))
    t = feed(s, straight_line(0.5, 3.0))
    s.sample(pose(1.5, 3.0, 0.0, flying=False), t)  # 2 m from the pad
    assert s.state_name == DNF


def test_smoother_flight_scores_higher():
    jerky = RaceScorer(scene())
    smooth = RaceScorer(scene())
    n, dt = 90, 1 / 30
    # same start/end, same duration: one glides, one stops and starts
    smooth_path = straight_line(0.5, 3.5, n=n)
    jerky_path = []
    for i in range(n):
        y = 0.5 + 3.0 * (i // 15) / 5  # move in abrupt steps
        jerky_path.append((1.5, min(y, 3.5), 1.5))
    feed(smooth, smooth_path, dt=dt)
    feed(jerky, jerky_path, dt=dt)
    assert smooth.score()["smoothness"] > jerky.score()["smoothness"]


def test_difficulty_multiplies_the_total():
    easy = RaceScorer(scene(difficulty=1.0))
    hard = RaceScorer(scene(difficulty=1.6))
    for s in (easy, hard):
        t = feed(s, straight_line(0.5, 4.9))
        s.sample(pose(1.5, 5.0, 0.0, flying=False), t)
    assert hard.score()["total"] == pytest.approx(easy.score()["total"] * 1.6, abs=0.2)


def test_angled_gate_uses_its_own_plane():
    gates = [
        {
            "x": 1.5,
            "y": 2.0,
            "z": 1.5,
            "yaw_deg": 90.0,
            "diameter_m": 1.2,
            "opening_diameter_m": 1.0,
        }
    ]
    s = RaceScorer(scene(gates=gates))
    # gate faces +x: fly east through it
    feed(s, [(0.5 + 0.1 * i, 2.0, 1.5) for i in range(30)])
    assert len(s.results) == 1
    assert s.results[0]["offset_m"] == pytest.approx(0.0, abs=0.01)


def test_state_message_shape():
    s = RaceScorer(scene())
    st = s.state()
    assert st["state"] == "idle" and st["score"] is None
    assert st["gates"] == [
        {"index": 0, "passed": False, "time_s": None, "offset_m": None}
    ]
    assert st["next_gate"] == 0 and st["total"] == 1
