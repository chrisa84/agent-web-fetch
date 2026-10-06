from typing import Optional


class FetchError(Exception):
    """A retrieval failure with a stable `category` for callers and logs.

    `transient` marks failures worth one bounded retry (timeouts, 502/503/504).
    `retry_after` is seconds the site asked us to wait, when known.
    """

    def __init__(self, category: str, message: str, status: Optional[int] = None,
                 transient: bool = False, retry_after: Optional[float] = None, scope: Optional[str] = None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.status = status
        self.transient = transient
        self.retry_after = retry_after
        self.scope = scope

    def to_dict(self) -> dict:
        d = {"category": self.category, "message": self.message}
        if self.status is not None:
            d["status"] = self.status
        if self.retry_after is not None:
            d["retry_after"] = round(self.retry_after, 1)
        if self.scope is not None:
            d["scope"] = self.scope
        return d


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header (delta-seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    from email.utils import parsedate_to_datetime
    import time

    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None
