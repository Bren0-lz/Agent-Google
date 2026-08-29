"""On-disk cache of extracted invoices, for development only.

Extraction costs a model call and about 25 seconds per invoice. Iterating on the
rule engine, the auditor tools or the dispute writer means re-reading the same
fifteen PDFs over and over, and paying for the same answer every time.

Cached by content_hash, the same key Firestore uses. That is deliberate: a
cached file and a stored document are the same identity, so nothing has to
translate between "the invoice on disk" and "the invoice in the database".

Nothing here ships to the container. Production reads Firestore.
"""

from __future__ import annotations

from pathlib import Path

from invoice_sentinel.schema import CanonicalInvoice


def path_for(cache_dir: Path, content_hash: str) -> Path:
    return cache_dir / f"{content_hash}.json"


def load(cache_dir: Path, content_hash: str) -> CanonicalInvoice | None:
    """Return the cached extraction, or None if it was never taken."""
    target = path_for(cache_dir, content_hash)
    if not target.exists():
        return None
    return CanonicalInvoice.model_validate_json(target.read_text(encoding="utf-8"))


def save(cache_dir: Path, canonical: CanonicalInvoice) -> Path:
    """Write one extraction to the cache, creating the directory if needed."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = path_for(cache_dir, canonical.content_hash)
    target.write_text(canonical.model_dump_json(indent=2), encoding="utf-8")
    return target


def load_all(cache_dir: Path) -> dict[str, CanonicalInvoice]:
    """Everything in the cache, keyed by content hash."""
    if not cache_dir.exists():
        return {}
    cached = {}
    for entry in sorted(cache_dir.glob("*.json")):
        invoice = CanonicalInvoice.model_validate_json(entry.read_text(encoding="utf-8"))
        cached[invoice.content_hash] = invoice
    return cached
