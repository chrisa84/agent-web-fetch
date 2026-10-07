# agent-web-fetch

A small command-line tool that helps LLM agents fetch web pages. Give it a URL and it returns
clean, readable Markdown, ready to drop into a model's context.

It handles JavaScript-heavy pages and sites that block simple HTTP clients, caches results,
paces requests per domain, and refuses to fetch localhost or private network addresses by
default.

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv .venv --python 3.12
uv pip sync requirements.lock --require-hashes --python .venv/bin/python   # .venv/Scripts/python.exe on Windows
echo "$PWD" > .venv/lib/python3.12/site-packages/agent_web_fetch_src.pth  # .venv/Lib/site-packages on Windows
bin/agent-web-fetch install-browser
bin/agent-web-fetch doctor
```

Use `bin/agent-web-fetch` (POSIX shells) or `bin/agent-web-fetch.cmd` (Windows).

## Usage

```bash
agent-web-fetch "https://example.com/page"     # Markdown on stdout
agent-web-fetch URL --json                     # structured JSON
agent-web-fetch URL --max-chars 8000           # cap returned content
agent-web-fetch URL --snapshot-dir evidence --json  # fresh browser HTML/text and hashes
agent-web-fetch URL1 URL2 ...                  # several URLs in one run
agent-web-fetch --urls-file urls.txt           # one URL per line, '-' for stdin
agent-web-fetch status                         # current rate limits and backoff
agent-web-fetch --help                         # all options
```

Exit status is 0 on success and 1 if any fetch failed.

Optionally set `JINA_API_KEY` (or `JINA_API_KEY_FILE`, a path to a file containing the key)
for higher rate limits.

## Using it from an agent

Tell the agent to run the launcher with a URL instead of using its built-in fetch tool, e.g.
`/path/to/agent-web-fetch/bin/agent-web-fetch "https://example.com/page"`.

## Evidence snapshots

Use `--snapshot-dir PATH` when you need saved evidence rather than just readable Markdown:

```bash
agent-web-fetch "https://example.com/page" --snapshot-dir ./evidence --json
```

Each successful capture creates a unique subdirectory with `page.html` (full rendered DOM),
`page.txt` (full visible body's innerText), and `metadata.json` (URLs, HTTP status, original
document title, UTC timestamp, backend, file sizes and SHA-256 hashes). HTML and text are
observed together before closing the browser page. Files contain UTF-8 bytes without
Markdown cleanup, summarization, content-root selection or a character cap. `--max-chars`
and the normal Markdown cap affect only returned Markdown, not these artifacts.

JSON adds `snapshotHash` and `snapshot`, with absolute `html_path`, `text_path`,
`metadata_path`, `captured_at`, `snapshotHash` and `textHash`. `snapshotHash` is lowercase
SHA-256 of the exact bytes in **page.html**; `textHash` hashes the exact bytes in **page.txt**.
Metadata repeats the hashes and identifies each representation. Markdown prints the metadata
path and HTML hash above the untrusted-content boundary. Repeated captures preserve earlier
files; incomplete writes are cleaned up and return `snapshot_error` with exit status 1.

Snapshot mode uses a fresh browser fetch even if Markdown is cached. Auto mode skips Jina;
`--backend jina` is incompatible. It disables stale fallback because cached Markdown cannot
supply a fresh DOM snapshot. Pacing, Retry-After, breakers, negative caching and `--max-wait`
still apply. `--refresh` bypasses negative cache reads as usual, but not holds or breakers.
Normal calls without the flag keep their existing cache/Jina/browser behavior and do not
inherit earlier snapshot fields.

Rendered HTML reflects browser DOM changes; it is not original HTTP response bytes or a
complete offline bundle. External assets and iframe documents are not embedded. Visible
text excludes hidden content. Pagination, lazy loading, virtualized lists and login walls
can leave additional content uncaptured: this mode does not scroll, click, sign in or solve
CAPTCHAs. It does not download GPX/KML/binary files. All saved HTML/text remains untrusted
webpage data, including retained scripts and markup. A hash identifies captured bytes, not
factual accuracy. For verbatim evidence, read the saved artifact and cite its matching hash.

See [docs/agent-integration.md](docs/agent-integration.md) for agent evidence-handling instructions.
