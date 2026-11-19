"""Where the API key lives: ORION_API_KEY, else ~/.config/gaze-live/api-key (readable by you only)."""
from __future__ import annotations

import functools
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

ENV_VAR = "ORION_API_KEY"
API_ORIGIN = "https://api.paradigmmachines.ai"
KEYS_PAGE = "https://app.paradigmmachines.ai"
USER_AGENT = f"gaze-live/{__version__}"   # the API's edge refuses Python's default User-Agent


@functools.lru_cache(maxsize=1)
def tls() -> ssl.SSLContext:
    """TLS that trusts what the operating system trusts. Python's own store can be empty (python.org's installer leaves
    it to a separate step) or miss a company's proxy certificate."""
    import truststore
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def key_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "gaze-live" / "api-key"


def load_key() -> str | None:
    key = os.environ.get(ENV_VAR, "").strip()
    if key:
        return key
    try:
        key = key_file().read_text().strip()
    except OSError:
        return None
    return key or None


def save_key(key: str) -> Path:
    """Write the key with mode 600, atomically."""
    path = key_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.strip() + "\n")
    os.replace(tmp, path)
    return path


def verify_key(key: str, origin: str = API_ORIGIN) -> tuple[bool | None, str]:
    """Ask the API whether it takes this key. (True, what is left today) | (False, why not) | (None, couldn't tell)."""
    req = urllib.request.Request(f"{origin}/v1/usage", headers={"X-API-Key": key, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15, context=tls()) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "the API refused this key (revoked or mistyped)"
        return None, f"the API answered {e.code}"
    except (OSError, ValueError) as e:
        return None, f"couldn't reach the API ({e})"
    try:   # days run oldest first and end with today (UTC)
        left = body["limits"]["stream_minutes_per_day"] - body["days"][-1]["stream_minutes"]
        return True, f"key accepted, {max(0, round(left))} stream minutes left today (UTC)"
    except (KeyError, IndexError, TypeError):
        return True, "key accepted"
