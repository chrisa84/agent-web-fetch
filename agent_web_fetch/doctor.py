"""`agent-web-fetch doctor`: environment and installation checks. Never prints secrets."""

import json
import os
import sqlite3
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import data_dir

LOCKFILE = Path(__file__).resolve().parent.parent / "requirements.lock"


def run_doctor(secrets: dict) -> int:
    failures = 0

    def report(ok, name, detail=""):
        nonlocal failures
        tag = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        if ok is False:
            failures += 1
        print(f"[{tag}] {name}" + (f": {detail}" if detail else ""))

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    report(sys.version_info >= (3, 10), "python", f"{sys.version.split()[0]} at {sys.executable}")
    report(in_venv or None, "virtualenv", sys.prefix if in_venv else "not running inside a virtualenv")

    # Installed packages must match the reviewed lockfile exactly.
    if LOCKFILE.exists():
        pins = {}
        for line in LOCKFILE.read_text(encoding="utf-8").splitlines():
            if "==" in line and not line.startswith((" ", "#")):
                name, ver = line.split(" ")[0].split("==")
                pins[name.lower()] = ver
        bad = []
        for name, ver in pins.items():
            try:
                have = version(name)
            except PackageNotFoundError:
                have = None
            if have != ver:
                bad.append(f"{name} {have or 'missing'} != {ver}")
        report(not bad, "lockfile", f"{len(pins)} packages match" if not bad else "; ".join(bad[:5]))
    else:
        report(None, "lockfile", "requirements.lock not found next to the package (not a source checkout?)")

    for dep in ("httpx", "camoufox", "playwright"):
        try:
            report(True, f"dependency {dep}", version(dep))
        except PackageNotFoundError:
            report(False, f"dependency {dep}", "not installed")

    from .backends.camoufox import install_dir, installed_executable, load_pins

    pins = load_pins()
    exe = installed_executable()
    if exe:
        marker = json.loads((install_dir() / "INSTALLED.json").read_text(encoding="utf-8"))
        ini = install_dir() / "application.ini"
        ff = next((l.split("=", 1)[1] for l in ini.read_text().splitlines() if l.startswith("Version=")), "?") \
            if ini.exists() else "?"
        report(marker.get("tag") == pins["tag"], "camoufox browser",
               f"{pins['tag']} (Firefox {ff}), sha256 {marker.get('sha256', '')[:16]}...")
        report(True, "browser location", str(exe))
    else:
        report(False, "camoufox browser", "not installed; run: agent-web-fetch install-browser")

    try:
        import httpx

        r = httpx.get("https://r.jina.ai/", timeout=10, trust_env=False)
        report(r.status_code < 500, "jina connectivity", f"HTTP {r.status_code}")
    except Exception as e:
        report(False, "jina connectivity", type(e).__name__)
    report(True, "JINA_API_KEY", "set" if secrets.get("JINA_API_KEY") else "not set (anonymous, lower rate limits)")

    root = data_dir()
    try:
        from .cache import Cache

        cache = Cache(root / "cache" / "cache.db")
        try:
            from .status import summary

            n = cache.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            ok = cache.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            report(True, "request discipline", summary(cache.discipline_status()))
        finally:
            cache.close()
        report(ok, "cache database", f"{root / 'cache' / 'cache.db'} ({n} pages)")
    except sqlite3.Error as e:
        report(False, "cache database", str(e))

    profiles = root / "profiles"
    report(profiles.is_dir(), "profile directory",
           f"{profiles} ({sum(1 for _ in profiles.iterdir()) if profiles.is_dir() else 0} domains)")

    if os.name == "posix":
        loose = [str(p) for p in [root, root / "cache", root / "profiles", root / "tmp"]
                 if p.exists() and p.stat().st_mode & 0o077]
        report(not loose, "permissions", "owner-only" if not loose else "group/other access on " + ", ".join(loose))
        report(os.geteuid() != 0, "unprivileged user")
    else:
        home = Path(os.path.expanduser("~")).resolve()
        under_home = home in root.resolve().parents
        report(under_home or None, "permissions",
               "data dir is inside the user profile (per-user ACLs)" if under_home else f"check ACLs on {root}")
        report(True, "unprivileged user", "not elevated")

    print("\nall checks passed" if not failures else f"\n{failures} check(s) failed")
    return 1 if failures else 0
