"""agent-web-fetch: readable Markdown from public web pages for LLM agents."""

import os
import sys
from pathlib import Path

__version__ = "0.1.0"

USER_AGENT = f"agent-web-fetch/{__version__} (local CLI)"


def data_dir() -> Path:
    """Application data root. Never inside the source tree."""
    override = os.environ.get("AGENT_WEB_FETCH_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "agent-web-fetch"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agent-web-fetch"


def ensure_dirs() -> Path:
    """Create the data directory tree with owner-only permissions."""
    root = data_dir()
    for sub in ("", "cache", "profiles", "tmp", "browser", "logs"):
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(d, 0o700)
    return root
