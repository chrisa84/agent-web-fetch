import asyncio
import contextlib
import io
import json
import sqlite3
from pathlib import Path
from unittest import mock

from agent_web_fetch import cli, data_dir, doctor
from agent_web_fetch import fetch as fm
from agent_web_fetch.backends import FetchError
from agent_web_fetch.backends import camoufox as cf
from agent_web_fetch.backends import jina as jm
import httpx
from agent_web_fetch.cache import Cache
from agent_web_fetch.status import summary
from helpers import TempHomeCase
from test_cache import page
from test_fetch import FakeBackend, GOOD


class Clock:
    def __init__(self):
        self.now = 100000.0
        self.sleeps = []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    @contextlib.contextmanager
    def patch(self):
        with mock.patch("time.time", lambda: self.now), mock.patch("time.monotonic", lambda: self.now), \
                mock.patch("asyncio.sleep", self.sleep):
            yield self


class DisciplineTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.cache = Cache(Path(self.home) / "cache/cache.db")
        self.addCleanup(self.cache.close)

    def test_delay_doubles_decays_and_persists(self):
        with Clock().patch():
            self.cache.record_outcome("e", "browser", 1, limited=True)
            self.assertEqual(self.cache.domain_delay("e", "browser", 1), 2)
            self.cache.record_outcome("e", "browser", 1, limited=True)
            self.assertEqual(self.cache.domain_delay("e", "browser", 1), 4)
            self.cache.record_outcome("e", "browser", 1, success=True)
            other = Cache(self.cache.path)
            try:
                self.assertEqual(other.domain_delay("e", "browser", 1), 3)
                for _ in range(10):
                    other.record_outcome("e", "browser", 1, success=True)
                self.assertEqual(other.domain_delay("e", "browser", 1), 1)
            finally:
                other.close()

    def test_retry_after_and_cap_preserve_long_explicit_hold(self):
        with Clock().patch():
            self.cache.record_outcome("e", "browser", 1, limited=True, retry_after=90)
            self.assertEqual(self.cache.domain_delay("e", "browser", 1), 90)
            self.assertEqual(self.cache.domain_wait("e"), 90)
            self.cache.record_outcome("e", "browser", 1, limited=True, retry_after=1)
            self.assertEqual(self.cache.domain_delay("e", "browser", 1), 180)
            self.cache.record_outcome("e", "browser", 1, limited=True, retry_after=900)
            self.assertEqual(self.cache.domain_delay("e", "browser", 1), 600)
            self.assertEqual(self.cache.domain_wait("e"), 900)

    def test_jina_zero_floor_gets_initial_backoff(self):
        self.cache.record_outcome("e", "jina", 0, limited=True)
        self.assertEqual(self.cache.domain_delay("e", "jina", 0), 10)

    def test_cooldown_persists_and_expires(self):
        with Clock().patch() as clock:
            self.cache.hold_jina(120)
            self.cache.hold_jina(10)
            other = Cache(self.cache.path)
            try:
                self.assertEqual(other.jina_wait(), 120)
                clock.now += 121
                self.assertEqual(other.jina_wait(), 0)
            finally:
                other.close()

    def test_consecutive_blocks_and_success_reset(self):
        with Clock().patch() as clock:
            for _ in range(2):
                self.cache.record_outcome("e", "browser", 1, blocked=True)
            self.assertEqual(self.cache.breaker_wait("e"), 0)
            self.cache.record_outcome("e", "browser", 1)  # non-block interrupts the streak
            self.cache.record_outcome("e", "browser", 1, blocked=True)
            self.assertEqual(self.cache.breaker_wait("e"), 0)
            for _ in range(2):
                self.cache.record_outcome("e", "browser", 1, blocked=True)
            self.assertEqual(self.cache.breaker_wait("e"), 1800)
            self.cache.record_outcome("e", "jina", 0, success=True)
            self.assertEqual(self.cache.breaker_wait("e"), 0)
            for _ in range(3):
                self.cache.record_outcome("e", "browser", 1, blocked=True)
            clock.now += 1801
            self.assertEqual(self.cache.breaker_wait("e"), 0)

    def test_hard_negative_cache_thirty_minutes_other_errors_two(self):
        with Clock().patch() as clock:
            hard = {"category": "blocked_by_site", "backend": "browser"}
            soft = {"category": "http_error", "backend": "browser", "status": 503}
            jina = {"category": "blocked_by_site", "backend": "jina"}
            for url, error in (("hard", hard), ("soft", soft), ("jina", jina)):
                self.cache.put_error(url, "auto", error)
            clock.now += 121
            self.assertIsNotNone(self.cache.get_error("hard", "auto"))
            self.assertIsNone(self.cache.get_error("soft", "auto"))
            self.assertIsNone(self.cache.get_error("jina", "auto"))
            clock.now += 1680
            self.assertIsNone(self.cache.get_error("hard", "auto"))

    def test_hard_block_is_shared_across_cli_modes(self):
        self.cache.put_error("https://example.com/a", "auto",
                             {"category": "blocked_by_site", "backend": "browser"})
        self.assertIsNotNone(self.cache.get_error("https://example.com/a", "browser"))
        self.assertIsNotNone(self.cache.get_error("https://example.com/a", "jina"))

    def test_old_schema_migration_preserves_holds_pages_and_leases(self):
        path = Path(self.home) / "old.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE domains (host TEXT PRIMARY KEY, next_allowed_at REAL NOT NULL DEFAULT 0, "
                   "backoff REAL NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO domains VALUES ('example.com', 200000, 40)")
        db.commit()
        db.close()
        old = Cache(path)
        try:
            old.put(page())
            old.try_acquire("test", 60)
        finally:
            old.close()
        migrated = Cache(path)
        try:
            with Clock().patch():
                self.assertEqual(migrated.domain_wait("example.com"), 100000)
            self.assertEqual(migrated.domain_backoff("example.com"), 40)
            self.assertEqual(migrated.get("https://example.com/")["content"], "body")
            self.assertEqual(migrated.db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 1)
        finally:
            migrated.close()


class ResilienceFetchTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.jina = FakeBackend()
        self.browser = FakeBackend()
        for target, fake in ((fm.jina_backend, self.jina), (fm.camoufox_backend, self.browser)):
            patch = mock.patch.object(target, "fetch", fake)
            patch.start()
            self.addCleanup(patch.stop)

    def run_fetch(self, url="https://example.com/frogs", **kw):
        return asyncio.run(fm.fetch(url, **kw))

    def cache(self):
        cache = Cache(data_dir() / "cache/cache.db")
        self.addCleanup(cache.close)
        return cache

    def test_service_429_sets_global_hold_and_auto_skips_then_expires(self):
        cache = self.cache()
        with Clock().patch() as clock:
            self.jina.error = FetchError("rate_limited", "Jina limit", status=429, retry_after=120, scope="jina")
            result = self.run_fetch(cache=cache)
            self.assertEqual(result["backend"], "browser")
            self.assertEqual(cache.jina_wait(), 120)
            self.assertEqual(cache.domain_delay("example.com", "jina", 0), 0)
            self.jina.error = None
            self.run_fetch("https://other.example/a", cache=cache)
            self.assertEqual(self.jina.calls, 1)
            explicit = self.run_fetch("https://third.example/a", backend="jina", cache=cache)
            self.assertEqual(explicit["error"]["scope"], "jina")
            clock.now += 121
            result = self.run_fetch("https://fourth.example/a", cache=cache)
            self.assertEqual(result["backend"], "jina")
            self.assertEqual(self.jina.calls, 2)

    def test_real_jina_backend_marks_service_limit_scope(self):
        async def run():
            transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "45"}))
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(FetchError) as error:
                    await jm._fetch_once(client, "https://example.com/a", {})
                self.assertEqual(error.exception.to_dict()["scope"], "jina")
                self.assertEqual(error.exception.retry_after, 45)
        asyncio.run(run())

    def test_target_jina_429_holds_domain_without_global_hold(self):
        cache = self.cache()
        with Clock().patch():
            self.jina.result = {"status": 429}
            result = self.run_fetch(cache=cache, backend="jina")
            self.assertEqual(result["error"]["category"], "rate_limited")
            self.assertEqual(cache.jina_wait(), 0)
            self.assertEqual(cache.domain_delay("example.com", "jina", 0), 10)

    def test_target_rate_limit_exception_honors_retry_after(self):
        cache = self.cache()
        with Clock().patch():
            self.jina.error = FetchError("jina_error", "Target returned error 429", status=429, retry_after=50)
            result = self.run_fetch(cache=cache, backend="jina")
            self.assertEqual(result["error"]["category"], "rate_limited")
            self.assertEqual(cache.domain_delay("example.com", "jina", 0), 50)
            self.assertEqual(cache.domain_wait("example.com"), 50)
            self.assertEqual(cache.jina_wait(), 0)

    def test_browser_429_adapts_then_success_decays(self):
        cache = self.cache()
        with Clock().patch() as clock:
            self.browser.error = FetchError("rate_limited", "site limit", status=429, retry_after=4)
            self.run_fetch(cache=cache, backend="browser")
            self.assertEqual(cache.domain_delay("example.com", "browser", 1), 4)
            self.browser.error = None
            clock.now += 5
            self.run_fetch("https://example.com/other", cache=cache, backend="browser")
            self.assertEqual(cache.domain_delay("example.com", "browser", 1), 3)

    def test_breaker_trips_short_circuits_and_recovers_after_expiry(self):
        cache = self.cache()
        with Clock().patch() as clock:
            self.browser.result = {"status": 403}
            for i in range(3):
                result = self.run_fetch(f"https://example.com/{i}", backend="browser", cache=cache)
            self.assertEqual(result["error"]["retry_after"], 1800)
            result = self.run_fetch("https://example.com/new", cache=cache, refresh=True)
            self.assertEqual(result["error"]["category"], "blocked_by_site")
            self.assertEqual(self.browser.calls, 3)
            self.assertEqual(self.jina.calls, 0)
            clock.now += 1801
            self.browser.result = None
            result = self.run_fetch("https://example.com/new", backend="browser", cache=cache)
            self.assertIsNone(result["error"])
            self.assertEqual(cache.breaker_wait("example.com"), 0)
            self.assertEqual(cache.db.execute("SELECT block_count FROM domains").fetchone()[0], 0)

    def test_success_between_blocks_prevents_trip(self):
        cache = self.cache()
        with Clock().patch():
            self.browser.result = {"status": 403}
            for i in range(2):
                self.run_fetch(f"https://example.com/block{i}", backend="browser", cache=cache)
            self.run_fetch("https://example.com/success", cache=cache)  # any backend success
            self.run_fetch("https://example.com/block3", backend="browser", cache=cache)
            self.assertEqual(cache.breaker_wait("example.com"), 0)

    def test_stale_fallback_for_each_allowed_failure_and_negative_cache(self):
        cache = self.cache()
        with Clock().patch() as clock:
            for category in ("rate_limited", "blocked_by_site", "busy", "timeout", "network_error"):
                with self.subTest(category=category):
                    url = f"https://{category}.example/a"
                    cache.put(page(url, fetched_at=clock.now - 80000, content=GOOD))
                    self.jina.error = FetchError(category, "failure")
                    result = self.run_fetch(url, cache=cache, backend="jina")
                    self.assertTrue(result["stale"])
                    self.assertTrue(result["cached"])
                    self.assertEqual(result["stale_age_seconds"], 80000)
                    self.assertIsNone(result["error"])
                    self.assertIn("Stale: yes (age 80000", cli.render_markdown(result))
                    self.assertTrue(json.loads(cli.render_json(result))["stale"])
                    if category in fm.NEGATIVE_CACHE:
                        calls = self.jina.calls
                        self.assertTrue(self.run_fetch(url, cache=cache, backend="jina")["stale"])
                        self.assertEqual(self.jina.calls, calls)

    def test_no_stale_and_age_limit(self):
        cache = self.cache()
        with Clock().patch() as clock:
            url = "https://example.com/frogs"
            self.jina.error = FetchError("timeout", "failure")
            for age, disabled in ((80000, True), (86401, False)):
                cache.put(page(url, fetched_at=clock.now - age))
                result = self.run_fetch(cache=cache, backend="jina", no_stale=disabled)
                self.assertNotIn("stale", result)
                self.assertEqual(result["error"]["category"], "timeout")

    def test_stale_exactly_twenty_four_hours_refresh_and_disallowed_error(self):
        cache = self.cache()
        with Clock().patch() as clock:
            url = "https://example.com/frogs"
            cache.put(page(url, fetched_at=clock.now - 86400))
            self.jina.error = FetchError("network_error", "failure")
            self.assertTrue(self.run_fetch(cache=cache, backend="jina", refresh=True)["stale"])
            self.jina.error = FetchError("not_found", "404", status=404)
            result = self.run_fetch(cache=cache, backend="jina", refresh=True)
            self.assertNotIn("stale", result)

    def test_stale_on_domain_hold_breaker_and_url_busy(self):
        cache = self.cache()
        with Clock().patch() as clock:
            url = "https://example.com/frogs"
            cache.put(page(url, fetched_at=clock.now - 4000))
            cache.set_domain("example.com", 600)
            self.assertTrue(self.run_fetch(cache=cache)["stale"])
            for _ in range(3):
                cache.record_outcome("example.com", "browser", 1, blocked=True)
            self.assertTrue(self.run_fetch(cache=cache)["stale"])
            with mock.patch.object(fm, "_acquire", mock.AsyncMock(return_value=False)):
                self.assertTrue(self.run_fetch(cache=cache)["stale"])
            self.assertEqual((self.jina.calls, self.browser.calls), (0, 0))

    def test_browser_transients_retry_exactly_once_and_backoff(self):
        for status in (None, 502, 503, 504):
            with self.subTest(status=status), Clock().patch() as clock:
                self.browser.calls = 0
                self.browser.error = FetchError("timeout", "timeout") if status is None else None
                self.browser.result = {"status": status} if status else None
                result = self.run_fetch(f"https://s{status}.example/a", backend="browser")
                self.assertIsNotNone(result["error"])
                self.assertEqual(self.browser.calls, 2)
                self.assertEqual(len(clock.sleeps), 1)
                self.assertGreaterEqual(clock.sleeps[0], 2)
                self.assertLessEqual(clock.sleeps[0], 5)

    def test_challenge_on_503_never_retries(self):
        with Clock().patch():
            self.browser.result = {"title": "Just a moment", "content": "Checking your browser", "status": 503}
            result = self.run_fetch(backend="browser")
            self.assertEqual(result["error"]["category"], "blocked_by_site")
            self.assertEqual(self.browser.calls, 1)

    def test_long_domain_wait_renews_leases(self):
        cache = self.cache()
        with Clock().patch() as clock:
            cache.set_domain("example.com", 400)
            with mock.patch.object(cache, "renew", wraps=cache.renew) as renew:
                result = self.run_fetch(cache=cache, max_wait=400)
            self.assertIsNone(result["error"])
            self.assertEqual(sum(clock.sleeps), 400)
            self.assertTrue(all(s <= 30 for s in clock.sleeps))
            self.assertGreater(renew.call_count, 20)

    def test_retry_can_recover(self):
        raw = {"final_url": "https://example.com/frogs", "title": "OK", "content": GOOD, "status": 200}
        fake = mock.AsyncMock(side_effect=[FetchError("timeout", "timeout"), raw])
        with Clock().patch(), mock.patch.object(fm.camoufox_backend, "fetch", fake):
            result = self.run_fetch(backend="browser")
        self.assertIsNone(result["error"])
        self.assertEqual(fake.await_count, 2)

    def test_no_browser_retry_for_blocks_not_found_or_429(self):
        for status, title in ((403, "Blocked"), (404, "Gone"), (200, "Just a moment"), (429, "Limit")):
            with self.subTest(status=status), Clock().patch():
                self.browser.calls = 0
                self.browser.result = {"status": status, "title": title}
                self.run_fetch(f"https://s{status}.example/{title}", backend="browser")
                self.assertEqual(self.browser.calls, 1)

    def test_max_wait_limits_total_hold_wait_and_rechecks_extensions(self):
        cache = self.cache()
        with Clock().patch() as clock:
            cache.set_domain("example.com", 5)
            result = self.run_fetch(cache=cache, max_wait=0)
            self.assertEqual(result["error"]["retry_after"], 5)
            self.assertEqual(clock.sleeps, [])
            result = self.run_fetch(cache=cache, max_wait=5)
            self.assertIsNone(result["error"])
            self.assertEqual(clock.sleeps, [5])
            cache.set_domain("example.com", 5)
            original = clock.sleep

            async def extend(seconds):
                await original(seconds)
                cache.set_domain("example.com", 20)

            with mock.patch("asyncio.sleep", extend):
                result = self.run_fetch("https://example.com/new", cache=cache, max_wait=10)
            self.assertEqual(result["error"]["retry_after"], 20)
            self.assertEqual(self.jina.calls, 1)

    def test_batch_stops_after_three_browser_blocks(self):
        with Clock().patch(), mock.patch.object(fm.camoufox_backend, "BrowserPool") as pool:
            pool.return_value.close = mock.AsyncMock()
            self.browser.result = {"status": 403}
            results = asyncio.run(fm.fetch_many([f"https://example.com/{i}" for i in range(6)], backend="browser"))
            self.assertEqual(self.browser.calls, 3)
            self.assertTrue(all(r["error"]["category"] == "blocked_by_site" for r in results))
            self.assertEqual(fm.pool_fetch_locks, {})


class ChallengeTests(TempHomeCase):
    def browse(self, clear_after=None, interactive=False, status=200, navigation_status=None, evaluation_error=False):
        clock = Clock()
        clock.now = 0
        response = mock.Mock(status=status, headers={}, request=None)
        page_obj = mock.Mock(url="https://example.com/a")
        page_obj.goto = mock.AsyncMock(return_value=response)
        page_obj.wait_for_load_state = mock.AsyncMock()
        page_obj.close = mock.AsyncMock()

        async def evaluate(script):
            if evaluation_error and clock.now == 0.5:
                raise RuntimeError("execution context destroyed by navigation")
            if clear_after is not None and clock.now >= clear_after:
                if navigation_status is not None:
                    final_response = mock.Mock(status=navigation_status, headers={}, frame=page_obj.main_frame)
                    final_response.request.is_navigation_request.return_value = True
                    page_obj.on.call_args.args[1](final_response)
                return {"title": "Jobs", "markdown": GOOD}
            return {"title": "Just a moment", "markdown": "Checking your browser" + (" CAPTCHA" if interactive else "")}

        page_obj.evaluate = mock.AsyncMock(side_effect=evaluate)
        session = mock.Mock()
        session.context.new_page = mock.AsyncMock(return_value=page_obj)
        session.guard.allowed = mock.AsyncMock(return_value=True)
        with mock.patch.object(cf, "_settle", mock.AsyncMock()), mock.patch.object(cf.asyncio, "get_running_loop") as loop, \
                mock.patch.object(cf.asyncio, "sleep", clock.sleep):
            loop.return_value.time = lambda: clock.now
            raw = asyncio.run(cf._browse(page_obj.url, session, RuntimeError))
        failure = fm.detect_failure(raw["title"], raw["content"], raw["status"], min_chars=1)
        return raw, failure, clock, page_obj

    def test_passive_challenge_clears_after_five_seconds(self):
        raw, failure, clock, page_obj = self.browse(clear_after=5)
        self.assertIsNone(failure)
        self.assertEqual(raw["title"], "Jobs")
        self.assertEqual(clock.now, 5)
        page_obj.close.assert_awaited_once()

    def test_self_navigation_updates_initial_403_status(self):
        raw, failure, _, _ = self.browse(clear_after=2, status=403, navigation_status=200, evaluation_error=True)
        self.assertEqual(raw["status"], 200)
        self.assertIsNone(failure)

    def test_challenge_never_clears_returns_block_within_cap(self):
        raw, failure, clock, _ = self.browse()
        self.assertEqual(failure["category"], "blocked_by_site")
        self.assertLessEqual(clock.now, 15)
        self.assertEqual(clock.now, 15)

    def test_interactive_captcha_is_not_waited_on(self):
        _, failure, clock, page_obj = self.browse(interactive=True)
        self.assertEqual(failure["category"], "blocked_by_site")
        self.assertEqual(clock.sleeps, [])
        page_obj.evaluate.assert_awaited_once()

    def test_plain_403_does_not_wait(self):
        _, failure, clock, _ = self.browse(clear_after=0, status=403)
        self.assertEqual(failure["category"], "blocked_by_site")
        self.assertEqual(clock.sleeps, [])


class StatusTests(TempHomeCase):
    def test_status_command_text_json_and_summary(self):
        cache = Cache(data_dir() / "cache/cache.db")
        with Clock().patch():
            cache.record_outcome("paced.example", "browser", 1, limited=True, retry_after=30)
            for _ in range(3):
                cache.record_outcome("blocked.example", "browser", 1, blocked=True)
            cache.hold_jina(90)
            state = cache.discipline_status()
            self.assertIn("1 circuit breakers", summary(state))
            cache.close()
            for flags in ([], ["--json"]):
                out = io.BytesIO()
                with mock.patch("sys.stdout", mock.Mock(buffer=out)), mock.patch.object(cli, "refuse_privileged"):
                    self.assertEqual(cli.main(["status"] + flags), 0)
                text = out.getvalue().decode()
                if flags:
                    self.assertEqual(json.loads(text), state)
                else:
                    for expected in ("paced.example", "blocked.example", "30.0s remaining", "circuit-broken", "backing off", "90.0s remaining"):
                        self.assertIn(expected, text)

    def test_status_expired_states_disappear_except_adaptive_delay(self):
        cache = Cache(data_dir() / "cache/cache.db")
        try:
            with Clock().patch() as clock:
                cache.set_domain("ordinary.example", 1)
                for _ in range(3):
                    cache.record_outcome("broken.example", "browser", 1, blocked=True)
                cache.record_outcome("backoff.example", "browser", 1, limited=True)
                cache.hold_jina(2)
                clock.now += 1801
                state = cache.discipline_status()
                self.assertEqual([d["host"] for d in state["domains"]], ["backoff.example"])
                self.assertEqual(state["domains"][0]["hold_seconds"], 0)
                self.assertEqual(state["jina_cooldown_seconds"], 0)
        finally:
            cache.close()

    def test_cli_forwards_new_flags_and_stale_success_exit_code(self):
        raw = fm._result("https://example.com/a", final_url="https://example.com/a", backend="jina", content=GOOD,
                         stale=True, stale_age_seconds=4000, cached=True)
        out = io.BytesIO()
        with mock.patch("sys.stdout", mock.Mock(buffer=out)), mock.patch.object(cli, "refuse_privileged"), \
                mock.patch.object(fm, "fetch", mock.AsyncMock(return_value=raw)) as fetch:
            self.assertEqual(cli.main([raw["url"], "--no-stale", "--max-wait", "7", "--json"]), 0)
        self.assertTrue(fetch.call_args.kwargs["no_stale"])
        self.assertEqual(fetch.call_args.kwargs["max_wait"], 7)
        self.assertTrue(json.loads(out.getvalue())["stale"])

    def test_doctor_prints_one_line_discipline_summary_without_network(self):
        cache = Cache(data_dir() / "cache/cache.db")
        cache.hold_jina(60)
        cache.close()
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.object(doctor, "version", return_value="test"), \
                mock.patch.object(cf, "installed_executable", return_value=None), \
                mock.patch("httpx.get", return_value=mock.Mock(status_code=200)):
            doctor.run_doctor({})
        lines = [line for line in out.getvalue().splitlines() if "request discipline" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("Jina cooldown", lines[0])

    def test_invalid_max_wait_rejected(self):
        with mock.patch.object(cli, "refuse_privileged"), mock.patch("sys.stderr", io.StringIO()):
            for value in ("-1", "nan", "inf"):
                with self.assertRaises(SystemExit):
                    cli.main(["https://example.com/a", "--max-wait", value])
