import os
import stat

import numpy as np

from gazelive import config, heatmap
from gazelive.heatmap import Heatmap


def test_confident_map_draws_opaque_and_flat_map_faint():
    sharp = np.zeros((40, 64), np.float32)
    sharp[20, 32] = 0.9
    rgba = Heatmap().update(sharp, 0.03)
    assert rgba.shape == (160, 256, 4) and rgba.dtype == np.uint8
    alpha = rgba[..., 3].astype(float)
    assert alpha.max() == int(heatmap.MAX_ALPHA * 255)
    ys, xs = np.nonzero(alpha == alpha.max())   # the opaque blob sits on the cell, in the 4x upsampled picture
    assert abs(ys.mean() - 20.5 * 4) <= 2 and abs(xs.mean() - 32.5 * 4) <= 2

    flat = np.full((40, 64), 0.95 / 2560, np.float32)
    assert Heatmap().update(flat, 0.03)[..., 3].max() < 40


def test_premultiplied_and_smoothed():
    h = Heatmap()
    a = np.zeros((20, 32), np.float32)
    a[2, 2] = 1.0
    b = np.zeros((20, 32), np.float32)
    b[17, 29] = 1.0
    h.update(a, 0.03)
    rgba = h.update(b, 0.03)                       # one frame later: mostly still at the old place
    assert rgba[2 * 4, 2 * 4, 3] > rgba[17 * 4, 29 * 4, 3]
    assert (rgba[..., :3] <= rgba[..., 3:4]).all()
    assert h.update(None, 0.03) is None and h.smooth is None


def test_key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    assert config.load_key() is None
    path = config.save_key("  orion_abc \n")
    assert path == tmp_path / "gaze-live" / "api-key"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert config.load_key() == "orion_abc"
    monkeypatch.setenv(config.ENV_VAR, "from_env")
    assert config.load_key() == "from_env"
