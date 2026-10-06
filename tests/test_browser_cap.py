import asyncio
from pathlib import Path
from unittest import mock

from agent_web_fetch import cli, data_dir
from agent_web_fetch.backends import FetchError
from agent_web_fetch.backends import camoufox as cf

from helpers import TempHomeCase


class FakeSession:
    async def close(self):
        pass


class CapTests(TempHomeCase):
    """BrowserPool must never run more than MAX_BROWSERS browsers machine-wide."""

    def setUp(self):
        super().setUp()
        self.open = 0
        self.peak = 0

        async def fake_session(pool, host, exe):
            if host not in pool.sessions:
                self.open += 1
                self.peak = max(self.peak, self.open)
                pool.sessions[host] = FakeSession()
            return pool.sessions[host]

        async def fake_browse(url, session, err):
            await asyncio.sleep(0.05)
            return {"final_url": url, "title": "t", "content": "c", "status": 200}

        original_discard = cf.BrowserPool.discard

        async def counting_discard(pool, host):
            if host in pool.sessions:
                self.open -= 1
            await original_discard(pool, host)

        for name, value in (("installed_executable", lambda: Path("fake.exe")), ("_browse", fake_browse)):
            p = mock.patch.object(cf, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("_session", fake_session), ("discard", counting_discard)):
            p = mock.patch.object(cf.BrowserPool, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(cf, "SLOT_WAIT", 5.0)
        p.start()
        self.addCleanup(p.stop)

    def test_cap_holds_across_processes(self):
        async def run():
            async def worker(host):  # one pool per worker, like separate CLI processes
                pool = cf.BrowserPool(slots_db=data_dir() / "cache" / "cache.db")
                try:
                    await pool.fetch(f"https://{host}/")
                    await asyncio.sleep(0.2)  # hold the browser open like a running batch
                finally:
                    await pool.close()

            await asyncio.gather(*(worker(f"h{i}.example") for i in range(7)))

        asyncio.run(run())
        self.assertLessEqual(self.peak, cf.MAX_BROWSERS)
        self.assertEqual(self.open, 0)

    def test_pool_evicts_its_own_idle_browser(self):
        async def run():
            pool = cf.BrowserPool()
            try:
                for i in range(cf.MAX_BROWSERS + 2):
                    await pool.fetch(f"https://h{i}.example/")
                return len(pool.sessions), len(pool.slots)
            finally:
                await pool.close()

        sessions, slots = asyncio.run(run())
        self.assertEqual((sessions, slots), (cf.MAX_BROWSERS, cf.MAX_BROWSERS))
        self.assertLessEqual(self.peak, cf.MAX_BROWSERS)

    def test_busy_when_other_process_holds_every_slot(self):
        async def run():
            hog = cf.BrowserPool()
            for i in range(cf.MAX_BROWSERS):
                await hog.fetch(f"https://hog{i}.example/")
            other = cf.BrowserPool()
            with mock.patch.object(cf, "SLOT_WAIT", 0.3):
                try:
                    await other.fetch("https://waiting.example/")
                finally:
                    await other.close()
                    await hog.close()

        with self.assertRaises(FetchError) as ctx:
            asyncio.run(run())
        self.assertEqual(ctx.exception.category, "busy")


class MaxCharsTests(TempHomeCase):
    def test_truncates_on_line_boundary_and_reports_length(self):
        content = "\n".join(f"line {i} " + "x" * 40 for i in range(100))
        r = {"url": "u", "content": content, "error": None}
        out = cli.limit_content(r, 500)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["content_length"], len(content))
        self.assertLess(len(out["content"]), 650)
        self.assertIn("[truncated: 500 of", out["content"])
        self.assertEqual(r["content"], content)  # original untouched

    def test_short_content_and_zero_limit_unchanged(self):
        r = {"url": "u", "content": "short", "error": None}
        self.assertIs(cli.limit_content(r, 500), r)
        self.assertIs(cli.limit_content({"content": "x" * 900}, 0)["content"], "x" * 900)
