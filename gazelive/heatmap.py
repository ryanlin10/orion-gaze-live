"""A gaze map -> the picture the overlay draws.

Opacity says how likely the gaze is near a point: the map's probability within about 8% of the screen's width of it,
fully opaque at M_FULL. The maps are calibrated (measured for gaze-base on people it never saw: when it puts 0.8 of the
gaze somewhere, the gaze is there about 80% of the time), so opacity can be read as confidence and a faint blob is a
guess. Drawing full strength at a fixed density instead makes wrong maps look as sure as right ones.
"""
from __future__ import annotations

import math

import numpy as np

COLOR = (235, 104, 52)   # one orange hue
M_FULL = 0.8             # probability near a point at which it is drawn fully opaque
GAMMA = 0.75             # opacity = (mass / M_FULL) ** GAMMA: a 30% blob draws at about half strength
MAX_ALPHA = 200 / 255    # never fully hide what is underneath
TAU_S = 0.12             # smoothing between maps
UPSAMPLE = 4             # cells are interpolated to this many pixels each before the screen scales them


def local_mass(p: np.ndarray, r: int) -> np.ndarray:
    """Probability within +-r cells of each cell of p [rows, cols] (nothing beyond the screen's edge)."""
    c = np.pad(p, ((r + 1, r), (r + 1, r))).cumsum(0).cumsum(1)
    n = 2 * r + 1
    return (c[n:, n:] - c[:-n, n:] - c[n:, :-n] + c[:-n, :-n]).astype(np.float32)


def _lerp(n_out: int, n_in: int) -> np.ndarray:
    """Linear interpolation matrix [n_out, n_in], cell centres aligned."""
    x = np.clip((np.arange(n_out) + 0.5) * n_in / n_out - 0.5, 0, n_in - 1)
    i = np.minimum(x.astype(int), n_in - 2)
    m = np.zeros((n_out, n_in), np.float32)
    m[np.arange(n_out), i] = 1 - (x - i)
    m[np.arange(n_out), i + 1] = x - i
    return m


class Heatmap:
    def __init__(self):
        self.smooth: np.ndarray | None = None
        self._lerps: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}

    def update(self, grid: np.ndarray | None, dt: float) -> np.ndarray | None:
        """grid: [rows, cols] probabilities (summing to the chance gaze is on screen), or None for nothing to draw.
        -> [rows * UPSAMPLE, cols * UPSAMPLE, 4] premultiplied RGBA, uint8."""
        if grid is None:
            self.smooth = None
            return None
        p = np.asarray(grid, np.float32)
        if self.smooth is None or self.smooth.shape != p.shape:
            self.smooth = p.copy()
        else:
            self.smooth += (1 - math.exp(-max(dt, 0.0) / TAU_S)) * (p - self.smooth)
        rows, cols = p.shape
        if (rows, cols) not in self._lerps:
            self._lerps[rows, cols] = _lerp(rows * UPSAMPLE, rows), _lerp(cols * UPSAMPLE, cols)
        ly, lx = self._lerps[rows, cols]
        r = max(1, round(cols / 32))          # +-2 cells of 64x40, +-1 of 32x20
        mass = ly @ local_mass(self.smooth, r) @ lx.T
        a = np.clip(mass / M_FULL, 0, 1) ** GAMMA * MAX_ALPHA
        rgba = np.empty(a.shape + (4,), np.uint8)
        rgba[..., :3] = (a[..., None] * np.array(COLOR, np.float32)).astype(np.uint8)
        rgba[..., 3] = (a * 255).astype(np.uint8)
        return rgba
