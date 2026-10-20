"""The cockpit's 3D view must show the room the camera is flying through.

The 3D view is a *display* — it draws arena-absolute coordinates no block or
`comp2.api` method can reach — but it is the operator's mental model of the
flight, so it showing a different arena from the video feed beside it is a real
defect rather than a cosmetic one. It used to: the camera flew through black
netting, a hall of furniture and a dark varnished floor while the 3D view drew
an empty blue glass room with bright orange gates.

Both now read one `appearance` block. These tests hold the two together:

* the wall table the browser uses is re-derived from the renderer's own, so
  neither side can move alone (the browser half is checked by
  ``tests/js/arena_layout.test.js`` against the same fixture);
* every field the browser reads is actually sent;
* the gate the view draws is the gate the camera sees, dimensions included.
"""

import json
import re
from pathlib import Path

import pytest

from comp2.server import FRONTEND_DIR
from comp2.sim import arena
from comp2.sim.drone import SimDrone
from comp2.sim.render import _at, _outward, _walls
from comp2.sim.world import (
    GATE_CHECKER_ROWS,
    GATE_ORANGE_HALF_ANGLE_DEG,
    GATE_TUBE_M,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wall_points.json"


def test_the_browsers_wall_fixture_still_matches_the_renderer():
    """Regenerates the fixture the JS test measures itself against.

    If the renderer's wall table is ever changed, this fails here rather than
    letting the browser quietly keep placing the hall where it used to be.
    """
    doc = json.loads(FIXTURE.read_text())
    a = doc["arena"]
    table = {
        name: (origin, u_axis, v_axis, span, height)
        for name, origin, u_axis, v_axis, span, height in _walls(
            a["width_m"], a["depth_m"], a["height_m"]
        )
    }
    assert doc["cases"], "the fixture is empty"
    for case in doc["cases"]:
        origin, u_axis, v_axis, span, _height = table[case["wall"]]
        assert span == pytest.approx(case["span"])
        point = _at(
            origin,
            u_axis,
            v_axis,
            case["u"],
            case["v"],
            case["out"],
            _outward(u_axis, v_axis),
        )
        assert list(point) == pytest.approx(case["point"], abs=1e-9)


def test_every_wall_is_covered_by_the_fixture():
    covered = {case["wall"] for case in json.loads(FIXTURE.read_text())["cases"]}
    assert covered == {name for name, *_rest in _walls(6.0, 4.0, 3.0)}


# --- the payload the browser reads ---------------------------------------


def scene_of(arena_name):
    return SimDrone(delay=0, arena_name=arena_name, seed=3).scene()


def test_scene_carries_the_appearance_the_3d_view_draws():
    scene = scene_of("cage")
    look = scene["appearance"]
    assert look["name"] == "cage"
    assert set(look) == {
        "name",
        "floor",
        "wall",
        "net",
        "hall",
        "light",
        "gate",
        "crowd",
    }
    assert set(look["floor"]) == {"colour", "grid", "gloss", "plank_m"}
    assert set(look["net"]) == {"pitch_m", "colour", "opacity"}
    assert set(look["hall"]) == {"depth_m", "wall_colour", "floor_colour", "props"}
    assert set(look["gate"]) == {
        "orange",
        "dark",
        "light",
        "print",
        "stand",
        "elbow",
        "string",
        "mount",
    }
    assert look["gate"]["mount"] == "stand"
    assert look["hall"]["props"], "the hall has no furniture to draw"
    assert set(look["hall"]["props"][0]) == {"kind", "u", "v", "w", "h", "colour"}


def test_colours_cross_the_wire_as_css_not_bgr():
    """three.js wants CSS hex; the renderer's BGR tuples stay in Python."""
    look = scene_of("cage")["appearance"]
    for colour in (
        look["floor"]["colour"],
        look["wall"]["colour"],
        look["net"]["colour"],
        look["gate"]["orange"],
        look["gate"]["dark"],
        look["hall"]["wall_colour"],
    ):
        assert re.fullmatch(r"#[0-9a-f]{6}", colour), colour


def test_the_channel_order_is_not_swapped():
    """A BGR tuple sent straight through would turn the wood floor blue."""
    look = scene_of("cage")["appearance"]
    red, green, blue = (int(look["floor"]["colour"][i : i + 2], 16) for i in (1, 3, 5))
    # The measured varnished floor is BGR (73, 80, 110): a warm brown, so red
    # must dominate. Serialised without swapping it would read as blue.
    assert red > blue, "the varnished floor came out cold"
    assert (red, green, blue) == (110, 80, 73)


def test_the_spectators_reach_the_browser():
    look = scene_of("cage")["appearance"]
    assert look["crowd"], "nobody is watching"
    person = look["crowd"][0]
    assert set(person) == {
        "wall",
        "u",
        "stature",
        "shirt",
        "trousers",
        "skin",
        "standoff",
    }
    assert 1.5 <= person["stature"] <= 2.0


def test_a_clean_arena_says_so_rather_than_sending_nothing():
    """The view falls back to its glass room, so the absence must be explicit."""
    look = scene_of("clean")["appearance"]
    assert look["net"] is None and look["hall"] is None and look["light"] is None
    assert look["floor"]["grid"] is True
    assert look["crowd"] == []


# --- the gate the view draws ----------------------------------------------


def test_the_view_is_told_the_gates_own_numbers():
    """The 3D view used to carry its own constants, so it kept drawing the old
    rig after the measured one was corrected."""
    scene = scene_of("cage")
    assert scene["tube_m"] == GATE_TUBE_M == 0.02
    assert scene["ring"] == {
        "orange_half_angle_deg": GATE_ORANGE_HALF_ANGLE_DEG,
        "checker_rows": GATE_CHECKER_ROWS,
    }
    gate = scene["gates"][0]
    assert (gate["diameter_m"], gate["opening_diameter_m"]) == (0.70, 0.50)


def test_the_3d_view_takes_the_gate_from_the_scene_not_a_constant():
    source = (FRONTEND_DIR / "scene3d.js").read_text(encoding="utf-8")
    assert "desc.tube_m" in source, "the view is not reading the sent diameter"
    assert "desc.ring" in source, "the view is not reading the band layout"
    # The constants may survive only as fallbacks for a payload without them.
    assert "tubeM ?? GATE_TUBE_M" in source
    assert "g.diameter_m ?? GATE_DIAMETER_M" in source


def test_the_hall_lit_ring_is_not_drawn_as_competition_orange():
    """The camera sees fabric dulled by the hall's lights, not brand orange."""
    cage = arena.CAGE.gate.orange_bgr
    assert cage != arena.CLEAN.gate.orange_bgr
    look = scene_of("cage")["appearance"]["gate"]
    assert look["orange"] != "#f7941d"


@pytest.mark.parametrize("name", ["cage", "clean"])
def test_the_browsers_scene_fixture_is_still_what_the_server_sends(name):
    """``tests/js/scene3d.test.js`` builds the 3D arena from these payloads.

    A stale fixture would let the browser test keep passing against a message
    shape the server no longer sends, which is the one thing it exists to catch.
    """
    fixture = Path(__file__).parent / "fixtures" / f"scene_{name}.json"
    assert json.loads(fixture.read_text()) == scene_of(name), (
        f"tests/fixtures/scene_{name}.json is out of date with scene()"
    )


def test_a_hanging_arena_tells_the_3d_view_to_draw_cords_not_a_stand():
    scene = SimDrone(delay=0, arena_name="cage", seed=3)
    scene.world.appearance = arena.compose("hung", mount="hanging")
    doc = scene.scene()
    assert doc["appearance"]["gate"]["mount"] == "hanging"
    assert doc["appearance"]["gate"]["string"] == "#f8f8f8"
    assert doc["string_m"] > 0
