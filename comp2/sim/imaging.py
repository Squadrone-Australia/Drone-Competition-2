"""What the camera does to the picture after the optics have formed it.

:mod:`comp2.sim.render` produces a geometrically honest frame: flat, noiseless
and perfectly exposed. No real camera hands you that. The Tello's frames arrive
vignetted, smeared by the airframe's own motion, hunting for exposure against a
bright doorway, speckled with sensor noise and chewed by H.264. A detector tuned
against the clean frame and never shown these is being flattered, not tested.

This stage is kept strictly *after* rendering and outside :func:`render` itself,
which stays pure and deterministic — geometry tests depend on that. Everything
stochastic or frame-to-frame lives here, behind
:meth:`comp2.sim.drone.SimDrone.get_frame`.

Seeded: the same seed and the same pose sequence reproduce the same frames, so a
failing run can be replayed. The clock is the drone's *flight* time, never
``time.monotonic()`` — the test harness skips real sleeps, and a wall-clock phase
would make runs irreproducible.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

# One precomputed bank of noise, cropped at a random offset per frame. Drawing
# fresh gaussian noise costs ~11.6 ms a frame — more than everything else here
# put together — and at these amplitudes is indistinguishable from reusing a
# bank, so the bank it is.
MAX_BLUR_PX = 9
_NOISE_BANK_SHAPE = (600, 800, 3)
_noise_bank: np.ndarray | None = None
_vignette_cache: dict[tuple[int, int, int], np.ndarray] = {}


def _bank(rng) -> np.ndarray:
    global _noise_bank
    if _noise_bank is None:
        _noise_bank = rng.integers(-128, 128, _NOISE_BANK_SHAPE, dtype=np.int16)
    return _noise_bank


def _vignette_map(w: int, h: int, strength: float) -> np.ndarray:
    """Radial falloff as a uint8 gain map, cached per size and strength.

    ``strength`` is the fraction of brightness lost at the extreme corners. The
    footage's own corner-to-centre ratio ranges from 0.39 to 0.96 across five
    frames, because scene content dominates it, so this is set mild rather than
    fitted to a number that is mostly not about the lens.
    """
    key = (w, h, round(strength * 100))
    cached = _vignette_cache.get(key)
    if cached is not None:
        return cached
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2) / math.sqrt(2)
    gain = np.clip(1.0 - strength * r**2, 0.0, 1.0)
    out = np.repeat((gain * 255).astype(np.uint8)[:, :, None], 3, axis=2)
    _vignette_cache[key] = out
    return out


class ImagingModel:
    """Applies one camera's imperfections to rendered frames.

    Holds its own RNG and a flight clock so that a given seed replays exactly.
    """

    def __init__(self, spec, seed: int = 0):
        self.spec = spec
        self.rng = np.random.default_rng(seed)
        self._t = 0.0
        # Fixed phases, so exposure wander is a smooth function of flight time
        # rather than a fresh random number each frame.
        self._phase = self.rng.uniform(0, 2 * math.pi, 4)

    def advance(self, dt: float) -> None:
        self._t += max(0.0, dt)

    def _exposure_gain(self) -> tuple[float, float, float]:
        """Slow auto-exposure and white-balance wander, as a per-channel gain."""
        s = self.spec
        if s.exposure <= 0 and s.white_balance <= 0:
            return (1.0, 1.0, 1.0)
        e = 1.0 + s.exposure * math.sin(2 * math.pi * 0.11 * self._t + self._phase[0])
        b = 1.0 + s.white_balance * math.sin(
            2 * math.pi * 0.07 * self._t + self._phase[1]
        )
        r = 1.0 - s.white_balance * math.sin(
            2 * math.pi * 0.05 * self._t + self._phase[2]
        )
        return (e * b, e, e * r)

    def apply(self, img: np.ndarray, motion_px: tuple[float, float] = (0.0, 0.0)):
        """Run the chain, in the order a real camera imposes it."""
        s = self.spec
        out = img

        # 1. Motion blur, from how far the scene slid during the exposure. Two
        #    separable box blurs beat rotating a kernel through filter2D.
        if s.motion_blur > 0:
            kx = int(abs(motion_px[0]) * s.motion_blur)
            ky = int(abs(motion_px[1]) * s.motion_blur)
            # Capped: past a few pixels the frame is unusable anyway, and an
            # unbounded kernel turns one teleport into a grey rectangle.
            if kx > 1:
                out = cv2.blur(out, (min(kx, MAX_BLUR_PX), 1))
            if ky > 1:
                out = cv2.blur(out, (1, min(ky, MAX_BLUR_PX)))

        # 2. Bloom: bright sources bleed into their surroundings, which is what
        #    a doorway does to everything beside it.
        if s.bloom > 0:
            grey = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
            hot = cv2.threshold(grey, 230, 255, cv2.THRESH_BINARY)[1]
            if hot.any():
                glow = cv2.GaussianBlur(hot, (0, 0), 9).astype(np.float32) * s.bloom
                out = cv2.add(
                    out, cv2.cvtColor(glow.astype(np.uint8), cv2.COLOR_GRAY2BGR)
                )

        # 3. Exposure and white balance, before the lens shading is applied.
        gains = self._exposure_gain()
        if gains != (1.0, 1.0, 1.0):
            lut = np.clip(
                np.arange(256)[None, :] * np.array(gains)[:, None], 0, 255
            ).astype(np.uint8)
            channels = list(cv2.split(out))
            for i in range(3):
                channels[i] = cv2.LUT(channels[i], lut[i])
            out = cv2.merge(channels)

        # 4. Lens shading.
        if s.vignette > 0:
            out = cv2.multiply(
                out,
                _vignette_map(img.shape[1], img.shape[0], s.vignette),
                scale=1 / 255,
            )

        # 5. Sensor noise.
        if s.noise > 0:
            bank = _bank(self.rng)
            h, w = out.shape[:2]
            oy = int(self.rng.integers(0, max(1, bank.shape[0] - h)))
            ox = int(self.rng.integers(0, max(1, bank.shape[1] - w)))
            crop = bank[oy : oy + h, ox : ox + w]
            out = cv2.add(
                out, (crop * (s.noise / 128.0)).astype(np.int16), dtype=cv2.CV_8U
            )

        # 6. Compression, last, exactly as the link does it. Chroma subsampling
        #    on a red tube against dark wood is precisely where a colour key
        #    starts to struggle, so the detector's own copy must see it too.
        if s.jpeg_quality:
            ok, buf = cv2.imencode(
                ".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, int(s.jpeg_quality)]
            )
            if ok:
                out = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        return out
