"""Camera geometry — the single source of truth for turning pixels into metres.

Everything here is expressed in *normalised* units (fractions of the frame width
or height) so the same constants describe the 960x720 Tello stream and the
640x480 simulator frame without rescaling. The number that matters most is
``f_norm`` — the pinhole focal length divided by the frame width.

The Tello's numbers come from ``tello_camera.json`` beside this file: a ChArUco
calibration of a real Tello at 960x720 (48 views, 0.28 px RMS reprojection
error; ``tello_camera.report.json`` has the details).
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path

CALIBRATION_FILE = Path(__file__).with_name("tello_camera.json")

# DJI publish a single "FOV: 82.6" figure for the Tello camera, and it is the
# *diagonal*. It is also too wide: the calibrated camera's diagonal is about
# 65 degrees, and its horizontal about 54. Kept only for comparison.
TELLO_DFOV_DEG = 82.6


@dataclass(frozen=True)
class CameraIntrinsics:
    f_norm: float  # focal length in pixels / frame width
    # Principal point, where the optical axis meets the image: a fraction of
    # the frame width across, and of its height down. A lens is rarely
    # mounted dead centre.
    cx_frac: float = 0.5
    cy_frac: float = 0.5

    @classmethod
    def from_calibration(cls, path: Path = CALIBRATION_FILE) -> "CameraIntrinsics":
        """A camera from a calibration file in ``tello_camera.json``'s format."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            f_norm=float(data["focal_per_width"]),
            cx_frac=float(data["cx_fraction"]),
            cy_frac=float(data["cy_fraction"]),
        )

    @classmethod
    def from_hfov(cls, hfov_deg: float) -> "CameraIntrinsics":
        return cls(f_norm=0.5 / math.tan(math.radians(hfov_deg) / 2))

    @classmethod
    def from_dfov(
        cls, dfov_deg: float, aspect_w: float = 4, aspect_h: float = 3
    ) -> "CameraIntrinsics":
        # half the sensor diagonal, in units of frame widths
        half_diag = math.hypot(1.0, aspect_h / aspect_w) / 2
        return cls(f_norm=half_diag / math.tan(math.radians(dfov_deg) / 2))

    @classmethod
    def from_focal_px(cls, focal_px: float, frame_width: int) -> "CameraIntrinsics":
        return cls(f_norm=focal_px / frame_width)

    # --- derived field of view ---

    @property
    def hfov_deg(self) -> float:
        left, right = self.cx_frac, 1.0 - self.cx_frac
        return math.degrees(
            math.atan(left / self.f_norm) + math.atan(right / self.f_norm)
        )

    def vfov_deg(self, aspect_hw: float = 0.75) -> float:
        top, bottom = self.cy_frac * aspect_hw, (1.0 - self.cy_frac) * aspect_hw
        return math.degrees(
            math.atan(top / self.f_norm) + math.atan(bottom / self.f_norm)
        )

    def focal_px(self, frame_width: int) -> float:
        return self.f_norm * frame_width

    # --- pixels -> angles ---

    def bearing_deg(self, cx_norm: float) -> float:
        """Horizontal angle off the optical axis. Positive = target is to the right."""
        return math.degrees(math.atan((cx_norm - self.cx_frac) / self.f_norm))

    def elevation_deg(self, cy_norm: float, aspect_hw: float = 0.75) -> float:
        """Vertical angle off the optical axis. Positive = target is above centre.

        ``cy_norm`` is normalised by frame *height*; image rows grow downward, so
        the sign is flipped to make "up" positive.
        """
        return -math.degrees(
            math.atan((cy_norm - self.cy_frac) * aspect_hw / self.f_norm)
        )

    # --- pixels <-> range, for a target of known real size ---

    def distance_m(self, radius_norm: float, real_radius_m: float) -> float:
        """Range to a circle of known radius, from its apparent radius.

        The pinhole relation is ``r_px = f_px * R / d``; the frame width cancels
        when both focal length and radius are normalised by it.
        """
        if radius_norm <= 0:
            return float("inf")
        return self.f_norm * real_radius_m / radius_norm

    def radius_norm_at(self, distance_m: float, real_radius_m: float) -> float:
        """Inverse of :meth:`distance_m` — the apparent radius at a given range."""
        if distance_m <= 0:
            return float("inf")
        return self.f_norm * real_radius_m / distance_m

    def max_range_m(
        self, real_radius_m: float, min_area_ratio: float, aspect_hw: float = 0.75
    ) -> float:
        """Furthest range at which a marker still clears the detector's area gate.

        This is the number that silently caps detection: with the default
        ``min_area_ratio`` a 0.25 m marker vanishes at about 4 m regardless of
        how good the rest of the pipeline is.
        """
        if min_area_ratio <= 0:
            return float("inf")
        min_radius_norm = math.sqrt(min_area_ratio * aspect_hw / math.pi)
        return self.distance_m(min_radius_norm, real_radius_m)

    def min_area_ratio_for_range(
        self, real_radius_m: float, max_range_m: float, aspect_hw: float = 0.75
    ) -> float:
        """Inverse of :meth:`max_range_m` — pick the area gate from a desired range."""
        r = self.radius_norm_at(max_range_m, real_radius_m)
        return math.pi * r * r / aspect_hw


# The real hardware, and — deliberately — the simulator too, so that parameters
# tuned in the sim transfer to the arena. The lens distortion in the file is
# left out: it moves a pixel by 2-3 px at 960x720, and by 1.2% of the frame
# width at worst, in the corners.
TELLO_INTRINSICS = CameraIntrinsics.from_calibration()
SIM_INTRINSICS = TELLO_INTRINSICS
