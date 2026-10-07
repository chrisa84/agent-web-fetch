import asyncio
import hashlib
import io
import json
from pathlib import Path
from unittest import mock

from agent_web_fetch import cli, data_dir
from agent_web_fetch import fetch as fm
from agent_web_fetch.backends import FetchError
from agent_web_fetch.backends import camoufox as cf
from agent_web_fetch.cache import Cache
from agent_web_fetch.snapshot import save_snapshot
from helpers import TempHomeCase
from test_fetch import FakeBackend, GOOD
from test_cache import page
from test_batch import FakePool
from test_resilience import Clock

URL = "https://example.com/page"
HTML = '<!DOCTYPE html>\n<html><head><title>Evidence</title></head><body><nav>Full menu</nav>\n<p>5 km/h \u2014 caf\u00e9</p></body></html>'
TEXT = 'Full menu\n5 km/h \u2014 caf\u00e9\r\nExact spacing  and punctuation.'
CAPTURE = {"html": HTML, "text": TEXT, "title": "Evidence", "final_url": URL,
           "captured_at": "2026-10-07T12:34:56.789Z"}


def raw_capture(**kw):
    raw = {"final_url": URL, "status": 200, "title": "Evidence", "content": GOOD,
           "snapshot_data": dict(CAPTURE)}
    raw.update(kw)
    return raw


class SnapshotFilesTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.output = Path(self.home) / "evidence"

    def test_exact_saved_bytes_hashes_metadata_and_absolute_paths(self):
        saved = save_snapshot(self.output, URL + "#section", URL, raw_capture())
        html = Path(saved["html_path"]).read_bytes()
        text = Path(saved["text_path"]).read_bytes()
        meta = json.loads(Path(saved["metadata_path"]).read_bytes())
        self.assertEqual(html, HTML.encode("utf-8"))
        self.assertEqual(text, TEXT.encode("utf-8"))
        self.assertEqual(saved["snapshotHash"], hashlib.sha256(html).hexdigest())
        self.assertEqual(saved["textHash"], hashlib.sha256(text).hexdigest())
        self.assertEqual(meta["snapshotHash"], saved["snapshotHash"])
        self.assertEqual(meta["textHash"], saved["textHash"])
        self.assertEqual(meta["html"]["size_bytes"], len(html))
        self.assertEqual(meta["text"]["size_bytes"], len(text))
        self.assertEqual(meta["html"]["kind"], "rendered_dom")
        self.assertEqual(meta["url"], URL + "#section")
        self.assertEqual(meta["final_url"], URL)
        self.assertEqual(meta["captured_at"], CAPTURE["captured_at"])
        self.assertEqual(meta["status"], 200)
        self.assertFalse(meta["cached"])
        self.assertFalse(meta["stale"])
        self.assertEqual(meta["content_trust"], "untrusted")
        for key in ("html_path", "text_path", "metadata_path"):
            self.assertTrue(Path(saved[key]).is_absolute())
        self.assertEqual(len(list(self.output.iterdir())), 1)

    def test_repeated_captures_never_overwrite_existing_evidence(self):
        first = save_snapshot(self.output, URL, URL, raw_capture())
        second = save_snapshot(self.output, URL, URL, raw_capture())
        self.assertNotEqual(first["metadata_path"], second["metadata_path"])
        self.assertEqual(first["snapshotHash"], second["snapshotHash"])
        self.assertTrue(Path(first["html_path"]).exists())
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_failure_cleans_partial_directory(self):
        original = Path.write_bytes

        def fail_metadata(path, contents):
            if path.name == "metadata.json":
                raise OSError("disk full")
            return original(path, contents)

        with mock.patch.object(Path, "write_bytes", fail_metadata), self.assertRaises(OSError):
            save_snapshot(self.output, URL, URL, raw_capture())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_rename_failure_keeps_existing_files_and_cleans_staging(self):
        first = save_snapshot(self.output, URL, URL, raw_capture())
        with mock.patch("agent_web_fetch.snapshot.os.rename", side_effect=PermissionError), self.assertRaises(OSError):
            save_snapshot(self.output, URL, URL, raw_capture())
        self.assertEqual(len(list(self.output.iterdir())), 1)
        self.assertEqual(Path(first["html_path"]).read_bytes(), HTML.encode())


class SnapshotFetchTests(TempHomeCase):
    def setUp(self):
        super().setUp()
        self.output = Path(self.home) / "snapshots"
        self.browser = mock.AsyncMock(return_value=raw_capture())
        self.jina = FakeBackend()
        for target, fake in ((fm.camoufox_backend, self.browser), (fm.jina_backend, self.jina)):
            patch = mock.patch.object(target, "fetch", fake)
            patch.start()
            self.addCleanup(patch.stop)
        self.cache = Cache(data_dir() / "cache/cache.db")
        self.addCleanup(self.cache.close)

    def run_fetch(self, **kw):
        return asyncio.run(fm.fetch(URL, cache=self.cache, snapshot_dir=str(self.output), **kw))

    def test_auto_uses_fresh_browser_despite_jina_or_browser_cache(self):
        with Clock().patch():
            self.cache.put(page(URL, content="old Jina"))
            first = self.run_fetch()
            second = self.run_fetch()
        self.assertEqual(self.jina.calls, 0)
        self.assertEqual(self.browser.await_count, 2)
        self.assertTrue(self.browser.call_args.kwargs["snapshot"])
        self.assertEqual(first["backend"], "browser")
        self.assertFalse(first["cached"])
        self.assertNotEqual(first["snapshot"]["html_path"], second["snapshot"]["html_path"])
        self.assertEqual(first["snapshotHash"], hashlib.sha256(HTML.encode()).hexdigest())
        self.assertNotIn("snapshot_data", first)

    def test_normal_fetch_remains_cacheable_without_snapshot_capture(self):
        first = asyncio.run(fm.fetch(URL, backend="browser", cache=self.cache))
        second = asyncio.run(fm.fetch(URL, backend="browser", cache=self.cache))
        self.assertEqual(self.browser.await_count, 1)
        self.assertNotIn("snapshot", self.browser.call_args.kwargs)
        self.assertNotIn("snapshot", first)
        self.assertTrue(second["cached"])
        self.assertFalse(self.output.exists())

    def test_snapshot_is_full_even_when_markdown_and_stdout_are_truncated(self):
        full = "Exact evidence\n" * 40000
        raw = raw_capture(content=full, snapshot_data=dict(CAPTURE, text=full, html=HTML + full))
        self.browser.return_value = raw
        result = cli.limit_content(self.run_fetch(), 100)
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["content"]), 300)
        self.assertEqual(Path(result["snapshot"]["text_path"]).read_bytes(), full.encode())
        self.assertEqual(Path(result["snapshot"]["html_path"]).read_bytes(), (HTML + full).encode())

    def test_stale_markdown_cannot_masquerade_as_snapshot(self):
        with Clock().patch() as clock:
            self.cache.put(page(URL, backend="browser", fetched_at=clock.now - 4000))
            self.browser.side_effect = FetchError("network_error", "failed")
            result = self.run_fetch()
        self.assertEqual(result["error"]["category"], "network_error")
        self.assertNotIn("stale", result)
        self.assertNotIn("snapshotHash", result)
        self.assertFalse(self.output.exists())

    def test_holds_and_breakers_prevent_snapshot_browser_call(self):
        with Clock().patch():
            self.cache.set_domain("example.com", 600)
            result = self.run_fetch(refresh=True)
            self.assertEqual(result["error"]["category"], "rate_limited")
            for _ in range(3):
                self.cache.record_outcome("example.com", "browser", 1, blocked=True)
            result = self.run_fetch(refresh=True)
            self.assertEqual(result["error"]["category"], "blocked_by_site")
        self.browser.assert_not_awaited()
        self.assertFalse(self.output.exists())

    def test_negative_cache_is_respected_and_blocked_pages_are_not_saved(self):
        self.browser.return_value = raw_capture(status=403)
        first = self.run_fetch()
        second = self.run_fetch()
        self.assertEqual(first["error"]["category"], "blocked_by_site")
        self.assertTrue(second["cached"])
        self.assertEqual(self.browser.await_count, 1)
        self.assertFalse(self.output.exists())

    def test_challenge_appearing_in_final_capture_is_not_successful_evidence(self):
        self.browser.return_value = raw_capture(snapshot_data=dict(CAPTURE, title="Just a moment", text="Checking your browser"))
        result = self.run_fetch()
        self.assertEqual(result["error"]["category"], "blocked_by_site")
        self.assertNotIn("snapshotHash", result)
        self.assertFalse(self.output.exists())

    def test_write_failure_returns_structured_error_without_site_negative_cache(self):
        self.output.write_text("not a directory")
        result = self.run_fetch()
        self.assertEqual(result["error"]["category"], "snapshot_error")
        self.assertIsNone(result["content"])
        self.assertNotIn("snapshotHash", result)
        self.assertIsNone(self.cache.get_error(URL, "browser"))
        self.assertEqual(self.output.read_text(), "not a directory")
        self.assertIsNotNone(self.cache.get(URL, "browser"))

    def test_missing_capture_fails_explicitly(self):
        raw = raw_capture()
        raw.pop("snapshot_data")
        self.browser.return_value = raw
        result = self.run_fetch()
        self.assertEqual(result["error"]["category"], "snapshot_error")
        self.assertFalse(self.output.exists())

    def test_private_url_rejected_before_capture(self):
        result = asyncio.run(fm.fetch("http://127.0.0.1/private", snapshot_dir=str(self.output)))
        self.assertEqual(result["error"]["category"], "blocked_address")
        self.browser.assert_not_awaited()
        self.assertFalse(self.output.exists())

    def test_explicit_jina_snapshot_rejected(self):
        with self.assertRaises(ValueError):
            self.run_fetch(backend="jina")
        self.assertEqual(self.jina.calls, 0)
        self.browser.assert_not_awaited()

    def test_batch_unique_snapshots_and_pool_option_propagation(self):
        async def capture(url, **kw):
            return raw_capture(final_url=url, snapshot_data=dict(CAPTURE, final_url=url))

        self.browser.side_effect = capture
        urls = [URL, "https://another.example/second", URL]
        with mock.patch.object(fm.camoufox_backend, "BrowserPool", FakePool):
            results = asyncio.run(fm.fetch_many(urls, snapshot_dir=str(self.output)))
        self.assertEqual(self.browser.await_count, 2)
        self.assertEqual(self.jina.calls, 0)
        self.assertEqual(results[0], results[2])
        self.assertNotEqual(results[0]["snapshot"]["html_path"], results[1]["snapshot"]["html_path"])
        for call in self.browser.call_args_list:
            self.assertTrue(call.kwargs["snapshot"])
            self.assertIsInstance(call.kwargs["pool"], FakePool)


class BrowserSnapshotTests(TempHomeCase):
    def browse(self, capture=None, allowed=True, navigates=False, same_url_navigation=False, navigation_status=200):
        page_obj = mock.Mock(url=URL)
        response = mock.Mock(status=200, headers={}, request=None)
        page_obj.goto = mock.AsyncMock(return_value=response)
        page_obj.wait_for_load_state = mock.AsyncMock()
        page_obj.close = mock.AsyncMock()
        extracted = {"title": "Evidence", "markdown": GOOD}
        captured = dict(CAPTURE) if capture is None else capture

        async def evaluate(script):
            if script == cf.SNAPSHOT_JS:
                self.assertEqual(page_obj.close.await_count, 0)
                if navigates:
                    page_obj.url = "https://example.com/new"
                if same_url_navigation:
                    changed_response = mock.Mock(status=navigation_status, headers={}, frame=page_obj.main_frame)
                    changed_response.header_value = mock.AsyncMock(return_value="60")
                    changed_response.request.is_navigation_request.return_value = True
                    page_obj.on.call_args.args[1](changed_response)
                return captured
            return extracted

        page_obj.evaluate = mock.AsyncMock(side_effect=evaluate)
        session = mock.Mock()
        session.context.new_page = mock.AsyncMock(return_value=page_obj)
        # initial URL, final URL, snapshot URL checks
        session.guard.allowed = mock.AsyncMock(side_effect=[True, True, allowed])
        with mock.patch.object(cf, "_settle", mock.AsyncMock()):
            raw = asyncio.run(cf._browse(URL, session, RuntimeError, snapshot=True))
        page_obj.close.assert_awaited_once()
        return raw, page_obj

    def test_browser_captures_full_dom_and_visible_text_before_page_close(self):
        raw, page_obj = self.browse()
        self.assertEqual(raw["snapshot_data"], CAPTURE)
        self.assertEqual(page_obj.evaluate.await_count, 2)

    def test_navigation_during_capture_retries_as_timeout(self):
        with self.assertRaises(FetchError) as error:
            self.browse(navigates=True)
        self.assertEqual(error.exception.category, "timeout")
        self.assertTrue(error.exception.transient)

    def test_same_url_navigation_does_not_mix_old_status_with_new_document(self):
        with self.assertRaises(FetchError) as error:
            self.browse(same_url_navigation=True)
        self.assertEqual(error.exception.category, "timeout")

    def test_rate_limit_during_capture_is_preserved_instead_of_transient_retry(self):
        with self.assertRaises(FetchError) as error:
            self.browse(same_url_navigation=True, navigation_status=429)
        self.assertEqual(error.exception.category, "rate_limited")
        self.assertEqual(error.exception.retry_after, 60)
        self.assertFalse(error.exception.transient)

    def test_snapshot_url_is_revalidated(self):
        with self.assertRaises(FetchError) as error:
            self.browse(allowed=False)
        self.assertEqual(error.exception.category, "blocked_address")

    def test_backend_passes_snapshot_option_to_shared_pool(self):
        pool = mock.Mock()
        pool.fetch = mock.AsyncMock(return_value=raw_capture())
        asyncio.run(cf.fetch(URL, pool=pool, snapshot=True))
        pool.fetch.assert_awaited_once_with(URL, snapshot=True)

    def test_browser_pool_passes_snapshot_option_to_browse(self):
        async def run():
            pool = cf.BrowserPool()
            pool._session = mock.AsyncMock(return_value=mock.Mock())
            try:
                return await pool.fetch(URL, snapshot=True)
            finally:
                pool._cache.close()

        with mock.patch.object(cf, "installed_executable", return_value=Path("fake.exe")), \
                mock.patch.object(cf, "_browse", mock.AsyncMock(return_value=raw_capture())) as browse:
            result = asyncio.run(run())
        self.assertEqual(result["snapshot_data"], CAPTURE)
        self.assertTrue(browse.call_args.kwargs["snapshot"])


class SnapshotCliTests(TempHomeCase):
    def test_cli_json_and_markdown_expose_paths_and_html_hash(self):
        raw = raw_capture()
        saved = save_snapshot(Path(self.home) / "output", URL, URL, raw)
        result = fm._result(URL, final_url=URL, backend="browser", content=GOOD,
                            snapshot=saved, snapshotHash=saved["snapshotHash"])
        for flags in ([], ["--json"]):
            out = io.BytesIO()
            with mock.patch("sys.stdout", mock.Mock(buffer=out)), mock.patch.object(cli, "refuse_privileged"), \
                    mock.patch.object(fm, "fetch", mock.AsyncMock(return_value=result)) as fetch:
                code = cli.main([URL, "--snapshot-dir", str(Path(self.home) / "output")] + flags)
            self.assertEqual(code, 0)
            self.assertEqual(fetch.call_args.kwargs["snapshot_dir"], str(Path(self.home) / "output"))
            text = out.getvalue().decode()
            if flags:
                self.assertEqual(json.loads(text)["snapshotHash"], saved["snapshotHash"])
                self.assertEqual(json.loads(text)["snapshot"]["metadata_path"], saved["metadata_path"])
            else:
                self.assertIn("Snapshot: " + saved["metadata_path"], text)
                self.assertIn("Snapshot SHA-256: " + saved["snapshotHash"], text)

    def test_cli_incompatible_jina_rejected(self):
        with mock.patch.object(cli, "refuse_privileged"), mock.patch("sys.stderr", io.StringIO()), \
                mock.patch.object(fm, "fetch", mock.AsyncMock()) as fetch:
            with self.assertRaises(SystemExit) as error:
                cli.main([URL, "--snapshot-dir", self.home, "--backend", "jina"])
        self.assertEqual(error.exception.code, 2)
        fetch.assert_not_awaited()

    def test_cli_snapshot_write_error_exit_status(self):
        result = fm._result(URL, error={"category": "snapshot_error", "message": "could not save snapshot"})
        with mock.patch("sys.stdout", mock.Mock(buffer=io.BytesIO())), mock.patch.object(cli, "refuse_privileged"), \
                mock.patch.object(fm, "fetch", mock.AsyncMock(return_value=result)):
            self.assertEqual(cli.main([URL, "--snapshot-dir", self.home, "--json"]), 1)
