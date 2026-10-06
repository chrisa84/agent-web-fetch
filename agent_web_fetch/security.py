"""URL validation, canonicalisation and SSRF protection.

Every URL is treated as hostile: only http(s), no embedded credentials, and the
host must resolve exclusively to public addresses unless private-network access
is explicitly allowed. Redirect targets go through the same check.
"""

import ipaddress
import os
import socket
import sys
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = {"http", "https"}

# Hostnames that point at internal infrastructure regardless of what DNS says.
BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")

# Explicit list on top of ipaddress.is_global, which already covers RFC1918,
# loopback, link-local (incl. 169.254.169.254), CGNAT, ULA, documentation, etc.
EXTRA_BLOCKED_NETWORKS = [
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud metadata
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IPv6 metadata
]


class URLRejected(Exception):
    """Raised when a URL must not be fetched. `category` is a stable error code."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
        self.message = message


def canonicalize(url: str) -> str:
    """Validate the syntax of `url` and return a canonical form used as cache key."""
    if not isinstance(url, str) or not url.strip():
        raise URLRejected("invalid_url", "empty URL")
    url = url.strip()
    if any(c in url for c in "\r\n\t\x00"):
        raise URLRejected("invalid_url", "URL contains control characters")
    if "://" not in url:
        raise URLRejected("invalid_url", "URL must start with http:// or https://")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise URLRejected("invalid_url", f"scheme '{scheme}' is not allowed (only http/https)")
    if parts.username is not None or parts.password is not None:
        raise URLRejected("invalid_url", "URLs with embedded credentials are not allowed")

    host = parts.hostname
    if not host:
        raise URLRejected("invalid_url", "URL has no host")
    try:
        port = parts.port
    except ValueError:
        raise URLRejected("invalid_url", "invalid port")

    host = host.rstrip(".").lower()
    if not _is_ip_literal(host):
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise URLRejected("invalid_url", "invalid hostname")

    netloc = f"[{host}]" if ":" in host else host
    if port is not None and not (scheme == "http" and port == 80) and not (scheme == "https" and port == 443):
        netloc += f":{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def is_blocked_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True if `ip` is not a plain public unicast address."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_blocked_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None and is_blocked_ip(ip.sixtofour):
            return True
        if ip.teredo is not None:
            return True
        if ip in ipaddress.ip_network("64:ff9b::/96"):  # NAT64 may wrap an internal v4 address
            return is_blocked_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_loopback or ip.is_link_local:
        return True
    if not ip.is_global:
        return True
    return any(ip in net for net in EXTRA_BLOCKED_NETWORKS)


def resolve(host: str, port: int) -> list:
    """Resolve `host` to every address the OS would connect to."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise URLRejected("dns_error", f"could not resolve host: {e}")
    addrs = []
    for info in infos:
        addr = info[4][0].split("%", 1)[0]  # drop IPv6 zone id
        ip = ipaddress.ip_address(addr)
        if ip not in addrs:
            addrs.append(ip)
    if not addrs:
        raise URLRejected("dns_error", "host resolved to no addresses")
    return addrs


def check_host(host: str, port: int, allow_private: bool = False) -> list:
    """Reject internal hostnames and hosts resolving to any non-public address.

    Every resolved address must be public, otherwise a rebinding-style record set
    (one public, one private address) could still reach internal services.
    """
    host = host.strip("[]").rstrip(".").lower()
    if not allow_private:
        if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
            raise URLRejected("blocked_address", f"host '{host}' is an internal name")
        if "." not in host and not _is_ip_literal(host):
            raise URLRejected("blocked_address", f"single-label host '{host}' is not allowed")
    addrs = resolve(host, port)
    if not allow_private:
        for ip in addrs:
            if is_blocked_ip(ip):
                raise URLRejected("blocked_address", f"host '{host}' resolves to a non-public address")
    return addrs


def validate_url(url: str, allow_private: bool = False) -> str:
    """Canonicalise and fully validate a URL (syntax + DNS). Returns the canonical URL."""
    canonical = canonicalize(url)
    parts = urlsplit(canonical)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    check_host(parts.hostname, port, allow_private)
    return canonical


def refuse_privileged() -> None:
    """Exit if running as root/administrator. The browser parses hostile content."""
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.exit("agent-web-fetch refuses to run as root.")
    if sys.platform == "win32":
        try:
            import ctypes

            if ctypes.windll.shell32.IsUserAnAdmin():
                sys.exit("agent-web-fetch refuses to run from an elevated (administrator) shell.")
        except (AttributeError, OSError):
            pass


# Environment variables the process (and therefore the Playwright driver and
# browser) may keep. Everything else -- tokens, cloud credentials, SSH agent
# sockets, proxy settings -- is dropped before any fetch work starts.
ENV_ALLOWLIST = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME",
    "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
    "AGENT_WEB_FETCH_HOME",
}


def scrub_environment(tmp_dir: str) -> dict:
    """Reduce os.environ to the allowlist in place. Returns the removed secrets we need.

    JINA_API_KEY is captured and removed so it never reaches subprocesses.
    """
    kept_secrets = {}
    key = os.environ.get("JINA_API_KEY")
    key_file = os.environ.get("JINA_API_KEY_FILE")
    if not key and key_file:
        try:
            with open(key_file, encoding="utf-8-sig") as f:
                key = f.read().strip()
        except OSError:
            key = None
    if key:
        kept_secrets["JINA_API_KEY"] = key
    for name in list(os.environ):
        if name.upper() not in ENV_ALLOWLIST:
            del os.environ[name]
    os.environ["TEMP"] = os.environ["TMP"] = tmp_dir
    return kept_secrets


def browser_env(tmp_dir: str) -> dict:
    """Minimal explicit environment for the browser process."""
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOWLIST}
    env["TEMP"] = env["TMP"] = tmp_dir
    env["MOZ_CRASHREPORTER_DISABLE"] = "1"
    return env


# --- egress proxy for the browser ----------------------------------------------
# Playwright's request routing does not see redirect hops, so it cannot be the
# SSRF boundary. Instead the browser is forced through this local proxy: it
# resolves every destination itself, rejects non-public addresses, and connects
# to the exact address it checked (which also defeats DNS rebinding). HTTPS is
# tunnelled with CONNECT, so TLS stays end-to-end between browser and site.

import asyncio  # noqa: E402

HEADER_LIMIT = 65536
CONNECT_TIMEOUT = 15.0


class EgressProxy:
    def __init__(self, allow_private: bool = False):
        self.allow_private = allow_private
        self.blocked: list = []
        self._server = None
        self._tasks: set = set()
        self.port = None

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(self._server.wait_closed(), 2)
        except (asyncio.TimeoutError, Exception):
            pass

    @property
    def server(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _open(self, host: str, port: int):
        addrs = await asyncio.to_thread(check_host, host, port, self.allow_private)
        last = None
        for ip in addrs:
            try:
                return await asyncio.wait_for(asyncio.open_connection(str(ip), port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as e:
                last = e
        raise last or OSError("connect failed")

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        upstream = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            if len(head) > HEADER_LIMIT:
                return
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                host = host.strip("[]")
                port = int(port)
                rest = b""
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    return await self._refuse(writer, target)
                host, port = parts.hostname, parts.port or 80
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                headers = [l for l in lines[1:] if l and not l.lower().startswith(
                    ("proxy-", "connection:", "keep-alive:"))]
                rest = (f"{method} {path} {version}\r\n" + "\r\n".join(headers) +
                        "\r\nConnection: close\r\n\r\n").encode("latin-1")
            try:
                up_reader, upstream = await self._open(host, port)
            except URLRejected:
                return await self._refuse(writer, f"{host}:{port}")
            except (OSError, asyncio.TimeoutError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                upstream.write(rest)
            await asyncio.gather(_pipe(reader, upstream), _pipe(up_reader, writer))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError,
                ConnectionError, OSError):
            pass
        finally:
            self._tasks.discard(task)
            for w in (upstream, writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    async def _refuse(self, writer, what: str):
        self.blocked.append(what)
        writer.write(b"HTTP/1.1 403 Forbidden\r\nX-Agent-Web-Fetch: blocked-address\r\n"
                     b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.write_eof()
        except (OSError, RuntimeError, AttributeError):
            pass
