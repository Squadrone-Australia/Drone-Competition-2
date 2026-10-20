"""Scoring a simulated race against the track's ground truth.

Fed :meth:`DroneAdapter.pose` at the pose-loop rate and built from
:meth:`DroneAdapter.scene` — the same one-way display feeds the browser's 3D
view runs on. It is a *scoring* feed and stays one-way: nothing here may
become part of the contestant API or a sensor.

Three metrics, per the competition brief:

* **Completion time** — first takeoff to final landing.
* **Gate accuracy** — where the flight path pierced each gate's plane,
  measured as the offset from the ring's centre. The crossing point is
  linearly interpolated between the two pose samples that straddle the plane,
  so a fast pass between samples is still measured, not missed. The opening
  is a circle, so the score falls with the offset over its radius: 1 dead
  centre, 0 at the fabric band.
* **Trajectory smoothness** — RMS jerk over the whole sampled path. Scored on
  the pose stream rather than on commands, so stop-start discrete moves and
  continuous rc flying are judged by the same rule.
"""

import math

from .world import DEFAULT_GATE_OPENING_M

FINISH_RADIUS_M = 0.4  # how close to the landing pad a finish must be
# RMS jerk that maps to a smoothness of 1/e. Calibrated against the 30 Hz pose
# stream (third-order finite differences amplify sampling noise, so raw values
# run in the hundreds): a held rc glide measures ~160, a stop-start discrete
# lap ~470 — scoring ~0.6 vs ~0.2 here.
J_REF = 300.0
MIN_SMOOTH_SAMPLES = 8  # need a few samples before jerk means anything

IDLE, RUNNING, FINISHED, DNF = "idle", "running", "finished", "dnf"


def _opening_radius(gate: dict) -> float:
    """Radius of a scene gate's clear opening, in metres."""
    return (gate.get("opening_diameter_m") or DEFAULT_GATE_OPENING_M) / 2


class RaceScorer:
    """Ground truth for one attempt. Rebuilt on every reset."""

    def __init__(self, scene: dict):
        self.gates = [dict(g) for g in scene.get("gates") or []]
        finish = scene.get("finish") or scene.get("start") or (0.0, 0.0)
        self.finish_xy = (float(finish[0]), float(finish[1]))
        self.difficulty = float(scene.get("difficulty") or 1.0)
        self.t_ref_s = float(scene.get("t_ref_s") or 30.0)
        weights = scene.get("weights") or {}
        self.weights = {
            "smoothness": float(weights.get("smoothness", 0.4)),
            "accuracy": float(weights.get("accuracy", 0.3)),
            "time": float(weights.get("time", 0.3)),
        }
        self.state_name = IDLE
        self.next_gate = 0
        # {"index", "time_s", "offset_m", "edge"} per passed gate; "edge" is how
        # far out towards the band the pass was, 0 at the centre, 1 at the rim
        self.results: list[dict] = []
        self.start_t: float | None = None
        self.end_t: float | None = None
        self._samples: list[tuple] = []  # (t, x, y, z)
        self._prev = None  # previous (t, x, y, z) while running

    # --- feeding -----------------------------------------------------------

    def sample(self, pose: dict, t: float) -> bool:
        """Advance the race with one pose sample. Returns True on any change
        worth broadcasting (start, gate pass, finish, DNF)."""
        if pose is None or self.state_name in (FINISHED, DNF):
            return False
        flying = bool(pose.get("flying"))
        p = (t, pose["x"], pose["y"], pose["z"])

        if self.state_name == IDLE:
            if not flying:
                return False
            self.state_name = RUNNING
            self.start_t = t
            self._samples.append(p)
            self._prev = p
            return True

        changed = False
        self._samples.append(p)
        if self._prev is not None and self.next_gate < len(self.gates):
            crossing = self._crossing(self.gates[self.next_gate], self._prev, p)
            if crossing is not None:
                t_pass, offset, edge = crossing
                self.results.append(
                    {
                        "index": self.next_gate,
                        "time_s": round(t_pass - self.start_t, 2),
                        "offset_m": round(offset, 3),
                        "edge": edge,
                    }
                )
                self.next_gate += 1
                changed = True
        self._prev = p

        if not flying:
            # landed: the race is over one way or the other
            if (
                self.next_gate == len(self.gates)
                and math.hypot(
                    pose["x"] - self.finish_xy[0], pose["y"] - self.finish_xy[1]
                )
                <= FINISH_RADIUS_M
            ):
                self.state_name = FINISHED
            else:
                self.state_name = DNF
            self.end_t = t
            changed = True
        return changed

    def _crossing(self, gate: dict, a: tuple, b: tuple):
        """Where segment a->b pierces the gate plane inside the opening, if it does.

        Returns (time, offset from the centroid, edge fraction) or None. The
        crossing must run along the gate normal (+n) — flying through backwards
        is not a pass, and neither is drifting past outside the opening.
        """
        yaw = math.radians(gate.get("yaw_deg") or 0.0)
        nx, ny = math.sin(yaw), math.cos(yaw)
        cx, cy, cz = gate["x"], gate["y"], gate["z"]
        s_a = (a[1] - cx) * nx + (a[2] - cy) * ny
        s_b = (b[1] - cx) * nx + (b[2] - cy) * ny
        if not (s_a < 0 <= s_b):  # must go from behind to in front
            return None
        f = s_a / (s_a - s_b)  # fraction of the segment at the plane
        t = a[0] + (b[0] - a[0]) * f
        px = a[1] + (b[1] - a[1]) * f
        py = a[2] + (b[2] - a[2]) * f
        pz = a[3] + (b[3] - a[3]) * f
        # offset inside the ring plane: lateral (perpendicular to the normal,
        # in the floor plane) and vertical components
        lateral = (px - cx) * ny - (py - cy) * nx
        vertical = pz - cz
        offset = math.hypot(lateral, vertical)
        edge = offset / _opening_radius(gate)
        if edge > 1.0:
            return None
        return t, offset, edge

    # --- scoring -----------------------------------------------------------

    @property
    def elapsed_s(self) -> float:
        if self.start_t is None:
            return 0.0
        end = self.end_t if self.end_t is not None else self._samples[-1][0]
        return max(0.0, end - self.start_t)

    def _jerk_rms(self) -> float | None:
        """RMS jerk (m/s^3) of the sampled path via finite differences."""
        s = self._samples
        if len(s) < MIN_SMOOTH_SAMPLES:
            return None
        total, count = 0.0, 0
        for i in range(3, len(s)):
            t0, t3 = s[i - 3][0], s[i][0]
            dt = (t3 - t0) / 3
            if dt <= 1e-4:
                continue
            j = 0.0
            for axis in (1, 2, 3):
                # third-order finite difference of position ~= jerk * dt^3
                d3 = (
                    s[i][axis]
                    - 3 * s[i - 1][axis]
                    + 3 * s[i - 2][axis]
                    - s[i - 3][axis]
                )
                j += (d3 / dt**3) ** 2
            total += j
            count += 1
        return math.sqrt(total / count) if count else None

    def score(self) -> dict | None:
        """The three metric scores plus the difficulty-weighted total, or None
        until the race has actually started."""
        if self.state_name == IDLE:
            return None
        jerk = self._jerk_rms()
        s_smooth = math.exp(-jerk / J_REF) if jerk is not None else 0.0
        if self.gates:
            per_gate = {r["index"]: r for r in self.results}
            acc = 0.0
            for i in range(len(self.gates)):
                r = per_gate.get(i)
                if r is not None:
                    acc += max(0.0, 1.0 - r["edge"])
            s_acc = acc / len(self.gates)
        else:
            s_acc = 0.0
        elapsed = self.elapsed_s
        s_time = (self.t_ref_s / max(elapsed, self.t_ref_s)) if elapsed > 0 else 0.0
        if self.state_name != FINISHED:
            s_time = 0.0  # no completion, no time score
        w = self.weights
        total = (
            self.difficulty
            * 100
            * (w["smoothness"] * s_smooth + w["accuracy"] * s_acc + w["time"] * s_time)
        )
        return {
            "smoothness": round(s_smooth, 3),
            "accuracy": round(s_acc, 3),
            "time": round(s_time, 3),
            "total": round(total, 1),
        }

    def state(self) -> dict:
        """The race panel's whole view of the attempt — the wire message body."""
        per_gate = {r["index"]: r for r in self.results}
        return {
            "state": self.state_name,
            "next_gate": self.next_gate if self.next_gate < len(self.gates) else None,
            "passed": len(self.results),
            "total": len(self.gates),
            "elapsed_s": round(self.elapsed_s, 2),
            "gates": [
                {
                    "index": i,
                    "passed": i in per_gate,
                    "time_s": per_gate[i]["time_s"] if i in per_gate else None,
                    "offset_m": per_gate[i]["offset_m"] if i in per_gate else None,
                }
                for i in range(len(self.gates))
            ],
            "score": self.score(),
        }
