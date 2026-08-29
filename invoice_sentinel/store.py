"""Firestore persistence: canonical invoices, contracts, findings, disputes.

Invoices are keyed by content_hash, which is what makes reprocessing safe: the
same PDF arriving twice - a Pub/Sub redelivery, a retried upload, someone
dragging the same file in again - produces the same key and therefore one
record. Idempotency is a property of the key, not of a check the caller has to
remember to perform.

Money crosses into Firestore as a string, exactly as it crosses every other
wire in this project. Firestore has no decimal type: storing an amount as a
double would silently reintroduce the floating-point error the whole schema
exists to keep away from currency.

Every function takes an optional `client`, so the auditor tools, the eval
harness and the tests can all point at the same code without any of them
reaching for a global.
"""

from __future__ import annotations

from . import config
from .anomaly import Anomaly
from .contract import Contract
from .dispute import Dispute, DisputeStatus
from .schema import CanonicalInvoice


def _client(client=None):
    """The Firestore client, built on demand.

    Deferred so that importing this module - which the tests and the eval
    harness do - never requires credentials.
    """
    if client is not None:
        return client
    from google.cloud import firestore

    return firestore.Client(project=config.PROJECT_ID)


# --- Invoices ----------------------------------------------------------------


def save_invoice(canonical: CanonicalInvoice, *, client=None) -> tuple[CanonicalInvoice, bool]:
    """Persist an invoice, returning (stored_record, created).

    Uses create() rather than set(), so a second arrival of the same PDF loses
    the race by design instead of overwriting a record that downstream
    anomalies already reference. `created` is False in that case and the
    already-stored record is returned - the caller can then skip the audit
    rather than duplicate it.
    """
    from google.api_core import exceptions

    document = (
        _client(client).collection(config.COLLECTION_INVOICES).document(canonical.document_id)
    )

    try:
        document.create(canonical.model_dump(mode="json"))
    except exceptions.AlreadyExists:
        return CanonicalInvoice.model_validate(document.get().to_dict()), False

    return canonical, True


def get_invoice(content_hash: str, *, client=None) -> CanonicalInvoice | None:
    """Fetch one canonical invoice by its content hash, or None."""
    snapshot = (
        _client(client).collection(config.COLLECTION_INVOICES).document(content_hash).get()
    )
    return CanonicalInvoice.model_validate(snapshot.to_dict()) if snapshot.exists else None


def get_history(
    account_id: str, *, before_period: str | None = None, limit: int = 12, client=None
) -> list[CanonicalInvoice]:
    """This account's invoices, oldest first, optionally ending before a period.

    Ordering is by the billing period on the page, not by when the record was
    written. A backfilled cycle uploaded last would otherwise sort as the most
    recent one, and every rule that counts "the trailing N cycles" would be
    reading the wrong window.

    `before_period` excludes the invoice under audit, so the caller can hand the
    result straight to AuditContext.history. Requires the composite index in
    firestore.indexes.json.
    """
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = (
        _client(client)
        .collection(config.COLLECTION_INVOICES)
        .where(filter=FieldFilter("invoice.header.account_id", "==", account_id))
        .order_by("invoice.header.billing_period_end")
    )
    if before_period is not None:
        query = query.end_before({"invoice": {"header": {"billing_period_end": before_period}}})

    invoices = [
        CanonicalInvoice.model_validate(snapshot.to_dict())
        for snapshot in query.limit(limit).stream()
    ]
    # Belt and braces: a stored period that sorts oddly should not silently
    # reorder the audit window.
    invoices.sort(key=lambda record: record.invoice.header.billing_period_end)
    return invoices


# --- Contracts ---------------------------------------------------------------


def save_contract(contract: Contract, *, client=None) -> Contract:
    """Store or replace an account's contract, keyed by account id."""
    _client(client).collection(config.COLLECTION_CONTRACTS).document(
        contract.account_id
    ).set(contract.model_dump(mode="json"))
    return contract


def get_contract(account_id: str, *, client=None) -> Contract | None:
    """The contracted truth for an account, or None if none was ever loaded.

    None is a meaningful answer, not an error: an account with no contract on
    file cannot be audited for rate drift or plan sizing, and the auditor is
    expected to say so rather than fall back to the invoice's own claims.
    """
    snapshot = (
        _client(client).collection(config.COLLECTION_CONTRACTS).document(account_id).get()
    )
    return Contract.model_validate(snapshot.to_dict()) if snapshot.exists else None


# --- Findings ----------------------------------------------------------------


def anomaly_id(content_hash: str, anomaly: Anomaly) -> str:
    """Stable id for a finding: the invoice it came from, plus what it is.

    Deterministic so that re-auditing the same invoice overwrites its findings
    instead of accumulating duplicates next to them.
    """
    return f"{content_hash}:{anomaly.type.value}:{anomaly.line_id or 'account'}"


def save_anomalies(
    content_hash: str, account_id: str, anomalies: list[Anomaly], *, client=None
) -> list[str]:
    """Persist findings for one invoice, returning their document ids."""
    firestore_client = _client(client)
    batch = firestore_client.batch()
    collection = firestore_client.collection(config.COLLECTION_ANOMALIES)

    ids = []
    for anomaly in anomalies:
        document_id = anomaly_id(content_hash, anomaly)
        batch.set(
            collection.document(document_id),
            {
                "content_hash": content_hash,
                "account_id": account_id,
                **anomaly.model_dump(mode="json"),
            },
        )
        ids.append(document_id)

    if ids:
        batch.commit()
    return ids


def get_anomalies(content_hash: str, *, client=None) -> list[Anomaly]:
    """Findings recorded against one invoice."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    snapshots = (
        _client(client)
        .collection(config.COLLECTION_ANOMALIES)
        .where(filter=FieldFilter("content_hash", "==", content_hash))
        .stream()
    )
    findings = []
    for snapshot in snapshots:
        payload = snapshot.to_dict()
        payload.pop("content_hash", None)
        payload.pop("account_id", None)
        findings.append(Anomaly.model_validate(payload))
    return findings


# --- Human review ------------------------------------------------------------


def enqueue_review(
    content_hash: str,
    account_id: str,
    anomaly: Anomaly,
    reason: str,
    *,
    client=None,
) -> str:
    """Put one finding in front of a person, with why it got there.

    The reason is the agent's, in its own words. It is the part a reviewer reads
    first, and the part that makes an escalation useful rather than a shrug.
    """
    document_id = anomaly_id(content_hash, anomaly)
    _client(client).collection(config.COLLECTION_REVIEWS).document(document_id).set(
        {
            "content_hash": content_hash,
            "account_id": account_id,
            "reason": reason,
            "status": "pending",
            **anomaly.model_dump(mode="json"),
        }
    )
    return document_id


def list_reviews(*, account_id: str | None = None, limit: int = 50, client=None) -> list[dict]:
    """The review queue, newest first is not needed - a queue is read whole."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = _client(client).collection(config.COLLECTION_REVIEWS)
    if account_id is not None:
        query = query.where(filter=FieldFilter("account_id", "==", account_id))
    return [snapshot.to_dict() for snapshot in query.limit(limit).stream()]


# --- Disputes ----------------------------------------------------------------


def save_dispute(dispute: Dispute, *, client=None) -> Dispute:
    """Store a drafted dispute, keyed by the invoice it came from.

    set() rather than create(): re-auditing an invoice should replace its
    dispute, not leave two contradictory letters about the same cycle.
    """
    _client(client).collection(config.COLLECTION_DISPUTES).document(
        dispute.document_id
    ).set(dispute.model_dump(mode="json"))
    return dispute


def get_dispute(content_hash: str, *, client=None) -> Dispute | None:
    snapshot = (
        _client(client).collection(config.COLLECTION_DISPUTES).document(content_hash).get()
    )
    return Dispute.model_validate(snapshot.to_dict()) if snapshot.exists else None


def advance_dispute(
    content_hash: str, to_status: DisputeStatus, *, client=None
) -> Dispute:
    """Move a dispute along, refusing transitions that skip a human.

    A blocked dispute failed the amount check, and no status change gets it out
    of that except a person fixing it. A draft cannot jump straight to submitted:
    something addressed to a carrier over the customer's name is approved by a
    person first, and encoding that here means no caller can forget it.
    """
    dispute = get_dispute(content_hash, client=client)
    if dispute is None:
        raise ValueError(f"no dispute stored for {content_hash}")

    allowed = {
        DisputeStatus.DRAFT: {DisputeStatus.APPROVED, DisputeStatus.BLOCKED},
        DisputeStatus.APPROVED: {DisputeStatus.SUBMITTED, DisputeStatus.BLOCKED},
        DisputeStatus.SUBMITTED: set(),
        DisputeStatus.BLOCKED: set(),
    }
    if to_status not in allowed[dispute.status]:
        raise ValueError(
            f"cannot move dispute {content_hash} from {dispute.status.value} "
            f"to {to_status.value}"
        )

    dispute.status = to_status
    return save_dispute(dispute, client=client)


def list_disputes(*, account_id: str | None = None, limit: int = 50, client=None) -> list[dict]:
    """Every dispute, or an account's disputes."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    query = _client(client).collection(config.COLLECTION_DISPUTES)
    if account_id is not None:
        query = query.where(filter=FieldFilter("account_id", "==", account_id))
    return [snapshot.to_dict() for snapshot in query.limit(limit).stream()]
