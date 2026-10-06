"""Command-line entry point.

    agent-web-fetch URL [URL ...] [--urls-file FILE] [--json] [--backend auto|jina|browser] [--refresh] [--allow-private-network]
    agent-web-fetch doctor
    agent-web-fetch install-browser
"""

import argparse
import asyncio
import json
import os
import sys

from . import __version__, data_dir, ensure_dirs
from .extract import BOUNDARY_BEGIN, BOUNDARY_END
from .security import refuse_privileged, scrub_environment


def limit_content(r: dict, max_chars: int) -> dict:
    """Trim the returned content (the cache keeps the full page)."""
    content = r.get("content")
    if not max_chars or not content or len(content) <= max_chars:
        return r
    out = dict(r)
    cut = content.rfind("\n", 0, max_chars)
    out["content"] = content[: cut if cut > max_chars // 2 else max_chars].rstrip() + (
        f"\n\n[truncated: {max_chars} of {len(content)} characters shown; "
        "rerun without --max-chars for the full page]")
    out["truncated"] = True
    out["content_length"] = len(content)
    return out


def render_markdown(r: dict) -> str:
    if r["error"]:
        e = r["error"]
        lines = [f"Error: {e['category']}: {e['message']}", f"URL: {r['url']}"]
        if e.get("retry_after") is not None:
            lines.append(f"Retry after: {e['retry_after']}s")
        for a in e.get("attempts", []):
            lines.append(f"- {a['backend']}: {a['category']}: {a['message']}")
        return "\n".join(lines) + "\n"
    head = [f"Source: {r['final_url']}", f"Backend: {r['backend']}" + (" (cached)" if r["cached"] else "")]
    if r.get("stale"):
        head.append(f"Stale: yes (age {r['stale_age_seconds']}s)")
    title = f"# {r['title']}\n\n" if r["title"] else ""
    return "\n".join(head) + f"\n\n{BOUNDARY_BEGIN}\n{title}{r['content']}\n{BOUNDARY_END}\n"


def render_json(r: dict) -> str:
    out = dict(r)
    if out["error"]:
        out["error"] = {k: v for k, v in out["error"].items() if k != "failed_at"}
    else:
        out["content_trust"] = "untrusted"
    return json.dumps(out, ensure_ascii=False, indent=2) + "\n"


def _write(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    refuse_privileged()
    root = ensure_dirs()
    secrets = scrub_environment(str(root / "tmp"))

    if argv[:1] == ["status"]:
        from .cache import Cache
        from .status import render_status

        status_parser = argparse.ArgumentParser(prog="agent-web-fetch status")
        status_parser.add_argument("--json", action="store_true")
        status_args = status_parser.parse_args(argv[1:])
        cache = Cache(root / "cache" / "cache.db")
        try:
            state = cache.discipline_status()
        finally:
            cache.close()
        _write(json.dumps(state, indent=2) + "\n" if status_args.json else render_status(state))
        return 0
    if argv[:1] == ["doctor"]:
        from .doctor import run_doctor

        return run_doctor(secrets)
    if argv[:1] == ["install-browser"]:
        from .backends import FetchError
        from .backends.camoufox import install_browser

        try:
            install_browser()
            return 0
        except FetchError as e:
            print(f"install failed: {e.message}", file=sys.stderr)
            return 1

    p = argparse.ArgumentParser(prog="agent-web-fetch",
                                description="Fetch a public web page as clean Markdown (cache -> Jina -> Camoufox).",
                                epilog="Other commands: 'agent-web-fetch doctor', 'agent-web-fetch install-browser', 'agent-web-fetch status'.")
    p.add_argument("urls", nargs="*", metavar="URL")
    p.add_argument("--urls-file", metavar="FILE", help="read URLs from FILE, one per line ('-' for stdin)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    p.add_argument("--backend", choices=["auto", "jina", "browser"], default="auto")
    p.add_argument("--refresh", action="store_true", help="bypass the cache")
    p.add_argument("--no-stale", action="store_true", help="disable fallback to cached pages up to 24 hours old")
    p.add_argument("--max-wait", type=float, default=30, metavar="N",
                   help="maximum seconds to wait on domain holds (default: 30)")
    p.add_argument("--max-chars", type=int, default=0, metavar="N",
                   help="return at most about N characters of content per page (default: no limit)")
    p.add_argument("--allow-private-network", action="store_true",
                   help="allow localhost/LAN/private addresses (off by default)")
    p.add_argument("--version", action="version", version=f"agent-web-fetch {__version__}")
    args = p.parse_args(argv)

    if args.max_wait < 0 or not args.max_wait < float("inf"):
        p.error("--max-wait must be finite and non-negative")
    urls = list(args.urls)
    if args.urls_file:
        src = sys.stdin if args.urls_file == "-" else open(args.urls_file, encoding="utf-8")
        with src:
            urls += [l.strip() for l in src if l.strip() and not l.lstrip().startswith("#")]
    if not urls:
        p.error("no URL given")

    from .fetch import fetch, fetch_many

    opts = dict(backend=args.backend, refresh=args.refresh, allow_private=args.allow_private_network,
                jina_key=secrets.get("JINA_API_KEY"), no_stale=args.no_stale, max_wait=args.max_wait)
    if len(urls) == 1:
        result = limit_content(asyncio.run(fetch(urls[0], **opts)), args.max_chars)
        _write(render_json(result) if args.json else render_markdown(result))
        return 1 if result["error"] else 0

    results = [limit_content(r, args.max_chars) for r in asyncio.run(fetch_many(urls, **opts))]
    if args.json:
        _write(json.dumps([json.loads(render_json(r)) for r in results], ensure_ascii=False, indent=2) + "\n")
    else:
        _write("\n".join(f"===== {i + 1}/{len(results)} {r['url']} =====\n{render_markdown(r)}"
                         for i, r in enumerate(results)))
    return 1 if any(r["error"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
