"""Jina Reader backend: https://r.jina.ai/<url> returns LLM-ready Markdown.

The target page is fetched by Jina's servers, not this machine. We still
validate the target URL first so internal hostnames are never sent out, and we
validate the final URL Jina reports.
"""

import asyncio
import re
from typing import Optional

import httpx

from .. import USER_AGENT
from ..security import URLRejected, canonicalize
from . import FetchError, parse_retry_after

JINA_ENDPOINT = "https://r.jina.ai/"
TIMEOUT = httpx.Timeout(45.0, connect=10.0)
MAX_ATTEMPTS = 2  # one retry, only for transient failures

TARGET_STATUS = re.compile(r"returned error (\d{3})")


async def fetch(url: str, api_key: Optional[str] = None, refresh: bool = False) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-Retain-Images": "none",
        "X-Timeout": "30",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if refresh:
        headers["X-No-Cache"] = "true"

    # trust_env=False: ignore proxy env vars and ~/.netrc credentials.
    async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
        for attempt in range(MAX_ATTEMPTS):
            try:
                return await _fetch_once(client, url, headers)
            except FetchError as e:
                if not e.transient or attempt == MAX_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(2.0 * (2 ** attempt))
    raise AssertionError("unreachable")


async def _fetch_once(client: httpx.AsyncClient, url: str, headers: dict) -> dict:
    try:
        resp = await client.get(JINA_ENDPOINT + url, headers=headers)
    except httpx.TimeoutException:
        raise FetchError("timeout", "Jina Reader timed out", transient=True)
    except httpx.HTTPError as e:
        raise FetchError("network_error", f"could not reach Jina Reader: {type(e).__name__}", transient=True)

    if resp.status_code == 429:
        raise FetchError("rate_limited", "Jina Reader rate limit reached", status=429,
                         retry_after=parse_retry_after(resp.headers.get("Retry-After")), scope="jina")
    if resp.status_code in (502, 503, 504):
        raise FetchError("upstream_error", f"Jina Reader HTTP {resp.status_code}", status=resp.status_code,
                         transient=True)
    if resp.status_code in (401, 402):
        raise FetchError("jina_auth", f"Jina Reader refused the request (HTTP {resp.status_code})",
                         status=resp.status_code)

    try:
        payload = resp.json()
    except ValueError:
        raise FetchError("jina_error", f"Jina Reader returned non-JSON (HTTP {resp.status_code})",
                         status=resp.status_code)

    data = payload.get("data") if isinstance(payload, dict) else None
    if resp.status_code >= 400 or not isinstance(data, dict):
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("readableMessage") or payload.get("message") or "")
        m = TARGET_STATUS.search(message)
        status = int(m.group(1)) if m else None
        raise FetchError("jina_error", f"Jina Reader could not read the page: {message[:200]}".rstrip(": "),
                         status=status)

    status = 200
    m = TARGET_STATUS.search(str(data.get("warning") or ""))
    if m:
        status = int(m.group(1))

    final_url = data.get("url") or url
    try:
        final_url = canonicalize(final_url)
    except URLRejected:
        final_url = url

    return {
        "final_url": final_url,
        "title": data.get("title") or "",
        "content": data.get("content") or "",
        "status": status,
    }
