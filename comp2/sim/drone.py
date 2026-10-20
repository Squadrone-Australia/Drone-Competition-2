import math
import random
import threading
import time

from ..drone.base import DroneAdapter
from . import arena, track
from .disturbance import Gusts
from .imaging import ImagingModel
from .render import build_scenery, draw_minimap, render, render_focal
from .world import (
    GATE_CHECKER_ROWS,
    GATE_ORANGE_HALF_ANGLE_DEG,
    GATE_STRING_ANGLE_DEG,
    GATE_STRING_M,
    GATE_TUBE_M,
)

ANIM_FPS = 60  # pose updates per second while a command is in flight
MIN_ALT_M = 0.3
CEILING_CLEARANCE_M = 0.3  # how close to the cage ceiling the drone may climb
TAKEOFF_ALT_M = 1.0
LEAN_DEG = 12.0  # how far the airframe tips into a translation
WALL_MARGIN_M = 0.2

# rc (velocity) control: full stick is 1 m/s translation / 100 deg/s yaw, with a
# first-order response so the sim drone banks into a setpoint instead of
# teleporting onto it — that lag is what makes rc flying smoother than
# stop-start discrete steps, in the sim exactly as on hardware.
RC_MAX_SPEED_MS = 1.0
RC_MAX_YAW_DPS = 100.0
RC_TAU_S = 0.25
RC_FPS = 60

# Discrete commands (takeoff/land/move/rotate) are timed from real speeds, not
# a fixed budget, so a 2.3 m forward() takes as long as it would on a Tello.
# The SDK only bounds `speed` to 10-100 cm/s and documents no default for
# forward/up/cw, so these sit at this repo's go()/curve() default of 50 cm/s and
# the 100 deg/s that a full yaw stick gives above. `delay` scales them: the
# default 0.3 is real time, smaller is faster, 0 is instant (tests).
MOVE_SPEED_MS = 0.5
TURN_RATE_DPS = 100.0
REALTIME_DELAY_S = 0.3

# How far the aircraft wanders off its commanded path, per unit of `noise`.
GUST_SCALE_M = 1.0
# A real Tello does not stop at the same height twice. Per unit of `noise`,
# as a fraction of TAKEOFF_ALT_M, clamped to what the airframe actually does.
TAKEOFF_SPREAD = 4.0
TAKEOFF_MIN_M, TAKEOFF_MAX_M = 0.70, 1.30
# Heading error per rotate(), in degrees per unit of `noise`. Chosen so that
# --noise 0.05, the value the CLI help suggests, keeps the 2 deg sigma this
# had before it was made to scale.
YAW_NOISE_DEG = 40.0
# The Tello reports height as whole centimetres, so a controller reading it
# never sees finer than that however still the air is.
HEIGHT_QUANTUM_CM = 1.0
# Motion blur is smeared over the shutter's open time, not the whole frame
# interval — the Tello's exposure in a lit hall is a small fraction of it.
EXPOSURE_FRACTION = 0.12
# Translation only smears in proportion to how near the scene is. Using the
# cage's typical working distance keeps this a plain scalar instead of needing
# a depth buffer the renderer does not have.
BLUR_DEPTH_M = 2.5
# The camera sits on the airframe, which sits on its legs: it is never actually
# at floor level, and never below it.
CAMERA_FLOOR_M = 0.04


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


class SimDrone(DroneAdapter):
    """A drone that flies rather than teleports.

    Discrete commands (move/rotate/takeoff/land) interpolate their pose over a
    wall-clock budget, finishing on the exact end pose. Continuous control
    (:meth:`send_rc`) runs a background integrator instead, holding the last
    stick setpoint until it changes. The two are mutually exclusive: a discrete
    command zeroes the rc setpoint before it runs.
    """

    mode = "sim"
    can_reset = True  # unlike hardware, this one really can go home

    def __init__(
        self,
        world=None,
        noise=0.0,
        delay=0.3,
        seed=None,
        track_name="circuit1_oval",
        arena_name="cage",
    ):
        self.track_name = world.name if world is not None else track_name
        self.world = world if world is not None else track.build(track_name)
        self.noise = noise
        self.delay = delay
        self._seed = seed
        self.arena_name = arena_name
        self.world.appearance = arena.build(arena_name)
        self._rc_thread = None
        self._rc_lock = threading.Lock()
        self.reset()

    def reset(self):
        """Back on the takeoff pad, landed, sticks centred."""
        self._mission_pads_enabled = False
        self._rng = random.Random(self._seed)
        seed = 0 if self._seed is None else self._seed
        # The air, the camera and the scenery are all redrawn from the same
        # seed, so a run can be replayed exactly.
        self._gusts = Gusts(seed, scale=self.noise * GUST_SCALE_M)
        self._imaging = ImagingModel(self.world.appearance.imaging, seed)
        self.world.scenery = build_scenery(self.world.appearance, self.world, seed)
        self._flight_t = 0.0
        self._takeoff_alt = TAKEOFF_ALT_M
        self._last_view = None
        with self._rc_lock:
            self._rc_target = (0.0, 0.0, 0.0, 0.0)  # lr, fb, ud, yaw in -1..1
            self._vel = [0.0, 0.0, 0.0, 0.0]  # body-frame lr, fb, ud, yaw dps
        self.x, self.y = self.world.start_xy
        self.z = 0.0
        self.heading = self.world.start_heading_deg % 360.0
        self.flying = False
        # visual-only attitude, in degrees: read only by the third-person view
        self.roll = 0.0
        self.pitch = 0.0
        self._abort = False
        self._animating = False

    def _observed(self):
        """Where the aircraft actually is, as opposed to where it believes it is.

        ``self.x/y/z`` stay the *commanded* pose — the dead-reckoning truth a
        controller is entitled to assume. The gust offset is added here, at the
        moment anything looks at the aircraft: the camera, the pose feed and the
        scorer. Doing it at read time rather than inside the integrator means
        every motion path — rc, discrete moves, go_xyz, curve, takeoff, land —
        drifts without any of them needing to know, and the drift can never
        accumulate into a wall clamp.
        """
        if not self.flying:
            # On the ground the aircraft is held by the ground, not the air.
            return (self.x, self.y, self.z)
        dx, dy, dz = self._gusts.offset(self._flight_t)
        # The airframe cannot be pushed through the floor or the walls: a gust
        # that did would put the camera underneath the world, looking up at the
        # underside of it.
        x, y = self._clamp_xy(self.x + dx, self.y + dy)
        z = min(max(self.z + dz, CAMERA_FLOOR_M), self.world.height_m)
        return (x, y, z)

    @property
    def _max_alt(self) -> float:
        return self.world.height_m - CEILING_CLEARANCE_M

    # --- animation ---------------------------------------------------------

    def _animate(self, apply, duration, ease=_smoothstep):
        """Drive ``apply(t)`` from t=0 to t=1 over ``duration`` seconds.

        Always finishes with ``apply(1.0)`` so the end pose is exact — unless
        :meth:`emergency` aborted, in which case the drone stops where it is.
        """
        self._animating = True
        try:
            if duration <= 0:
                apply(1.0)
                return
            steps = max(1, round(duration * ANIM_FPS))
            for i in range(1, steps + 1):
                time.sleep(duration / steps)
                self._flight_t += duration / steps
                if self._abort:
                    return
                apply(ease(i / steps))
            apply(1.0)
        finally:
            self._animating = False

    def _require_flying(self):
        if not self.flying:
            raise RuntimeError("not flying — call takeoff() first")

    def _clamp_xy(self, x, y):
        return (
            min(max(x, WALL_MARGIN_M), self.world.width_m - WALL_MARGIN_M),
            min(max(y, WALL_MARGIN_M), self.world.depth_m - WALL_MARGIN_M),
        )

    # --- commands ----------------------------------------------------------

    def connect(self):
        pass

    def takeoff(self):
        self._abort = False
        z0 = self.z
        # A real Tello settles wherever its downward sensor and the air agree,
        # which is not the same height twice. A program that assumes it is at
        # exactly a metre after takeoff is relying on something hardware will
        # not give it.
        self._takeoff_alt = TAKEOFF_ALT_M
        if self.noise:
            spread = TAKEOFF_ALT_M * self.noise * TAKEOFF_SPREAD
            self._takeoff_alt = min(
                TAKEOFF_MAX_M,
                max(TAKEOFF_MIN_M, self._rng.gauss(TAKEOFF_ALT_M, spread)),
            )
        target = self._takeoff_alt
        self.flying = True  # props spin for the whole climb
        self._animate(
            lambda t: setattr(self, "z", z0 + (target - z0) * t),
            self._move_duration(abs(target - z0)),
        )

    def _stop_rc(self):
        """Discrete commands own the pose outright: kill the setpoint AND the
        residual velocity, so no rc momentum bleeds into or after the command."""
        with self._rc_lock:
            self._rc_target = (0.0, 0.0, 0.0, 0.0)
            self._vel = [0.0, 0.0, 0.0, 0.0]

    def land(self):
        self._stop_rc()
        z0 = self.z
        self._animate(
            lambda t: setattr(self, "z", z0 * (1 - t)), self._move_duration(z0)
        )
        self.z = 0.0
        self.flying = False
        self.roll = self.pitch = 0.0

    def emergency(self):
        with self._rc_lock:
            self._rc_target = (0.0, 0.0, 0.0, 0.0)
            self._vel = [0.0, 0.0, 0.0, 0.0]
        self._abort = True  # cut any command mid-animation
        self.flying = False
        self.z = 0.0
        self.roll = self.pitch = 0.0

    def move(self, direction, cm):
        self._require_flying()
        self._stop_rc()
        dist = cm / 100.0
        if self.noise:
            dist += self._rng.gauss(0, self.noise * dist)
        h = math.radians(self.heading)
        fx, fy = math.sin(h), math.cos(h)  # forward
        rx, ry = math.cos(h), -math.sin(h)  # right
        x0, y0, z0 = self.x, self.y, self.z
        dx = dy = dz = 0.0
        lean_pitch = lean_roll = 0.0
        if direction == "forward":
            dx, dy, lean_pitch = fx * dist, fy * dist, LEAN_DEG
        elif direction == "back":
            dx, dy, lean_pitch = -fx * dist, -fy * dist, -LEAN_DEG
        elif direction == "right":
            dx, dy, lean_roll = rx * dist, ry * dist, LEAN_DEG
        elif direction == "left":
            dx, dy, lean_roll = -rx * dist, -ry * dist, -LEAN_DEG
        elif direction == "up":
            dz = min(self._max_alt, z0 + dist) - z0
        elif direction == "down":
            dz = max(MIN_ALT_M, z0 - dist) - z0

        def step(t):
            self.x, self.y = self._clamp_xy(x0 + dx * t, y0 + dy * t)
            self.z = z0 + dz * t
            # tip into the move and level out again — a quadcopter that translates
            # perfectly flat looks like a lift, not a drone
            tilt = math.sin(math.pi * t)
            self.pitch, self.roll = lean_pitch * tilt, lean_roll * tilt

        self._animate(step, self._move_duration(abs(dist)))
        self.pitch = self.roll = 0.0

    def rotate(self, direction, deg):
        self._require_flying()
        self._stop_rc()
        if self.noise:
            deg += self._rng.gauss(0, self.noise * YAW_NOISE_DEG)
        h0 = self.heading
        turn = deg if direction == "cw" else -deg
        self._animate(
            lambda t: setattr(self, "heading", (h0 + turn * t) % 360),
            self._turn_duration(abs(deg)),
        )
        self.heading = (h0 + turn) % 360

    def flip(self, direction):
        self._require_flying()
        self._stop_rc()
        # pose is unchanged — all that moves is the visual roll the third-person
        # view draws
        sign = -1 if direction in ("b", "back") else 1
        self._animate(
            lambda t: setattr(self, "roll", (sign * 360 * t) % 360),
            max(self.delay, 0.6) if self.delay else 0.0,
        )
        self.roll = 0.0

    def _move_duration(self, dist_m):
        return dist_m / MOVE_SPEED_MS * self.delay / REALTIME_DELAY_S

    def _turn_duration(self, deg):
        return deg / TURN_RATE_DPS * self.delay / REALTIME_DELAY_S

    # --- continuous control ------------------------------------------------

    def send_rc(self, lr, fb, ud, yaw):
        """Velocity setpoint, Tello rc semantics: each channel -100..100, held
        until the next call. All-zeros is "hover" and is always accepted, even
        landed — that is what stop paths send."""
        target = tuple(max(-1.0, min(1.0, v / 100.0)) for v in (lr, fb, ud, yaw))
        if any(target):
            self._require_flying()
        with self._rc_lock:
            self._rc_target = target
        if any(target) and self._rc_thread is None:
            self._rc_thread = threading.Thread(
                target=self._rc_loop, name="sim-rc", daemon=True
            )
            self._rc_thread.start()

    def _rc_loop(self):
        last = time.perf_counter()
        while True:
            time.sleep(1.0 / RC_FPS)
            now = time.perf_counter()
            dt, last = now - last, now
            if self.flying and not self._animating:
                self._flight_t += dt
            with self._rc_lock:
                target = self._rc_target
                vel = self._vel
            if self._animating or not self.flying:
                with self._rc_lock:
                    self._vel = [0.0, 0.0, 0.0, 0.0]
                continue
            k = min(1.0, dt / RC_TAU_S)
            goal = (
                target[0] * RC_MAX_SPEED_MS,
                target[1] * RC_MAX_SPEED_MS,
                target[2] * RC_MAX_SPEED_MS,
                target[3] * RC_MAX_YAW_DPS,
            )
            vel = [v + (g - v) * k for v, g in zip(vel, goal, strict=True)]
            with self._rc_lock:
                self._vel = vel
            v_lr, v_fb, v_ud, yaw_dps = vel
            if all(abs(v) < 1e-4 for v in vel) and not any(target):
                self.roll = self.pitch = 0.0
                continue
            self.heading = (self.heading + yaw_dps * dt) % 360
            h = math.radians(self.heading)
            fx, fy = math.sin(h), math.cos(h)
            rx, ry = math.cos(h), -math.sin(h)
            self.x, self.y = self._clamp_xy(
                self.x + (v_fb * fx + v_lr * rx) * dt,
                self.y + (v_fb * fy + v_lr * ry) * dt,
            )
            self.z = min(max(self.z + v_ud * dt, MIN_ALT_M), self._max_alt)
            self.pitch = LEAN_DEG * v_fb / RC_MAX_SPEED_MS
            self.roll = LEAN_DEG * v_lr / RC_MAX_SPEED_MS

    def go_xyz(self, x_cm, y_cm, z_cm, speed_cms):
        """Straight line to a body-frame offset: x forward, y left, z up (cm)."""
        self._require_flying()
        self._stop_rc()
        self._fly_path([(x_cm / 100.0, y_cm / 100.0, z_cm / 100.0)], speed_cms / 100.0)

    def curve_xyz(self, x1, y1, z1, x2, y2, z2, speed_cms):
        """Quadratic Bezier through body-frame point 1, ending at point 2 (cm)."""
        self._require_flying()
        self._stop_rc()
        p1 = (x1 / 100.0, y1 / 100.0, z1 / 100.0)
        p2 = (x2 / 100.0, y2 / 100.0, z2 / 100.0)
        # sample the Bezier B(t) = (1-t)^2*O + 2t(1-t)*P1 + t^2*P2 (origin = 0)
        pts = []
        for i in range(1, 25):
            t = i / 24
            pts.append(tuple(2 * t * (1 - t) * p1[a] + t * t * p2[a] for a in range(3)))
        self._fly_path(pts, speed_cms / 100.0)

    def _fly_path(self, body_points, speed_ms):
        """Animate along body-frame waypoints at constant speed, no stop-start
        easing — this is the smooth pathway."""
        h = math.radians(self.heading)
        fx, fy = math.sin(h), math.cos(h)
        rx, ry = math.cos(h), -math.sin(h)
        x0, y0, z0 = self.x, self.y, self.z
        world_pts = [
            (x0 + fwd * fx - left * rx, y0 + fwd * fy - left * ry, z0 + up)
            for fwd, left, up in body_points
        ]
        length = 0.0
        prev = (x0, y0, z0)
        legs = []
        for p in world_pts:
            d = math.dist(prev, p)
            legs.append((length, length + d, prev, p))
            length += d
            prev = p
        if length <= 1e-6:
            return
        duration = length / max(speed_ms, 0.1)

        def step(t):
            s = t * length
            for s0, s1, a, b in legs:
                if s <= s1 or (s0, s1, a, b) is legs[-1]:
                    f = 0.0 if s1 == s0 else min(1.0, max(0.0, (s - s0) / (s1 - s0)))
                    self.x, self.y = self._clamp_xy(
                        a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
                    )
                    self.z = min(
                        max(a[2] + (b[2] - a[2]) * f, MIN_ALT_M), self._max_alt
                    )
                    return

        self._animate(step, duration, ease=lambda t: t)

    # --- sensing -----------------------------------------------------------

    def enable_mission_pads(self) -> None:
        self._mission_pads_enabled = True

    def disable_mission_pads(self) -> None:
        self._mission_pads_enabled = False

    def mission_pad(self) -> dict | None:
        """Pad 1 lies on the takeoff floor, rocket along the starting heading.

        Approximate the documented downward footprint, never an unlimited
        position sensor. Each invocation samples the current simulated sensor.
        """
        if not self._mission_pads_enabled:
            raise RuntimeError("Enable mission-pad detection before reading it")
        if not self.flying or not 0.3 <= self.z <= 1.2:
            return None
        h = math.radians(self.world.start_heading_deg)
        dx, dy = self.x - self.world.start_xy[0], self.y - self.world.start_xy[1]
        x = dx * math.sin(h) + dy * math.cos(h)
        y = -dx * math.cos(h) + dy * math.sin(h)
        half_width = 0.2 + (self.z - 0.3) / 3
        if max(abs(x), abs(y)) > half_width:
            return None
        return {"id": 1, "x_cm": x * 100, "y_cm": y * 100, "z_cm": self.z * 100}

    def center_on_mission_pad(self, pad_id: int, height_cm: int, speed: int) -> None:
        self._require_flying()
        self._stop_rc()
        reading = self.mission_pad()
        if reading is None or reading["id"] != pad_id:
            raise RuntimeError(f"Mission pad {pad_id} is not currently detected")
        dx, dy = self.world.start_xy[0] - self.x, self.world.start_xy[1] - self.y
        h = math.radians(self.heading)
        self._fly_path(
            [
                (
                    dx * math.sin(h) + dy * math.cos(h),
                    -dx * math.cos(h) + dy * math.sin(h),
                    height_cm / 100 - self.z,
                )
            ],
            speed / 100,
        )

    def get_frame(self):
        x, y, z = self._observed()
        frame = render(self.world, x, y, z, self.heading)
        # How far the scene slid since the last frame, in pixels — what smears
        # it. Measured from the view actually rendered, so a gust blurs the
        # picture exactly as much as it moved the aircraft.
        motion = (0.0, 0.0)
        view = (x, y, z, self.heading)
        if self._last_view is not None:
            px, py, pz, ph = self._last_view
            focal = render_focal(frame.shape[1]) * EXPOSURE_FRACTION
            yaw = math.radians((self.heading - ph + 180) % 360 - 180)
            slide = math.hypot(x - px, y - py) / BLUR_DEPTH_M
            motion = (
                abs(focal * yaw) + focal * slide,
                abs(focal * (z - pz) / BLUR_DEPTH_M),
            )
        self._last_view = view
        self._imaging.advance(1.0 / ANIM_FPS)
        return self._imaging.apply(frame, motion_px=motion)

    def height_cm(self) -> float:
        """The height a Tello *reports*, not the height it is at.

        Quantised to whole centimetres because the SDK reports integers, and
        offset by however far the air has pushed the aircraft. A climb loop that
        trusts this reading to the millimetre is trusting something the hardware
        never promised.
        """
        _x, _y, z = self._observed()
        return round(z * 100 / HEIGHT_QUANTUM_CM) * HEIGHT_QUANTUM_CM

    def annotate(self, frame):
        x, y, _z = self._observed()
        return draw_minimap(frame, self.world, x, y, self.heading)

    def battery(self):
        return 100

    # --- display feeds -----------------------------------------------------
    # Absolute arena coordinates, for drawing the third-person view and scoring,
    # and nothing else. They reach the browser over the WebSocket; they are NOT
    # reachable from comp2.api or the squadrone client — live absolute pose is
    # exactly what the real drone does not have.

    def pose(self):
        # The scorer reads this, so it must report where the aircraft really
        # flew — gust included — not where it was told to go.
        x, y, z = self._observed()
        return {
            "x": round(x, 3),
            "y": round(y, 3),
            "z": round(z, 3),
            "heading": round(self.heading, 2),
            "roll": round(self.roll, 2),
            "pitch": round(self.pitch, 2),
            "flying": self.flying,
        }

    def scene(self):
        w = self.world
        return {
            "name": w.name,
            "track": self.track_name,  # catalog id, for the picker's selection
            "arena": self.arena_name,
            # How the room looks, for the cockpit's 3D view. Display only, like
            # the rest of this payload: it says what the camera is flying
            # through, never where the aircraft is.
            "appearance": arena.to_display(self.world.appearance, self.world.scenery),
            # The stand's tube diameter and the band's layout, so the 3D view
            # models the same gate the camera sees instead of carrying its own
            # copy of the numbers.
            "tube_m": GATE_TUBE_M,
            "string_m": GATE_STRING_M,
            "string_angle_deg": GATE_STRING_ANGLE_DEG,
            "ring": {
                "orange_half_angle_deg": GATE_ORANGE_HALF_ANGLE_DEG,
                "checker_rows": GATE_CHECKER_ROWS,
            },
            "width_m": w.width_m,
            "depth_m": w.depth_m,
            "height_m": w.height_m,
            "start": list(w.start),
            "finish": list(w.finish),
            "start_heading_deg": w.start_heading_deg,
            "difficulty": w.difficulty,
            "t_ref_s": w.t_ref_s,
            "weights": dict(w.weights),
            "gates": [
                {
                    "x": g.x,
                    "y": g.y,
                    "z": g.z,
                    "yaw_deg": g.yaw_deg,
                    "diameter_m": g.diameter_m,  # outer edge of the band
                    "opening_diameter_m": g.opening_diameter_m,  # what is scored
                }
                for g in w.gates
            ],
        }

    # --- track feed --------------------------------------------------------
    # The browser's track picker: published layouts only, loaded from the JSON
    # files in comp2/tracks. There is no in-browser editing — a track must match
    # the physical cage, so organisers edit the files.

    def track_catalog(self):
        return track.catalog()

    # --- arena feed --------------------------------------------------------
    # Unlike tracks, the arena picker *is* writable from the browser: a track
    # has to match the physical cage, but what the room looks like is exactly
    # what a pilot should be able to vary to see whether a detector survives it.

    def arena_catalog(self):
        return arena.catalog()

    def load_arena(self, name=None, **parts):
        """Switch the look: a preset by name, or one entry per library."""
        if parts:
            self.world.appearance = arena.compose(name or "custom", **parts)
            self.arena_name = "custom"
        elif name:
            self.world.appearance = arena.build(name)
            self.arena_name = name
        self.reset()
        return self.world.appearance

    def randomize_arena(self, seed=None):
        """A combination drawn from every library at once."""
        self.world.appearance = arena.randomize(seed)
        self.arena_name = "random"
        self.reset()
        return self.world.appearance

    def load_track(self, name=None):
        if name:
            appearance = self.world.appearance  # survives a track change
            self.world = track.build(name)
            self.world.appearance = appearance
            self.track_name = name
        self.reset()
        return self.world
