"""The track loader: published JSON layouts -> validated worlds."""

import json

import pytest
from pydantic import ValidationError

from comp2.sim import track
from comp2.sim.world import DEFAULT_GATE_DIAMETER_M, DEFAULT_GATE_OPENING_M


def spec(**overrides):
    base = {
        "name": "Test",
        "difficulty": 1.0,
        "arena": {"width_m": 4.0, "depth_m": 6.0, "height_m": 3.0},
        "takeoff": [1.5, 0.7],
        "landing": [1.5, 5.3],
        "gates": [{"x": 1.5, "y": 2.0, "z": 1.5}],
    }
    base.update(overrides)
    return base


def test_catalog_lists_the_shipped_tracks():
    cat = track.catalog()
    assert [t["id"] for t in cat] == [
        "circuit1_oval",
        "circuit2_figure_eight",
        "circuit3_double_figure_eight",
    ]
    assert [t["difficulty"] for t in cat] == [1.0, 1.2, 1.5]  # the brief's multipliers
    assert [t["gates"] for t in cat] == [3, 3, 3]  # each gate once, every circuit


def test_every_shipped_track_builds_a_valid_world():
    for name in track.names():
        world = track.build(name)
        assert world.gates
        assert world.difficulty > 0
        assert world.t_ref_s > 0


def test_shipped_tracks_use_the_published_arena_and_gate_coordinates():
    """The competition geometry, straight out of the technical specification."""
    published = [(1.0, 3.0, 2.0), (3.0, 1.0, 1.5), (5.0, 3.0, 1.0)]
    for name in track.names():
        world = track.build(name)
        assert (world.width_m, world.depth_m, world.height_m) == (6.0, 4.0, 3.0)
        assert world.start == world.finish == (3.0, 3.0)  # the Take off / Land pad
        assert world.start_heading_deg == 270.0  # facing Gate 1
        assert [(g.x, g.y, g.z) for g in world.gates] == published


def test_takeoff_heading_reaches_the_world():
    world = track.TrackConfig.model_validate(spec(takeoff_heading_deg=242.0)).build()
    assert world.start_heading_deg == 242.0


@pytest.mark.parametrize(
    "name, headings",
    [
        ("circuit1_oval", [270, 90, 270]),
        ("circuit2_figure_eight", [90, 90, 270]),
        ("circuit3_double_figure_eight", [90, 90, 90]),
    ],
)
def test_gate_directions_follow_the_pdf_arrows(name, headings):
    # The PDF is rotated relative to the app's normal plan: down the page
    # means +x (90 degrees); up the page means -x (270 degrees).
    assert [g.yaw_deg for g in track.build(name).gates] == headings


def test_build_unknown_track_raises():
    with pytest.raises(ValueError, match="unknown track"):
        track.build("nope")


def test_config_builds_a_world():
    world = track.TrackConfig.model_validate(spec()).build()
    assert world.width_m == 4.0 and world.depth_m == 6.0 and world.height_m == 3.0
    assert world.start == (1.5, 0.7) and world.finish == (1.5, 5.3)
    assert world.gates[0].z == 1.5 and (
        world.gates[0].diameter_m,
        world.gates[0].opening_diameter_m,
    ) == (DEFAULT_GATE_DIAMETER_M, DEFAULT_GATE_OPENING_M)


def test_an_opening_as_wide_as_the_ring_is_rejected():
    with pytest.raises(ValidationError, match="smaller than diameter_m"):
        track.TrackConfig.model_validate(
            spec(
                gates=[
                    {
                        "x": 1.5,
                        "y": 2.0,
                        "z": 1.5,
                        "diameter_m": 0.5,
                        "opening_diameter_m": 0.5,
                    }
                ]
            )
        )


def test_gate_outside_the_arena_is_rejected():
    with pytest.raises(ValidationError, match="does not fit the arena"):
        track.TrackConfig.model_validate(spec(gates=[{"x": 3.9, "y": 2.0, "z": 1.5}]))


def test_ring_bottom_below_the_floor_clearance_is_rejected():
    with pytest.raises(ValidationError, match="clearance"):
        track.TrackConfig.model_validate(
            spec(gates=[{"x": 1.5, "y": 2.0, "z": 0.6}])
        )  # bottom at 0.25 m


def test_frame_above_the_ceiling_is_rejected():
    with pytest.raises(ValidationError, match="ceiling"):
        track.TrackConfig.model_validate(spec(gates=[{"x": 1.5, "y": 2.0, "z": 3.8}]))


def test_pad_outside_the_arena_is_rejected():
    with pytest.raises(ValidationError, match="outside the arena"):
        track.TrackConfig.model_validate(spec(takeoff=[5.0, 0.7]))


def test_a_track_needs_at_least_one_gate():
    with pytest.raises(ValidationError):
        track.TrackConfig.model_validate(spec(gates=[]))


def test_load_file_reads_json(tmp_path):
    path = tmp_path / "custom.json"
    path.write_text(json.dumps(spec(name="Custom")), encoding="utf-8")
    assert track.load_file(path).name == "Custom"
    assert track.build("custom", directory=tmp_path).name == "Custom"
    assert track.catalog(directory=tmp_path)[0]["id"] == "custom"
