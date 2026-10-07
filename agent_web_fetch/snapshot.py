"""Persist browser evidence as exact UTF-8 bytes with content hashes."""

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path


def save_snapshot(directory, url: str, canonical: str, raw: dict) -> dict:
    """Publish a complete capture directory; incomplete writes are cleaned up."""
    captured = raw["snapshot_data"]
    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    capture_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12] + "-" + uuid.uuid4().hex
    destination = root / capture_id
    html = captured["html"].encode("utf-8")
    text = captured["text"].encode("utf-8")
    html_hash = hashlib.sha256(html).hexdigest()
    text_hash = hashlib.sha256(text).hexdigest()
    metadata = {
        "schema_version": 1, "url": url, "canonical_url": canonical,
        "final_url": raw["final_url"], "title": captured["title"],
        "status": raw.get("status"), "backend": "browser",
        "captured_at": captured["captured_at"], "cached": False, "stale": False,
        "content_trust": "untrusted", "hash_algorithm": "sha256",
        "snapshotHash": html_hash, "textHash": text_hash,
        "html": {"file": "page.html", "sha256": html_hash, "size_bytes": len(html),
                 "kind": "rendered_dom", "encoding": "utf-8"},
        "text": {"file": "page.txt", "sha256": text_hash, "size_bytes": len(text),
                 "kind": "visible_body_innerText", "encoding": "utf-8"},
    }
    # Same-filesystem rename publishes all three files together. Unique names
    # keep concurrent requests and repeated captures from overwriting evidence.
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=root) as staging:
        stage = Path(staging)
        (stage / "page.html").write_bytes(html)
        (stage / "page.txt").write_bytes(text)
        (stage / "metadata.json").write_bytes((json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        os.rename(stage, destination)
    return {
        "html_path": str(destination / "page.html"),
        "text_path": str(destination / "page.txt"),
        "metadata_path": str(destination / "metadata.json"),
        "captured_at": captured["captured_at"],
        "snapshotHash": html_hash, "textHash": text_hash,
    }
