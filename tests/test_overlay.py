"""Detection markers: validation in pixels, drawing in frame fractions."""

import numpy as np
import pytest

from comp2 import overlay


def test_pixels_become_fractions_of_the_analysed_frame():
    (marker,) = overlay.markers([[(320, 120), (640, 240)]], 640, 480)
    assert marker.points == ((0.5, 0.25), (1.0, 0.5))
    assert marker.label == ""


def test_a_labelled_outline_keeps_its_caption():
    (marker,) = overlay.markers([([(0, 0), (64, 48)], "gate 1.25 m")], 640, 480)
    assert marker.points == ((0.0, 0.0), (0.1, 0.1))
    assert marker.label == "gate 1.25 m"


def test_a_two_point_outline_is_not_read_as_a_labelled_pair():
    (marker,) = overlay.markers([[(0, 0), (640, 480)]], 640, 480)
    assert marker.points == ((0.0, 0.0), (1.0, 1.0)) and marker.label == ""


def test_numpy_corners_from_a_detector_are_accepted():
    corners = np.array([[0.0, 0.0], [320.0, 0.0], [320.0, 240.0], [0.0, 240.0]])
    (marker,) = overlay.markers([(corners, "gate")], 640, 480)
    assert marker.points == ((0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5))


def test_a_noisy_frame_is_clamped_rather_than_ending_the_flight():
    found = overlay.markers(
        [([(0, 0)] * (overlay.MAX_POINTS + 4), "x" * 200)]
        + [[(0, 0), (10, 10)]] * (overlay.MAX_MARKERS + 5),
        640,
        480,
    )
    assert len(found) == overlay.MAX_MARKERS
    assert len(found[0].points) == overlay.MAX_POINTS
    assert len(found[0].label) == overlay.MAX_LABEL_CHARS


def test_a_gate_leaving_the_frame_keeps_its_shape_but_cannot_run_away():
    (marker,) = overlay.markers([[(-320, -240), (640_000, 480_000)]], 640, 480)
    assert marker.points == ((-0.5, -0.5), (2.0, 2.0))


@pytest.mark.parametrize(
    "detections",
    [
        [[(10, 10)]],  # a single point is not an outline
        [[(10, 10, 10), (20, 20, 20)]],
        [[(float("nan"), 10), (20, 20)]],
        [[(10, float("inf")), (20, 20)]],
    ],
)
def test_a_malformed_outline_is_a_bug_in_the_caller_and_raises(detections):
    with pytest.raises(ValueError):
        overlay.markers(detections, 640, 480)


def test_nothing_detected_clears_the_overlay():
    assert overlay.markers([], 640, 480) == ()
    assert overlay.markers(None, 640, 480) == ()


def test_drawing_outlines_the_detection_on_the_display_copy():
    frame = np.zeros((480, 640, 3), np.uint8)
    found = overlay.markers(
        [([(160, 120), (480, 120), (480, 360), (160, 360)], "gate 1.00 m")], 640, 480
    )
    overlay.draw(frame, found)
    green = frame[:, :, 1]
    assert green.max() > 200
    assert green[120, 320] > 200  # the top edge of the box
    assert frame[240, 320].sum() == 0  # its inside is left alone
    assert green[:110].max() > 200  # the caption sits above the box


def test_drawing_survives_an_outline_that_leaves_the_frame():
    frame = np.zeros((480, 640, 3), np.uint8)
    found = overlay.markers([[(-200, -200), (900, 900)]], 640, 480)
    overlay.draw(frame, found)
    assert frame[:, :, 1].max() > 200


def test_wire_form_round_trips_through_json():
    import json

    found = overlay.markers([([(0, 0), (64, 48)], "gate")], 640, 480)
    wire = json.loads(json.dumps(overlay.to_wire(found)))
    assert overlay.markers([(w[0], w[1]) for w in wire], 1, 1) == found
