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
