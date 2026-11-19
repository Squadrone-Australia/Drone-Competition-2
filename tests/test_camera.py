import json

import pytest

from comp2.vision.camera import (
    CALIBRATION_FILE,
    SIM_INTRINSICS,
    TELLO_INTRINSICS,
    CameraIntrinsics,
)

REPORT = json.loads(
    CALIBRATION_FILE.with_name("tello_camera.report.json").read_text(encoding="utf-8")
)


def test_hfov_round_trips():
    cam = CameraIntrinsics.from_hfov(70.0)
    assert cam.hfov_deg == pytest.approx(70.0)


def test_diagonal_fov_is_not_the_horizontal_fov():
    # the bug this module exists to prevent: DJI's 82.6 deg is diagonal, and
    # reading it as horizontal makes the camera far too wide
    cam = CameraIntrinsics.from_dfov(82.6, 4, 3)
    assert cam.hfov_deg == pytest.approx(70.1, abs=0.5)
    assert cam.vfov_deg(0.75) == pytest.approx(55.5, abs=0.5)


def test_the_tello_camera_is_the_calibrated_one():
    cam = TELLO_INTRINSICS
    assert cam.focal_px(960) == pytest.approx(REPORT["fx_px"])
    assert cam.cx_frac * 960 == pytest.approx(REPORT["cx_px"])
    assert cam.cy_frac * 720 == pytest.approx(REPORT["cy_px"])
    assert cam.hfov_deg == pytest.approx(REPORT["hfov_deg"], abs=0.1)
    assert cam.vfov_deg(0.75) == pytest.approx(REPORT["vfov_deg"], abs=0.1)
    # far narrower than DJI's nominal figure, which is what made ranges short
    assert cam.hfov_deg < CameraIntrinsics.from_dfov(82.6).hfov_deg - 10


def test_the_simulator_sees_through_the_same_camera():
    assert SIM_INTRINSICS == TELLO_INTRINSICS


def test_bearing_is_zero_at_the_principal_point_and_spans_the_fov():
    cam = TELLO_INTRINSICS
    assert cam.bearing_deg(cam.cx_frac) == pytest.approx(0.0)
    assert cam.bearing_deg(1.0) - cam.bearing_deg(0.0) == pytest.approx(cam.hfov_deg)
    # the lens sits left of centre, so the right edge is further off axis
    assert cam.bearing_deg(1.0) > -cam.bearing_deg(0.0)


def test_elevation_is_positive_above_the_principal_point():
    cam = TELLO_INTRINSICS
    assert cam.elevation_deg(0.2) > 0  # upper part of the image
    assert cam.elevation_deg(0.8) < 0
    assert cam.elevation_deg(cam.cy_frac) == pytest.approx(0.0)


@pytest.mark.parametrize("d", [0.5, 1.0, 2.0, 3.5, 6.0])
def test_distance_round_trips_through_apparent_radius(d):
    cam, R = TELLO_INTRINSICS, 0.125
    assert cam.distance_m(cam.radius_norm_at(d, R), R) == pytest.approx(d)


def test_distance_is_resolution_independent():
    cam, R, d = TELLO_INTRINSICS, 0.125, 2.5
    # same physical scene, two stream sizes: the pixel radii differ, the range doesn't
    r_norm = cam.radius_norm_at(d, R)
    for width in (640, 960, 1280):
        radius_px = r_norm * width
        assert cam.distance_m(radius_px / width, R) == pytest.approx(d)


def test_area_gate_can_be_derived_from_a_wanted_range():
    cam, R = TELLO_INTRINSICS, 0.175  # a larger, A3-ish marker
    gate = cam.min_area_ratio_for_range(R, 6.0)
    assert cam.max_range_m(R, gate) == pytest.approx(6.0)
