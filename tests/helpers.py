import ipaddress
import os
import shutil
import tempfile
import unittest
from unittest import mock

PUBLIC_IP = ipaddress.ip_address("93.184.216.34")


def fake_resolve(mapping=None, default=(PUBLIC_IP,)):
    """Return a replacement for security.resolve backed by a host -> [ip strings] map."""
    mapping = mapping or {}

    def resolve(host, port):
        if host in mapping:
            return [ipaddress.ip_address(a) for a in mapping[host]]
        try:
            return [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            return list(default)

    return resolve


class TempHomeCase(unittest.TestCase):
    """Points AGENT_WEB_FETCH_HOME at a fresh temp dir and stubs DNS."""

    dns = None

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="awf-test-")
        env = mock.patch.dict(os.environ, {"AGENT_WEB_FETCH_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.home, True)
        from agent_web_fetch import ensure_dirs

        ensure_dirs()
        patcher = mock.patch("agent_web_fetch.security.resolve", fake_resolve(self.dns))
        patcher.start()
        self.addCleanup(patcher.stop)
