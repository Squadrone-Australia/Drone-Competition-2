"""examples/03_pilot.py: the parts that do not need a window or a drone."""

import importlib.util
from pathlib import Path

import cv2
import numpy as np
import pytest

pytest.importorskip("tkinter")

PILOT = Path(__file__).resolve().parents[1] / "examples" / "03_pilot.py"


@pytest.fixture(scope="module")
def pilot():
    spec = importlib.util.spec_from_file_location("pilot_example", PILOT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_held_keys_become_fly_arguments(pilot):
    assert pilot.stick_vector((), 40, 50) == {"lr": 0, "fb": 0, "ud": 0, "yaw": 0}
    assert pilot.stick_vector({"w", "d"}, 40, 50) == {
        "lr": 40,
        "fb": 40,
        "ud": 0,
        "yaw": 0,
    }
    assert pilot.stick_vector({"q", "f"}, 40, 50) == {
        "lr": 0,
        "fb": 0,
        "ud": -40,
        "yaw": -50,
    }
    # opposite keys cancel rather than one winning
    assert pilot.stick_vector({"w", "s"}, 40, 50)["fb"] == 0


def test_a_recording_plays_back_in_real_time(pilot, tmp_path):
    """Frames arrive unevenly; the file still lasts as long as the flight."""
    recorder = pilot.Recorder(tmp_path)
    path = recorder.start()
    frame = np.zeros((480, 640, 3), np.uint8)
    for t in (10.0, 10.04, 10.5, 11.0, 12.0):  # a stall between 11 and 12 s
        recorder.write(frame, t)
    assert recorder.stop() == path
    video = cv2.VideoCapture(str(path))
    frames = int(video.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = video.get(cv2.CAP_PROP_FPS)
    video.release()
    assert abs(frames / fps - 2.0) < 0.1


def test_a_recording_with_no_frames_leaves_nothing(pilot, tmp_path):
    recorder = pilot.Recorder(tmp_path)
    recorder.start()
    assert recorder.stop() is None
    assert not recorder.recording
