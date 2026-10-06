"""Fetch orchestration: validate -> cache -> (dedup, pacing) -> backends -> cache."""

import asyncio
import json
import random
import time
from typing import Optional
from urllib.parse import urlsplit

from . import data_dir
from .backends import FetchError
from .backends import camoufox as camoufox_backend
from .backends import jina as jina_backend
from .cache import DEFAULT_TTL, Cache
from .extract import clean_markdown, clean_title, detect_failure
from .security import URLRejected, validate_url

BACKENDS = ("auto", "jina", "browser")
LEASE_SECONDS = 360.0  # covers both backends, challenge waiting and bounded retries
LOCK_WAIT = 180.0
MAX_POLITE_WAIT = 30.0  # wait at most this long for a domain hold; beyond that, report rate limiting
MIN_DELAY = {"jina": 0.0, "browser": 1.0}
DOMAIN_SLOTS = {"jina": 3, "browser": 1}  # one Firefox profile per domain cannot be shared

BATCH_CONCURRENCY = 6  # total in-flight fetches across all domains in one run
pool_fetch_locks: dict = {}  # serialize per-domain orchestration within a batch
pool_leases: dict = {}  # id(pool) -> browser lease keys held while that pool's browsers are open

# Failures caused by the site itself: briefly cached so an agent cannot hammer it.
NEGATIVE_CACHE = {"blocked_by_site", "not_found", "auth_required", "rate_limited", "http_error",
                  "empty_content", "js_required", "blocked_address"}
# Failures that make a browser retry pointless.
PERMANENT = {"not_found", "blocked_address"}


def _result(url: str, **kw) -> dict:
    r = {"url": url, "final_url": None, "title": None, "backend": None, "cached": False,
         "status": None, "content": None, "error": None}
    r.update(kw)
    return r


def log_event(**fields) -> None:
    """Append operational metadata only: never content, URL paths or credentials."""
    fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        with open(data_dir() / "logs" / "fetch.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(fields) + "\n")
    except OSError:
        pass


async def _acquire(cache: Cache, key: str) -> bool:
    deadline = time.time() + LOCK_WAIT
    delay = 0.25
    while True:
        if cache.try_acquire(key, LEASE_SECONDS):
            return True
        if time.time() > deadline:
            return False
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 2.0)


async def fetch(url: str, backend: str = "auto", refresh: bool = False, allow_private: bool = False,
                cache: Optional[Cache] = None, jina_key: Optional[str] = None, ttl: float = DEFAULT_TTL,
                pool: Optional["camoufox_backend.BrowserPool"] = None,
                no_stale: bool = False, max_wait: float = MAX_POLITE_WAIT) -> dict:
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend}")
    if max_wait < 0 or not max_wait < float("inf"):
        raise ValueError("max_wait must be finite and non-negative")
    started = time.time()
    try:
        canonical = validate_url(url, allow_private)
    except URLRejected as e:
        log_event(host=urlsplit(url).hostname if "://" in url else None, error=e.category, cache="skip")
        return _result(url, error={"category": e.category, "message": e.message})
    host = urlsplit(canonical).hostname
    own_cache = cache is None
    cache = cache or Cache(data_dir() / "cache" / "cache.db")

    def fallback(result):
        if no_stale or (result.get("error") or {}).get("category") not in {
                "rate_limited", "blocked_by_site", "busy", "timeout", "network_error"}:
            return result
        page = cache.get(canonical, backend, ttl=86400)
        if page:
            age = max(0, time.time() - page["fetched_at"])
            if age <= 86400:
                log_event(host=host, backend=page["backend"], cache="stale",
                          error=result["error"]["category"])
                return _result(url, final_url=page["final_url"], title=page["title"], backend=page["backend"],
                               cached=True, status=page["status"], content=page["content"],
                               stale=True, stale_age_seconds=round(age, 1))
        return result

    try:
        if not refresh:
            hit = _from_cache(cache, url, canonical, backend, ttl)
            if hit:
                log_event(host=host, backend=hit["backend"], cache="hit", error=(hit["error"] or {}).get("category"))
                return fallback(hit)

        broken = cache.breaker_wait(host)
        if broken:
            return fallback(_result(url, error={"category": "blocked_by_site", "retry_after": round(broken, 1),
                                                "message": f"circuit breaker holding off {host}"}))
        wait = cache.domain_wait(host)
        if wait > max_wait:
            return fallback(_result(url, error={"category": "rate_limited", "retry_after": round(wait, 1),
                                                "message": f"holding off {host} after rate limiting"}))

        # Dedup: one process per URL at a time; later arrivals reuse its result.
        url_key = f"url:{canonical}"
        if not await _acquire(cache, url_key):
            return fallback(_result(url, error={"category": "busy", "message": "another fetch of this URL is still running"}))
        try:
            hit = _from_cache(cache, url, canonical, backend, ttl, newer_than=started)
            if hit:
                log_event(host=host, backend=hit["backend"], cache="dedup")
                return fallback(hit)
            if pool is not None:
                lock = pool_fetch_locks.setdefault(id(pool), {}).setdefault(host, asyncio.Lock())
                async with lock:
                    result = await _fetch_uncached(cache, url, canonical, host, backend, allow_private,
                                                   jina_key, refresh, pool, max_wait)
            else:
                result = await _fetch_uncached(cache, url, canonical, host, backend, allow_private,
                                               jina_key, refresh, pool, max_wait)
            return fallback(result)
        finally:
            cache.release(url_key)
    finally:
        if own_cache:
            cache.close()


def _from_cache(cache: Cache, url: str, canonical: str, backend: str, ttl: float,
                newer_than: float = 0) -> Optional[dict]:
    page = cache.get(canonical, backend, ttl, newer_than=newer_than)
    if page:
        return _result(url, final_url=page["final_url"], title=page["title"], backend=page["backend"],
                       cached=True, status=page["status"], content=page["content"])
    err = cache.get_error(canonical, backend)
    if err and (not newer_than or err.get("failed_at", 0) >= newer_than):
        return _result(url, cached=True, error=err)
    return None


async def _acquire_slot(cache: Cache, host: str, backend: str) -> Optional[str]:
    """Take one of the per-domain slots for `backend`; returns the lease key."""
    deadline = time.time() + LOCK_WAIT
    while time.time() < deadline:
        for i in range(DOMAIN_SLOTS[backend]):
            key = f"host:{host}:{backend}:{i}"
            if cache.try_acquire(key, LEASE_SECONDS):
                return key
        await asyncio.sleep(0.5)
    return None


async def _fetch_uncached(cache: Cache, url: str, canonical: str, host: str, backend: str,
                          allow_private: bool, jina_key: Optional[str], refresh: bool,
                          pool: Optional["camoufox_backend.BrowserPool"] = None,
                          max_wait: float = MAX_POLITE_WAIT) -> dict:
    broken = cache.breaker_wait(host)
    if broken:
        return _result(url, error={"category": "blocked_by_site", "retry_after": round(broken, 1),
                                   "message": f"circuit breaker holding off {host}"})
    order = ["jina", "browser"] if backend == "auto" else [backend]
    if backend == "auto" and cache.jina_wait() > 0:
        order = ["browser"]
    remaining_wait = max_wait
    attempts = []
    for name in order:
        if name == "jina" and cache.jina_wait() > 0:
            if backend == "auto":
                continue
            return _result(url, error={"category": "rate_limited", "scope": "jina",
                                       "retry_after": round(cache.jina_wait(), 1),
                                       "message": "Jina Reader cooldown is active"})
        broken = cache.breaker_wait(host)
        if broken:
            return _result(url, error={"category": "blocked_by_site", "retry_after": round(broken, 1),
                                       "message": f"circuit breaker holding off {host}"})
        slot = await _acquire_slot(cache, host, name)
        if slot is None:
            attempts.append({"category": "busy", "message": f"too many concurrent fetches for {host}",
                             "backend": name})
            continue
        try:
            # Slots can queue behind a process that just tripped the breaker.
            broken = cache.breaker_wait(host)
            if broken:
                return _result(url, error={"category": "blocked_by_site", "retry_after": round(broken, 1),
                                           "message": f"circuit breaker holding off {host}"})
            if name == "jina" and cache.jina_wait() > 0:
                attempts.append({"category": "rate_limited", "scope": "jina", "backend": "jina",
                                 "retry_after": round(cache.jina_wait(), 1),
                                 "message": "Jina Reader cooldown is active"})
                continue
            for attempt in range(2):
                # Recheck after sleeping: another process may extend the hold.
                while True:
                    wait = cache.domain_wait(host)
                    if wait <= 0:
                        break
                    if wait > remaining_wait:
                        return _result(url, error={"category": "rate_limited", "retry_after": round(wait, 1),
                                                   "message": f"holding off {host} after rate limiting"})
                    t_wait = time.monotonic()
                    # Renew leases during user-selected waits longer than a lease.
                    cache.renew(slot, LEASE_SECONDS)
                    cache.renew(f"url:{canonical}", LEASE_SECONDS)
                    await asyncio.sleep(min(wait, 30.0))
                    remaining_wait = max(0, remaining_wait - (time.monotonic() - t_wait))
                cache.renew(slot, LEASE_SECONDS)
                cache.renew(f"url:{canonical}", LEASE_SECONDS)
                outcome = await _attempt(cache, url, canonical, host, name, allow_private, jina_key, refresh, pool)
                if (name != "browser" or attempt or "url" in outcome or
                        outcome.get("category") in {"blocked_by_site", "blocked_address", "not_found"} or
                        outcome.get("status") in (403, 404) or
                        not (outcome.get("category") == "timeout" or outcome.get("status") in (502, 503, 504))):
                    break
                await asyncio.sleep(random.uniform(2.0, 5.0))
        finally:
            if pool is not None and name == "browser":
                pool_leases.setdefault(id(pool), set()).add(slot)  # held while the pooled browser is open
            else:
                cache.release(slot)
        if "url" in outcome:  # success result
            return outcome
        attempts.append(outcome)
        if outcome["category"] in PERMANENT:
            break

    error = dict(attempts[-1])
    if len(attempts) > 1:
        error["attempts"] = attempts
    if error["category"] == "auth_required":
        error["message"] = "authentication required"
    error["failed_at"] = time.time()
    if error["category"] in NEGATIVE_CACHE:
        cache.put_error(canonical, backend, error)
    return _result(url, error=error)


async def _attempt(cache: Cache, url: str, canonical: str, host: str, name: str, allow_private: bool,
                   jina_key: Optional[str], refresh: bool,
                   pool: Optional["camoufox_backend.BrowserPool"] = None) -> dict:
    """One backend attempt. Returns a result dict on success, else a failure dict."""
    t0 = time.time()
    raw = None
    try:
        if name == "jina":
            raw = await jina_backend.fetch(canonical, api_key=jina_key, refresh=refresh)
        else:
            extra = {"pool": pool} if pool is not None else {}
            raw = await camoufox_backend.fetch(canonical, allow_private=allow_private, **extra)
        title, content = clean_title(raw["title"]), clean_markdown(raw["content"])
        failure = detect_failure(title, content, raw.get("status"), canonical, raw["final_url"],
                                 min_chars=120 if name == "jina" else 1)
        if name == "browser" and raw.get("status") != 429:
            content_failure = detect_failure(title, content, None, canonical, raw["final_url"], min_chars=1)
            if content_failure and content_failure["category"] == "blocked_by_site":
                failure = content_failure  # challenges are never retried, even on a 5xx page
        if failure is None:
            validate_url(raw["final_url"], allow_private)
    except FetchError as e:
        failure = e.to_dict()
    except URLRejected:
        failure = {"category": "blocked_address", "message": "final URL is not a public address"}

    if failure is not None and raw is not None and raw.get("status") is not None:
        failure.setdefault("status", raw["status"])
        if raw.get("retry_after") is not None:
            failure.setdefault("retry_after", raw["retry_after"])
    if failure is not None and failure["category"] != "blocked_address":
        if failure.get("status") == 429:
            failure["category"] = "rate_limited"
        elif name == "browser" and failure.get("status") == 403:
            failure["category"] = "blocked_by_site"
    if failure is not None and failure["category"] == "rate_limited" and name == "jina" and failure.get("scope") == "jina":
        cache.hold_jina(failure.get("retry_after"))
    else:
        cache.record_outcome(host, name, MIN_DELAY[name], success=failure is None,
                             limited=failure is not None and failure["category"] == "rate_limited",
                             retry_after=(failure or {}).get("retry_after"),
                             blocked=failure is not None and (failure["category"] == "blocked_by_site" or
                                                               failure.get("status") == 403))

    latency = int((time.time() - t0) * 1000)
    if failure is None:
        page = {"url": canonical, "final_url": raw["final_url"], "backend": name, "status": raw.get("status"),
                "title": title, "content": content, "fetched_at": time.time()}
        cache.put(page)
        log_event(host=host, backend=name, cache="miss", latency_ms=latency, status=raw.get("status"))
        return _result(url, final_url=page["final_url"], title=title, backend=name,
                       status=page["status"], content=content)

    if name == "browser" and cache.breaker_wait(host) > 0:
        failure["retry_after"] = round(cache.breaker_wait(host), 1)
    failure["backend"] = name
    log_event(host=host, backend=name, cache="miss", latency_ms=latency, status=failure.get("status"),
              error=failure["category"])
    return failure


async def fetch_many(urls: list, backend: str = "auto", refresh: bool = False, allow_private: bool = False,
                     jina_key: Optional[str] = None, ttl: float = DEFAULT_TTL,
                     no_stale: bool = False, max_wait: float = MAX_POLITE_WAIT) -> list:
    """Fetch several URLs in one run, reusing one browser per domain. Results keep input order."""
    cache = Cache(data_dir() / "cache" / "cache.db")
    pool = camoufox_backend.BrowserPool(allow_private)
    gate = asyncio.Semaphore(BATCH_CONCURRENCY)
    unique = list(dict.fromkeys(urls))

    async def one(u):
        async with gate:
            return await fetch(u, backend=backend, refresh=refresh, allow_private=allow_private, cache=cache,
                               jina_key=jina_key, ttl=ttl, pool=pool, no_stale=no_stale, max_wait=max_wait)

    try:
        results = dict(zip(unique, await asyncio.gather(*(one(u) for u in unique))))
        return [results[u] for u in urls]
    finally:
        pool_fetch_locks.pop(id(pool), None)
        await pool.close()
        for key in pool_leases.pop(id(pool), set()):
            cache.release(key)
        cache.close()
