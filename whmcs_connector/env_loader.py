# -*- coding: utf-8 -*-
"""Small dependency-free .env loader used by the connector and Odoo sync."""
import os
from pathlib import Path

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}

def _candidate_files():
    here = Path(__file__).resolve()
    module_root = here.parent.parent
    candidates = []
    explicit = os.getenv("WHMCS_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend([module_root / ".env", Path.cwd() / ".env"])
    seen = set()
    for path in candidates:
        try: key = str(path.resolve())
        except OSError: key = str(path)
        if key not in seen:
            seen.add(key)
            yield path

def load_dotenv(path=None, override=False):
    """Load simple KEY=VALUE pairs without requiring python-dotenv."""
    paths = [Path(path).expanduser()] if path else list(_candidate_files())
    loaded = {}
    for env_path in paths:
        if not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key:
                continue
            if value and value[0] in ('"', "'") and value[-1:] == value[0]:
                value = value[1:-1]
            loaded[key] = value
            if override or key not in os.environ:
                os.environ[key] = value
        break
    return loaded

def env_bool(name, default=True):
    value = os.getenv(name)
    if value is None: return default
    value = value.strip().lower()
    if value in _TRUE: return True
    if value in _FALSE: return False
    return default

def env_timeout():
    value = os.getenv("WHMCS_API_TIMEOUT", "10,30")
    try:
        parts = [int(x.strip()) for x in value.split(",", 1)]
        if len(parts) == 1: return (parts[0], parts[0])
        return (parts[0], parts[1])
    except (TypeError, ValueError):
        return (10, 30)
