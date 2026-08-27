"""Firestore persistence for canonical invoices.

Documents are keyed by content_hash, which is what makes reprocessing safe: the
same PDF arriving twice - a Pub/Sub redelivery, a retried upload, someone
dragging the same file in again - produces the same key and therefore one
record. Idempotency is a property of the key, not of a check the caller has to
remember to perform.

Money crosses into Firestore as a string, exactly as it crosses every other
wire in this project. Firestore has no decimal type: storing an amount as a
double would silently reintroduce the floating-point error the whole schema
exists to keep away from currency.
"""

from __future__ import annotations

from . import config
from .schema import CanonicalInvoice


def _collection(client=None):
    """Return the invoices collection, building a default client if needed.

    The Firestore import is deferred so that importing this module - which the
    tests and the eval harness do - never requires credentials.
    """
    if client is None:
        from google.cloud import firestore

        client = firestore.Client(project=config.PROJECT_ID)
    return client.collection(config.COLLECTION_INVOICES)


def save_invoice(canonical: CanonicalInvoice, *, client=None) -> tuple[CanonicalInvoice, bool]:
    """Persist an invoice, returning (stored_record, created).

    Uses create() rather than set(), so a second arrival of the same PDF loses
    the race by design instead of overwriting a record that downstream
    anomalies already reference. `created` is False in that case and the
    already-stored record is returned - the caller can then skip the audit
    rather than duplicate it.
    """
    from google.api_core import exceptions

    document = _collection(client).document(canonical.document_id)
    payload = canonical.model_dump(mode="json")

    try:
        document.create(payload)
    except exceptions.AlreadyExists:
        existing = document.get()
        return CanonicalInvoice.model_validate(existing.to_dict()), False

    return canonical, True


def get_invoice(content_hash: str, *, client=None) -> CanonicalInvoice | None:
    """Fetch one canonical invoice by its content hash, or None."""
    snapshot = _collection(client).document(content_hash).get()
    if not snapshot.exists:
        return None
    return CanonicalInvoice.model_validate(snapshot.to_dict())
