import numpy as np

from comp2.drone.mock import MockDrone


def test_mock_logs_commands():
    d = MockDrone()
    d.connect()
    d.takeoff()
    d.move("forward", 50)
    d.rotate("cw", 90)
    d.land()
    assert d.log == [
        ("connect",),
        ("takeoff",),
        ("move", "forward", 50),
        ("rotate", "cw", 90),
        ("land",),
    ]


def test_mock_frame_factory():
    red = np.zeros((480, 640, 3), np.uint8)
    red[:] = (0, 0, 255)
    d = MockDrone(frame_factory=lambda: red)
    assert d.get_frame()[0, 0, 2] == 255


def test_mock_logs_continuous_control():
    d = MockDrone()
    d.send_rc(0, 60, 0, -10)
    d.go_xyz(100, 0, 20, 50)
    d.curve_xyz(50, 50, 0, 100, 0, 0, 60)
    assert d.log == [
        ("send_rc", 0, 60, 0, -10),
        ("go_xyz", 100, 0, 20, 50),
        ("curve_xyz", 50, 50, 0, 100, 0, 0, 60),
    ]
