import asyncio
import json
import io
from unittest import mock

from agent_web_fetch import data_dir
from agent_web_fetch import cli
from agent_web_fetch import fetch as fetch_mod
from agent_web_fetch.cache import Cache

from helpers import TempHomeCase
from test_fetch import FakeBackend


class FakePool:
    instances = []

    def __init__(self, allow_private=False):
        self.closed = False
        FakePool.instances.append(self)

    async def close(self):
        self.closed = True


class BatchTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.jina = FakeBackend()
        self.browser = FakeBackend()
        self.browser_pools = []

        async def browser(url, allow_private=False, pool=None):
            self.browser_pools.append(pool)
            return await self.browser(url)

        FakePool.instances = []
        for target, name, fake in ((fetch_mod.jina_backend, "fetch", self.jina),
                                   (fetch_mod.camoufox_backend, "fetch", browser),
                                   (fetch_mod.camoufox_backend, "BrowserPool", FakePool)):
            p = mock.patch.object(target, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def test_order_preserved_and_duplicates_fetched_once(self):
        urls = ["https://a.example/1", "https://b.example/2", "https://a.example/1", "https://c.example/3"]
        results = asyncio.run(fetch_mod.fetch_many(urls))
        self.assertEqual([r["url"] for r in results], urls)
        self.assertTrue(all(r["error"] is None for r in results))
        self.assertEqual(self.jina.calls, 3)

    def test_browser_shares_one_pool_and_releases_leases(self):
        urls = ["https://a.example/1", "https://a.example/2", "https://b.example/3"]
        results = asyncio.run(fetch_mod.fetch_many(urls, backend="browser"))
        self.assertTrue(all(r["backend"] == "browser" for r in results))
        self.assertEqual(len(FakePool.instances), 1)
        self.assertTrue(all(p is FakePool.instances[0] for p in self.browser_pools))
        self.assertTrue(FakePool.instances[0].closed)
        cache = Cache(data_dir() / "cache" / "cache.db")
        self.addCleanup(cache.close)
        self.assertEqual(cache.db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)
        self.assertEqual(fetch_mod.pool_leases, {})

    def test_one_failure_does_not_stop_batch(self):
        results = asyncio.run(fetch_mod.fetch_many(["https://a.example/1", "http://127.0.0.1/"]))
        self.assertIsNone(results[0]["error"])
        self.assertEqual(results[1]["error"]["category"], "blocked_address")

    def test_cli_batch_json_and_exit_code(self):
        out = io.BytesIO()
        fake_stdout = mock.Mock(buffer=out)
        with mock.patch("sys.stdout", fake_stdout), mock.patch.object(cli, "refuse_privileged"):
            code = cli.main(["https://a.example/1", "http://127.0.0.1/", "--json"])
        data = json.loads(out.getvalue().decode("utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["content_trust"], "untrusted")
        self.assertEqual(data[1]["error"]["category"], "blocked_address")
