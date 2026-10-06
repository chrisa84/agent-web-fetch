import asyncio
import ipaddress
import socket
import threading
import unittest
from unittest import mock

from agent_web_fetch import security
from agent_web_fetch.security import (EgressProxy, URLRejected, canonicalize, check_host, is_blocked_ip,
                                      validate_url)

from helpers import fake_resolve


class CanonicalizeTests(unittest.TestCase):
    def test_rejects_non_http_schemes(self):
        for url in ["file:///etc/passwd", "ftp://example.com/x", "data:text/html,hi",
                    "javascript:alert(1)", "gopher://example.com/", "unix:///var/run/docker.sock"]:
            with self.subTest(url=url), self.assertRaises(URLRejected) as cm:
                canonicalize(url)
            self.assertEqual(cm.exception.category, "invalid_url")

    def test_rejects_local_paths_and_garbage(self):
        for url in ["/etc/passwd", "C:\\Windows\\win.ini", "", "   ", "example.com/page", "http://"]:
            with self.subTest(url=url), self.assertRaises(URLRejected):
                canonicalize(url)

    def test_rejects_credentials(self):
        with self.assertRaises(URLRejected):
            canonicalize("https://user:pass@example.com/")
        with self.assertRaises(URLRejected):
            canonicalize("https://user@example.com/")

    def test_rejects_control_characters(self):
        with self.assertRaises(URLRejected):
            canonicalize("https://example.com/\r\nHost: evil")

    def test_canonical_form(self):
        self.assertEqual(canonicalize("HTTPS://Example.COM:443/a?b=1#frag"), "https://example.com/a?b=1")
        self.assertEqual(canonicalize("http://example.com:80"), "http://example.com/")
        self.assertEqual(canonicalize("http://example.com:8080/x"), "http://example.com:8080/x")
        self.assertEqual(canonicalize("https://example.com./"), "https://example.com/")

    def test_idna(self):
        self.assertEqual(canonicalize("https://bücher.example/"), "https://xn--bcher-kva.example/")

    def test_ipv6_literal(self):
        self.assertEqual(canonicalize("http://[2606:4700::1]:8080/"), "http://[2606:4700::1]:8080/")


class BlockedIpTests(unittest.TestCase):
    def test_blocked(self):
        for addr in ["127.0.0.1", "127.8.9.10", "::1", "10.0.0.1", "172.16.5.4", "192.168.1.1",
                     "169.254.169.254", "100.64.0.1", "224.0.0.1", "0.0.0.0", "255.255.255.255",
                     "::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::7f00:1", "fd00::1", "fe80::1",
                     "ff02::1", "100.100.100.200", "fd00:ec2::254", "::"]:
            with self.subTest(addr=addr):
                self.assertTrue(is_blocked_ip(ipaddress.ip_address(addr)))

    def test_public_allowed(self):
        for addr in ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111", "::ffff:93.184.216.34"]:
            with self.subTest(addr=addr):
                self.assertFalse(is_blocked_ip(ipaddress.ip_address(addr)))


class CheckHostTests(unittest.TestCase):
    def setUp(self):
        dns = {
            "public.example": ["93.184.216.34"],
            "private.example": ["10.1.2.3"],
            "rebind.example": ["93.184.216.34", "127.0.0.1"],
            "meta.example": ["169.254.169.254"],
        }
        p = mock.patch("agent_web_fetch.security.resolve", fake_resolve(dns))
        p.start()
        self.addCleanup(p.stop)

    def assertBlocked(self, host, allow_private=False):
        with self.assertRaises(URLRejected) as cm:
            check_host(host, 443, allow_private)
        self.assertEqual(cm.exception.category, "blocked_address")

    def test_public_host_ok(self):
        self.assertEqual(check_host("public.example", 443), [ipaddress.ip_address("93.184.216.34")])

    def test_private_resolution_blocked(self):
        self.assertBlocked("private.example")
        self.assertBlocked("meta.example")

    def test_mixed_public_private_answers_blocked(self):
        self.assertBlocked("rebind.example")

    def test_internal_names_blocked_before_dns(self):
        for host in ["localhost", "foo.localhost", "metadata.google.internal", "db.internal", "nas.local",
                     "router.lan", "intranet"]:
            with self.subTest(host=host):
                self.assertBlocked(host)

    def test_ip_literals(self):
        self.assertBlocked("127.0.0.1")
        self.assertBlocked("[::1]")
        self.assertBlocked("169.254.169.254")

    def test_allow_private_network(self):
        check_host("private.example", 443, allow_private=True)
        check_host("localhost", 443, allow_private=True)
        check_host("127.0.0.1", 80, allow_private=True)

    def test_validate_url_end_to_end(self):
        self.assertEqual(validate_url("https://public.example/x#y"), "https://public.example/x")
        with self.assertRaises(URLRejected):
            validate_url("http://private.example/")
        self.assertEqual(validate_url("http://private.example/", allow_private=True), "http://private.example/")


class DnsErrorTests(unittest.TestCase):
    def test_unresolvable(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with self.assertRaises(URLRejected) as cm:
                security.resolve("does-not-exist.example", 443)
        self.assertEqual(cm.exception.category, "dns_error")


class EnvScrubTests(unittest.TestCase):
    def test_scrub_keeps_allowlist_and_captures_key(self):
        env = {"PATH": "/bin", "JINA_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "x", "GITHUB_TOKEN": "y",
               "SSH_AUTH_SOCK": "/tmp/agent", "HTTPS_PROXY": "http://p"}
        with mock.patch.dict("os.environ", env, clear=True):
            kept = security.scrub_environment("/tmp/awf")
            import os

            self.assertEqual(kept, {"JINA_API_KEY": "secret"})
            self.assertNotIn("JINA_API_KEY", os.environ)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", os.environ)
            self.assertNotIn("GITHUB_TOKEN", os.environ)
            self.assertNotIn("SSH_AUTH_SOCK", os.environ)
            self.assertNotIn("HTTPS_PROXY", os.environ)
            self.assertEqual(os.environ["PATH"], "/bin")
            benv = security.browser_env("/tmp/awf")
            self.assertNotIn("JINA_API_KEY", benv)
            self.assertEqual(benv["TEMP"], "/tmp/awf")

    def test_key_file(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp()
        os.write(fd, b"\xef\xbb\xbfkey-from-file\r\n")
        os.close(fd)
        try:
            with mock.patch.dict("os.environ", {"JINA_API_KEY_FILE": path}, clear=True):
                kept = security.scrub_environment(tempfile.gettempdir())
                self.assertEqual(kept, {"JINA_API_KEY": "key-from-file"})
                self.assertNotIn("JINA_API_KEY_FILE", os.environ)
        finally:
            os.unlink(path)


class Canary:
    """A listener on 127.0.0.1 that records whether anything connected."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop:
            try:
                c, _ = self.sock.accept()
                self.hits += 1
                c.close()
            except (socket.timeout, OSError):
                pass

    def close(self):
        self._stop = True
        self.thread.join(1)
        self.sock.close()


class EgressProxyTests(unittest.TestCase):
    def setUp(self):
        self.canary = Canary()
        self.addCleanup(self.canary.close)

    async def _send(self, proxy, raw: bytes) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", proxy.port)
        w.write(raw)
        await w.drain()
        data = await asyncio.wait_for(r.read(4096), 5)
        w.close()
        return data

    def run_proxy(self, raws, allow_private=False):
        async def main():
            async with EgressProxy(allow_private) as proxy:
                out = [await self._send(proxy, raw) for raw in raws]
                return proxy, out

        return asyncio.run(main())

    def test_connect_to_loopback_refused(self):
        raw = f"CONNECT 127.0.0.1:{self.canary.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        proxy, (resp,) = self.run_proxy([raw])
        self.assertTrue(resp.startswith(b"HTTP/1.1 403"))
        self.assertIn(b"blocked-address", resp)
        self.assertEqual(len(proxy.blocked), 1)
        self.assertEqual(self.canary.hits, 0)

    def test_plain_get_to_loopback_and_localhost_refused(self):
        raws = [f"GET http://127.0.0.1:{self.canary.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
                f"GET http://localhost:{self.canary.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode(),
                b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: x\r\n\r\n"]
        proxy, resps = self.run_proxy(raws)
        for resp in resps:
            self.assertTrue(resp.startswith(b"HTTP/1.1 403"), resp)
        self.assertEqual(len(proxy.blocked), 3)
        self.assertEqual(self.canary.hits, 0)

    def test_non_http_absolute_uri_refused(self):
        proxy, (resp,) = self.run_proxy([b"GET ftp://example.com/ HTTP/1.1\r\n\r\n"])
        self.assertTrue(resp.startswith(b"HTTP/1.1 403"))

    def test_allow_private_permits_loopback(self):
        raw = f"CONNECT 127.0.0.1:{self.canary.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode()
        proxy, (resp,) = self.run_proxy([raw], allow_private=True)
        self.assertTrue(resp.startswith(b"HTTP/1.1 200"), resp)
        self.assertEqual(proxy.blocked, [])
        for _ in range(20):
            if self.canary.hits:
                break
            threading.Event().wait(0.05)
        self.assertEqual(self.canary.hits, 1)


if __name__ == "__main__":
    unittest.main()
