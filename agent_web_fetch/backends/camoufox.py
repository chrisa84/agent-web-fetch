"""Camoufox browser backend, plus the pinned browser installer.

Security notes:
- The browser binary is installed by `install_browser()` from the official
  daijro/camoufox GitHub release pinned in browser_pins.json and verified
  against the pinned sha256. Camoufox's own downloader is never used: it falls
  back to other repos and skips verification when no digest is published.
- The uBlock Origin default addon is excluded (it is fetched unpinned from AMO).
- All browser traffic is forced through security.EgressProxy, which applies
  the SSRF policy to every connection, including redirect hops (which
  Playwright's request routing does not see). WebRTC is restricted to the
  proxy. A route handler additionally rejects non-http(s) schemes.
- Each domain gets a persistent profile and a fingerprint generated once and
  reused, so cookies, consent and login state survive between fetches.
"""

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import warnings
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .. import data_dir
from ..extract import EXTRACT_JS, SHORT_PAGE_CHARS
from ..security import EgressProxy, URLRejected, browser_env, canonicalize, check_host
from . import FetchError, parse_retry_after

PINS_FILE = Path(__file__).resolve().parent.parent / "browser_pins.json"
NAV_TIMEOUT_MS = 30_000
LOAD_GRACE_MS = 6_000
SETTLE_SECONDS = 4.0
HARD_TIMEOUT = 75.0
CHALLENGE_WAIT_SECONDS = 15.0
MAX_BROWSERS = 3  # machine-wide cap on concurrently running browsers
SLOT_LEASE = 150.0  # renewed on every fetch; frees the slot if a process dies
SLOT_WAIT = 120.0  # must stay below fetch.LEASE_SECONDS
DOWNLOAD_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
LAUNCH_FILE = {"win": "camoufox.exe", "lin": "camoufox-bin", "mac": "Camoufox.app/Contents/MacOS/camoufox"}


# --- installation --------------------------------------------------------------

def load_pins() -> dict:
    return json.loads(PINS_FILE.read_text(encoding="utf-8"))


def platform_key() -> tuple:
    os_name = {"win32": "win", "darwin": "mac"}.get(sys.platform, "lin")
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    return os_name, arch


def install_dir() -> Path:
    pins = load_pins()
    os_name, arch = platform_key()
    return data_dir() / "browser" / f"{pins['tag']}-{os_name}.{arch}"


def installed_executable() -> Optional[Path]:
    """Path to the verified pinned browser, or None if not installed."""
    d = install_dir()
    marker = d / "INSTALLED.json"
    exe = d / LAUNCH_FILE[platform_key()[0]]
    if marker.exists() and exe.exists():
        return exe
    return None


def install_browser(progress=print) -> Path:
    """Download the pinned Camoufox build, verify sha256, and extract it."""
    import httpx

    pins = load_pins()
    os_name, arch = platform_key()
    asset = pins["assets"].get(f"{os_name}.{arch}")
    if not asset:
        raise FetchError("browser_not_installed", f"no pinned Camoufox build for {os_name}.{arch}")
    if urlsplit(asset["url"]).hostname != "github.com" or not asset["url"].startswith(
            f"https://github.com/{pins['repo']}/releases/download/{pins['tag']}/"):
        raise FetchError("browser_not_installed", "pinned asset URL is not an official release URL")

    target = install_dir()
    if installed_executable():
        progress(f"Already installed: {target}")
        return target

    tmp_zip = data_dir() / "tmp" / asset["name"]
    tmp_zip.parent.mkdir(parents=True, exist_ok=True)
    progress(f"Downloading {asset['url']}")
    progress(f"Expected sha256 {asset['sha256']} ({asset['size'] / 1e6:.0f} MB)")
    digest = hashlib.sha256()
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), trust_env=False, follow_redirects=False) as client:
        url = asset["url"]
        for _ in range(5):  # follow GitHub's redirect to its asset CDN, but only to GitHub hosts
            with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = str(resp.url.join(resp.headers["location"]))
                    if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in DOWNLOAD_HOSTS:
                        raise FetchError("browser_not_installed", f"refusing redirect to {urlsplit(url).hostname}")
                    continue
                resp.raise_for_status()
                done = 0
                with open(tmp_zip, "wb") as f:
                    for chunk in resp.iter_bytes(1 << 20):
                        f.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if done % (50 << 20) < (1 << 20):
                            progress(f"  {done / 1e6:.0f} MB")
                break
        else:
            raise FetchError("browser_not_installed", "too many redirects")

    actual = digest.hexdigest()
    if actual != asset["sha256"]:
        tmp_zip.unlink(missing_ok=True)
        raise FetchError("browser_not_installed",
                         f"sha256 mismatch: expected {asset['sha256']}, got {actual}. Download discarded.")
    progress("sha256 verified")

    staging = target.with_name(target.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(tmp_zip) as zf:
        root = staging.resolve()
        for member in zf.infolist():
            dest = (staging / member.filename).resolve()
            if root not in dest.parents and dest != root:
                raise FetchError("browser_not_installed", f"unsafe path in archive: {member.filename}")
        zf.extractall(staging)
    if os_name != "win":
        for p in staging.rglob("*"):
            if p.is_file() and (p.name.startswith("camoufox") or p.suffix in ("", ".so")):
                p.chmod(p.stat().st_mode | 0o100)
    (staging / "INSTALLED.json").write_text(json.dumps({"tag": pins["tag"], "asset": asset["name"],
                                                        "sha256": actual}, indent=2), encoding="utf-8")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    tmp_zip.unlink(missing_ok=True)
    progress(f"Installed to {target}")
    return target


# --- per-domain profiles -------------------------------------------------------

def profile_dir(host: str) -> Path:
    name = host[4:] if host.startswith("www.") else host
    safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in name)[:120] or "_"
    return data_dir() / "profiles" / safe


def _launch_options(host: str, exe: Path) -> dict:
    """Launch options for `host`, generating and persisting a fingerprint on first use."""
    pdir = profile_dir(host)
    (pdir / "user-data").mkdir(parents=True, exist_ok=True)
    tmp = str(data_dir() / "tmp")
    env = browser_env(tmp)
    saved_file = pdir / "launch.json"

    if saved_file.exists():
        saved = json.loads(saved_file.read_text(encoding="utf-8"))
    else:
        from camoufox import DefaultAddons
        from camoufox.utils import launch_options

        with warnings.catch_warnings():
            # ff_version must be passed explicitly because we bypass Camoufox's
            # managed install; that triggers an advisory LeakWarning.
            warnings.simplefilter("ignore")
            opts = launch_options(
                executable_path=str(exe),
                ff_version=load_pins()["ff_version"],
                headless=True,
                exclude_addons=[DefaultAddons.UBO],
                env=env,
            )
        saved = {
            "camou_env": {k: v for k, v in opts["env"].items() if k.startswith("CAMOU_CONFIG_")},
            "fontconfig": opts["env"].get("FONTCONFIG_FILE"),
            "args": opts.get("args") or [],
            "firefox_user_prefs": opts.get("firefox_user_prefs") or {},
        }
        saved_file.write_text(json.dumps(saved), encoding="utf-8")
        if os.name == "posix":
            os.chmod(saved_file, 0o600)
            os.chmod(pdir, 0o700)

    env.update(saved["camou_env"])
    if saved.get("fontconfig"):
        env["FONTCONFIG_FILE"] = saved["fontconfig"]
    return {
        "executable_path": str(exe),
        "args": saved["args"],
        "firefox_user_prefs": saved["firefox_user_prefs"],
        "headless": True,
        "env": env,
        "user_data_dir": str(pdir / "user-data"),
        "service_workers": "block",
        "accept_downloads": False,
    }


# --- fetching ------------------------------------------------------------------

class _Guard:
    """Route handler enforcing the SSRF policy on every browser request."""

    def __init__(self, allow_private: bool):
        self.allow_private = allow_private
        self.verdicts: dict = {}
        self.blocked: list = []

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme in ("data", "blob", "about"):
            return True
        if parts.scheme not in ("http", "https", "ws", "wss") or not parts.hostname:
            return False
        port = parts.port or (443 if parts.scheme in ("https", "wss") else 80)
        key = (parts.hostname, port)
        if key not in self.verdicts:
            try:
                await asyncio.to_thread(check_host, parts.hostname, port, self.allow_private)
                self.verdicts[key] = True
            except URLRejected:
                self.verdicts[key] = False
        return self.verdicts[key]

    async def route(self, route):
        if await self.allowed(route.request.url):
            await route.continue_()
        else:
            self.blocked.append(route.request.url)
            await route.abort("blockedbyclient")

    async def ws_route(self, ws):
        if await self.allowed(ws.url):
            ws.connect_to_server()
        else:
            self.blocked.append(ws.url)
            await ws.close()


# Firefox prefs that force all traffic, localhost included, through the egress proxy.
PROXY_PREFS = {
    "network.proxy.allow_hijacking_localhost": True,
    "network.proxy.no_proxies_on": "",
    "network.proxy.socks_remote_dns": True,
    "media.peerconnection.ice.proxy_only": True,
    "network.dns.disablePrefetch": True,
    "network.prefetch-next": False,
}


class _Session:
    """One running browser (persistent per-domain profile) behind its own egress proxy."""

    def __init__(self, proxy, manager, context, guard):
        self.proxy, self.manager, self.context, self.guard = proxy, manager, context, guard

    async def close(self) -> None:
        try:
            await self.manager.__aexit__(None, None, None)
        except Exception:
            pass
        await self.proxy.__aexit__(None, None, None)


class BrowserPool:
    """Keeps one browser per domain open for the lifetime of a CLI run.

    Pages for the same domain are fetched one at a time. Every open browser
    holds one of MAX_BROWSERS machine-wide slots (non-reentrant leases in the
    shared SQLite file), so no combination of processes or batches can run
    more browsers than that at once. Callers must also hold the domain's
    browser lease while a session is open (see fetch.py).
    """

    def __init__(self, allow_private: bool = False, slots_db: Optional[Path] = None):
        self.allow_private = allow_private
        self.sessions: dict = {}
        self.locks: dict = {}
        self.slots: dict = {}  # host -> machine-wide slot key
        self._slots_db = slots_db
        self._cache = None

    def lock(self, host: str) -> asyncio.Lock:
        return self.locks.setdefault(host, asyncio.Lock())

    def _slot_cache(self):
        if self._cache is None:
            from ..cache import Cache

            self._cache = Cache(self._slots_db or data_dir() / "cache" / "cache.db")
        return self._cache

    async def _take_slot(self, host: str) -> str:
        """Wait for a free machine-wide browser slot, evicting our own idle browsers if needed."""
        cache = self._slot_cache()
        deadline = asyncio.get_running_loop().time() + SLOT_WAIT
        while True:
            for i in range(MAX_BROWSERS):
                key = f"browser:slot:{i}"
                if cache.try_acquire(key, SLOT_LEASE, reentrant=False):
                    return key
            idle = [h for h in self.sessions if h != host and not self.lock(h).locked()]
            if idle:
                await self.discard(idle[0])
                continue
            if asyncio.get_running_loop().time() > deadline:
                raise FetchError("busy", f"all {MAX_BROWSERS} browser slots are in use; try again shortly",
                                 transient=True)
            await asyncio.sleep(0.5)

    async def _session(self, host: str, exe: Path) -> _Session:
        if host in self.sessions:
            return self.sessions[host]
        from camoufox.async_api import AsyncCamoufox

        opts = await asyncio.to_thread(_launch_options, host, exe)
        opts["firefox_user_prefs"] = {**opts["firefox_user_prefs"], **PROXY_PREFS}
        proxy = await EgressProxy(self.allow_private).__aenter__()
        opts["proxy"] = {"server": proxy.server, "bypass": ""}
        manager = AsyncCamoufox(from_options=opts, persistent_context=True)
        try:
            context = await manager.__aenter__()
            guard = _Guard(self.allow_private)
            await context.route("**/*", guard.route)
            await context.route_web_socket("**/*", guard.ws_route)
        except BaseException:
            await proxy.__aexit__(None, None, None)
            raise
        self.sessions[host] = _Session(proxy, manager, context, guard)
        return self.sessions[host]

    async def discard(self, host: str) -> None:
        session = self.sessions.pop(host, None)
        if session:
            await session.close()
        key = self.slots.pop(host, None)
        if key:
            self._slot_cache().release(key)

    async def fetch(self, url: str) -> dict:
        exe = installed_executable()
        if not exe:
            raise FetchError("browser_not_installed",
                             "Camoufox browser is not installed; run: agent-web-fetch install-browser")
        from playwright.async_api import Error as PlaywrightError

        host = urlsplit(url).hostname
        async with self.lock(host):
            if host not in self.slots:
                self.slots[host] = await self._take_slot(host)
            for key in self.slots.values():  # keep every slot this pool holds alive
                self._slot_cache().renew(key, SLOT_LEASE)
            try:
                session = await asyncio.wait_for(self._session(host, exe), timeout=HARD_TIMEOUT)
                return await asyncio.wait_for(_browse(url, session, PlaywrightError), timeout=HARD_TIMEOUT)
            except asyncio.TimeoutError:
                await self.discard(host)
                raise FetchError("timeout", f"browser fetch exceeded {HARD_TIMEOUT:.0f}s", transient=True)
            except PlaywrightError as e:
                await self.discard(host)  # browser likely crashed; relaunch on next use
                raise FetchError("browser_error", f"browser failed: {str(e).splitlines()[0][:200]}")
            except BaseException:
                if host not in self.sessions:  # launch failed or was cancelled: give the slot back
                    await self.discard(host)
                raise

    async def close(self) -> None:
        for host in list(self.sessions) + list(self.slots):
            await self.discard(host)
        if self._cache is not None:
            self._cache.close()
            self._cache = None


async def fetch(url: str, allow_private: bool = False, pool: Optional[BrowserPool] = None) -> dict:
    """Fetch one page. Without a pool, the browser is launched and closed for this call."""
    if pool is not None:
        return await pool.fetch(url)
    pool = BrowserPool(allow_private)
    try:
        return await pool.fetch(url)
    finally:
        await pool.close()


async def _browse(url: str, session: _Session, PlaywrightError) -> dict:
    proxy, guard = session.proxy, session.guard
    proxy.blocked.clear()
    guard.blocked.clear()
    page = await session.context.new_page()
    navigations = []

    def on_response(resp):
        if resp.request.is_navigation_request() and resp.frame == page.main_frame:
            navigations.append(resp)

    page.on("response", on_response)
    try:
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightError as e:
            if guard.blocked or proxy.blocked:
                raise FetchError("blocked_address", "page redirected to a non-public address")
            msg = str(e).split("\n", 1)[0]
            if "Timeout" in msg:
                raise FetchError("timeout", "browser navigation timed out", transient=True)
            raise FetchError("browser_error", f"navigation failed: {msg[:200]}")

        # Defence in depth: re-check the redirect chain and the final URL.
        chain = []
        req = response.request if response else None
        while req is not None:
            chain.append(req.url)
            req = req.redirected_from
        for hop in chain + [page.url]:
            if not await guard.allowed(hop):
                raise FetchError("blocked_address", "page redirected to a non-public address")
        if response and response.headers.get("x-agent-web-fetch") == "blocked-address":
            raise FetchError("blocked_address", "page redirected to a non-public address")

        status = response.status if response else None
        if status == 429:
            raise FetchError("rate_limited", "site is rate limiting requests (HTTP 429)", status=429,
                             retry_after=parse_retry_after(await response.header_value("retry-after")))
        try:
            await page.wait_for_load_state("load", timeout=LOAD_GRACE_MS)
        except PlaywrightError:
            pass
        await _settle(page)
        result = await page.evaluate(EXTRACT_JS)
        result = await _wait_for_challenge(page, result, PlaywrightError)
        # A challenge can navigate itself to the page (and change HTTP status).
        response = navigations[-1] if navigations else response
        status = response.status if response else None
        if not await guard.allowed(page.url):
            raise FetchError("blocked_address", "page redirected to a non-public address")
        if response and response.headers.get("x-agent-web-fetch") == "blocked-address":
            raise FetchError("blocked_address", "page redirected to a non-public address")
        if status == 429:
            raise FetchError("rate_limited", "site is rate limiting requests (HTTP 429)", status=429,
                             retry_after=parse_retry_after(await response.header_value("retry-after")))
        final_url = page.url
    finally:
        try:
            await page.close()
        except PlaywrightError:
            pass

    try:
        final_url = canonicalize(final_url)
    except URLRejected:
        final_url = url
    return {"final_url": final_url, "title": result.get("title") or "",
            "content": result.get("markdown") or "", "status": status}


async def _settle(page) -> None:
    """Give dynamic content a short, bounded window to appear (no networkidle)."""
    last = -1
    deadline = asyncio.get_running_loop().time() + SETTLE_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        try:
            n = await page.evaluate("() => document.body ? document.body.innerText.length : 0")
        except Exception:
            return
        if n == last and n > 0:
            return
        last = n
        await asyncio.sleep(0.7)


_PASSIVE_CHALLENGE = re.compile(r"just a moment|checking your browser|checking if the site connection is secure", re.I)
_INTERACTIVE_CHALLENGE = re.compile(r"captcha|verify (that )?you are (a )?human|press (and|&) hold", re.I)


def _passive_challenge(result: dict) -> bool:
    title, body = result.get("title", ""), result.get("markdown", "")
    text = title + " " + body
    return (bool(_PASSIVE_CHALLENGE.search(title)) or
            (len(body) < SHORT_PAGE_CHARS and bool(_PASSIVE_CHALLENGE.search(body)))) and not bool(
                _INTERACTIVE_CHALLENGE.search(text))


async def _wait_for_challenge(page, result: dict, navigation_error=()) -> dict:
    """Observe passive browser checks only; never click or solve a CAPTCHA."""
    if not _passive_challenge(result):
        return result
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CHALLENGE_WAIT_SECONDS
    try:
        while _passive_challenge(result):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.5, remaining))
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                result = await asyncio.wait_for(page.evaluate(EXTRACT_JS), timeout=remaining)
            except navigation_error:
                # A self-clearing check can replace the document mid-extraction.
                continue
    except asyncio.TimeoutError:
        pass  # return the last observed challenge for blocked_by_site classification
    if _passive_challenge(result):
        # Preserve a clear challenge signal even if the title is missing.
        result = dict(result, title="Just a moment")
    return result
