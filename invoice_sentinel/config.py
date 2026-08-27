"""Central configuration for Invoice Sentinel.

Golden rule #6: the model ID lives in exactly one place. Never hardcode a model
name inside an agent — import MODEL_ID from here.

Region rule: infrastructure runs in us-central1, but Gemini calls go to the
`global` endpoint, because the latest Gemini 3.x models are only served there.
See deploy.ps1.
"""

from __future__ import annotations

import os
from decimal import Decimal

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

#: How many times a single model call is retried after a transient API failure
#: (5xx, rate limit). Separate from MAX_EXTRACTION_REPAIRS on purpose: a repair
#: answers a wrong response, a retry answers no response at all, and letting a
#: passing 500 consume a repair would cut the model's chances at the schema for
#: a reason that has nothing to do with the invoice.
MAX_TRANSIENT_RETRIES: int = int(
    os.environ.get("INVOICE_SENTINEL_MAX_TRANSIENT_RETRIES", "3")
)

#: First backoff pause, in seconds; doubles per retry.
TRANSIENT_RETRY_BACKOFF: float = float(
    os.environ.get("INVOICE_SENTINEL_TRANSIENT_BACKOFF", "2.0")
)

#: Extraction is transcription, not composition. Anything above zero is the
#: model choosing between readings of the same page, which is exactly what we
#: do not want it doing to someone's phone bill.
EXTRACTION_TEMPERATURE: float = float(
    os.environ.get("INVOICE_SENTINEL_EXTRACTION_TEMPERATURE", "0.0")
)

#: Carrier profile assumed when the caller does not name one. See profiles.py —
#: profile_for() still refuses unknown keys rather than guessing.
DEFAULT_PROFILE_KEY: str = os.environ.get(
    "INVOICE_SENTINEL_DEFAULT_PROFILE", "br-vantel-empresas"
)

#: Consecutive billing cycles a rule needs before it will claim a pattern.
#: ChronicOverage, PlanTierMismatch and ZombieLine all key off this.
PATTERN_CYCLES: int = int(os.environ.get("INVOICE_SENTINEL_PATTERN_CYCLES", "3"))

# --- Rule thresholds ---------------------------------------------------------
# Every number here is a judgement call about what counts as evidence, so it
# lives in one visible place rather than buried in a comparison somewhere.

#: Data below this (MB, per cycle) counts as "not really used". Not zero: a
#: dormant SIM still emits background traffic, and a rule that only fires on an
#: exact zero never fires in production.
ZOMBIE_DATA_MB: Decimal = Decimal(os.environ.get("INVOICE_SENTINEL_ZOMBIE_DATA_MB", "50"))

#: Voice minutes below this, per cycle, also count as "not really used".
ZOMBIE_VOICE_MIN: Decimal = Decimal(os.environ.get("INVOICE_SENTINEL_ZOMBIE_VOICE_MIN", "15"))

#: A plan is oversized only if peak consumption stays under this fraction of the
#: allowance in every cycle. Deliberately strict — half-used is not wasteful.
TIER_MAX_UTILISATION: Decimal = Decimal(
    os.environ.get("INVOICE_SENTINEL_TIER_MAX_UTILISATION", "0.30")
)

#: Headroom kept when recommending a smaller plan. Right-sizing someone into a
#: plan they would immediately exceed is worse than leaving them alone.
TIER_HEADROOM: Decimal = Decimal(os.environ.get("INVOICE_SENTINEL_TIER_HEADROOM", "1.25"))

#: Rate differences at or below this are rounding, not drift.
RATE_DRIFT_TOLERANCE: Decimal = Decimal(
    os.environ.get("INVOICE_SENTINEL_RATE_DRIFT_TOLERANCE", "0.01")
)
