"""Detection markers a flying program draws on the cockpit video.

Display only, exactly like the simulator's minimap: markers are composited
onto the *display* copy of a frame inside the server's video loop, never onto
the sensor frame a team's detector consumes and never onto the raw JPEGs sent
to an IDE client. The platform still ships no detector — what is outlined here
is whatever the team's own OpenCV found, sent back for the operator to see.

Coordinates on the wire are fractions of the analysed frame, so the overlay
survives the display resize and any future change of camera resolution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

#: Markers older than this are dropped rather than drawn: a box left over from
#: a frame the drone has already flown past is a lie about what it sees now.
TTL_S = 1.0
MAX_MARKERS = 12
MAX_POINTS = 16
MAX_LABEL_CHARS = 48

# --live (= --ok) from the frontend's one token block: live telemetry, which is
# what a detection is. --hoop orange would be the gate's own colour and would
# vanish into the orange tubes it is meant to outline.
COLOUR = (128, 222, 74)  # BGR of #4ade80
SHADOW = (24, 24, 24)


@dataclass(frozen=True)
class Marker:
    """One outline in frame fractions, 0..1 across the width and height."""

    points: tuple[tuple[float, float], ...]
    label: str = ""


def _fraction(value, extent: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("detection coordinates must be finite numbers")
    # Clamped well outside the frame, not to it: a gate half out of view keeps
    # its true shape, while a runaway value cannot become a giant polygon.
    return max(-1.0, min(2.0, value / extent))


def _points(raw, width: float, height: float) -> tuple[tuple[float, float], ...]:
    points = []
    for point in raw:
        pair = tuple(point)
        if len(pair) != 2:
            raise ValueError("each detection point must be an (x, y) pair")
        points.append((_fraction(pair[0], width), _fraction(pair[1], height)))
        if len(points) == MAX_POINTS:
            break
    if len(points) < 2:
        raise ValueError("a detection outline needs at least two points")
    return tuple(points)


def markers(detections, width: float, height: float) -> tuple[Marker, ...]:
    """Validate detector output in pixels into display markers in fractions.

    ``detections`` is a sequence of outlines; each is either a sequence of
    ``(x, y)`` pixel points or an ``(points, label)`` pair. Counts, label
    lengths and coordinates are clamped — a noisy frame must not end a flight —
    but a malformed shape raises, because it is a bug in the caller.
    """
    if detections is None:
        return ()
    if not (width > 0 and height > 0):
        raise ValueError("detection frame size must be positive")
    out = []
    for detection in list(detections)[:MAX_MARKERS]:
        label = ""
        items = list(detection)
        if len(items) == 2 and isinstance(items[1], str):
            items, label = list(items[0]), items[1]
        out.append(Marker(_points(items, width, height), label[:MAX_LABEL_CHARS]))
    return tuple(out)


def to_wire(found: tuple[Marker, ...]) -> list:
    """JSON-safe form of already-validated markers (client -> server)."""
    return [[[list(p) for p in m.points], m.label] for m in found]


def draw(frame: np.ndarray, found) -> np.ndarray:
    """Outline every marker on ``frame``, in place. Display copies only."""
    # Imported here so comp2.api — and a team's own script through it — keeps
    # working on a machine where OpenCV's shared libraries are unavailable.
    import cv2
    import numpy as np

    font = cv2.FONT_HERSHEY_SIMPLEX
    height, width = frame.shape[:2]
    for marker in found:
        pixels = np.array(
            [(round(x * width), round(y * height)) for x, y in marker.points],
            np.int32,
        )
        cv2.polylines(frame, [pixels], True, SHADOW, 4, cv2.LINE_AA)
        cv2.polylines(frame, [pixels], True, COLOUR, 2, cv2.LINE_AA)
        if marker.label:
            x = int(pixels[:, 0].min())
            y = int(pixels[:, 1].min())
            origin = (max(2, min(x, width - 8)), max(14, y - 7))
            for colour, thickness in ((SHADOW, 3), (COLOUR, 1)):
                cv2.putText(
                    frame,
                    marker.label,
                    origin,
                    font,
                    0.45,
                    colour,
                    thickness,
                    cv2.LINE_AA,
                )
    return frame
