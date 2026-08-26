"""Central configuration for Invoice Sentinel.

Golden rule #6: the model ID lives in exactly one place. Never hardcode a model
name inside an agent — import MODEL_ID from here.

Region rule: infrastructure runs in us-central1, but Gemini calls go to the
`global` endpoint, because the latest Gemini 3.x models are only served there.
See deploy.ps1.
"""

from __future__ import annotations

import os

# --- Model -------------------------------------------------------------------

#: The Gemini model used for multimodal extraction and for agent reasoning.
#: Override per-environment with INVOICE_SENTINEL_MODEL.
MODEL_ID: str = os.environ.get("INVOICE_SENTINEL_MODEL", "gemini-3.5-flash")

# --- Google Cloud ------------------------------------------------------------

PROJECT_ID: str = os.environ.get("GOOGLE_CLOUD_PROJECT", "agent-hackton")

#: Gemini endpoint. Must be "global" for Gemini 3.x — not the infra region.
MODEL_LOCATION: str = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

#: Where Cloud Run, Firestore, GCS and Pub/Sub live.
INFRA_REGION: str = os.environ.get("INVOICE_SENTINEL_REGION", "us-central1")

#: Bucket holding raw invoice PDFs, addressed by content hash.
RAW_INVOICE_BUCKET: str = os.environ.get(
    "INVOICE_SENTINEL_BUCKET", f"{PROJECT_ID}-invoices-raw"
)

# --- Firestore collections ---------------------------------------------------

COLLECTION_INVOICES: str = "invoices"       # canonical invoices, keyed by content_hash
COLLECTION_ANOMALIES: str = "anomalies"     # findings produced by the rule engine
COLLECTION_CONTRACTS: str = "contracts"     # contracted plans, per account
COLLECTION_REVIEWS: str = "review_queue"    # low-confidence findings for a human

# --- Agent behaviour ---------------------------------------------------------

#: Findings below this confidence are escalated to a human instead of disputed.
#: An agent that knows when it is unsure demonstrates judgement; blind
#: automation does not.
ESCALATION_CONFIDENCE_THRESHOLD: float = float(
    os.environ.get("INVOICE_SENTINEL_ESCALATION_THRESHOLD", "0.75")
)

#: How many times the extractor may re-prompt Gemini with a repair message
#: after Pydantic rejects its output.
MAX_EXTRACTION_REPAIRS: int = int(
    os.environ.get("INVOICE_SENTINEL_MAX_REPAIRS", "2")
)

#: Consecutive billing cycles a rule needs before it will claim a pattern.
#: ChronicOverage, PlanTierMismatch and ZombieLine all key off this.
PATTERN_CYCLES: int = int(os.environ.get("INVOICE_SENTINEL_PATTERN_CYCLES", "3"))
