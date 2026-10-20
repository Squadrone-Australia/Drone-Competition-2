import numpy as np

from .base import DroneAdapter


class MockDrone(DroneAdapter):
    def __init__(self, frame_factory=None):
        self.log = []
        self._frame_factory = frame_factory or (
            lambda: np.zeros((480, 640, 3), np.uint8)
        )

    def connect(self):
        self.log.append(("connect",))

    def takeoff(self):
        self.log.append(("takeoff",))

    def land(self):
        self.log.append(("land",))

    def emergency(self):
        self.log.append(("emergency",))

    def move(self, direction, cm):
        self.log.append(("move", direction, cm))

    def rotate(self, direction, deg):
        self.log.append(("rotate", direction, deg))

    def flip(self, direction):
        self.log.append(("flip", direction))

    def send_rc(self, lr, fb, ud, yaw):
        self.log.append(("send_rc", lr, fb, ud, yaw))

    def go_xyz(self, x_cm, y_cm, z_cm, speed_cms):
        self.log.append(("go_xyz", x_cm, y_cm, z_cm, speed_cms))

    def curve_xyz(self, x1, y1, z1, x2, y2, z2, speed_cms):
        self.log.append(("curve_xyz", x1, y1, z1, x2, y2, z2, speed_cms))

    def get_frame(self):
        return self._frame_factory()

    def battery(self):
        return 100
