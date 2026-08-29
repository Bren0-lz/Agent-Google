"""Tools the auditor uses to act on findings.

The division of labour here is the whole architecture in miniature. The rule
engine computes every number. These tools let the agent decide what to *do*
with each finding - dispute it, send it to a person, or drop it - and record
that decision with the reasoning behind it.

Nothing in this module accepts a monetary amount as an argument. A tool signed
`flag_anomaly(finding_id, rationale)` cannot be talked into disputing R$ 4.000
that nobody computed; a tool signed `flag_anomaly(line_id, amount, ...)` can,
and would put the project's central guarantee in the hands of a prompt. The
agent names a finding the engine produced and says why; the amount comes from
the engine's own record, every time.

Finding ids are scoped to one invoice - "zombie_line:11987650103" - because
that is the scope an audit runs in, and a short id the model can hold in its
head is less likely to be mangled than a hash.
"""

from __future__ import annotations

from decimal import Decimal

from google.adk.tools import ToolContext

from . import config, store
from .anomaly import Anomaly, Remedy

# --- Session state keys ------------------------------------------------------

#: Written by the extractor: the CanonicalInvoice under audit.
STATE_INVOICE = "canonical_invoice"
#: Written by the extractor: Firestore document id of that invoice.
STATE_CONTENT_HASH = "content_hash"
#: Written by the context loader: the contract, or None if none is on file.
STATE_CONTRACT = "contract"
#: Written by the context loader: earlier cycles, oldest first.
STATE_HISTORY = "history"
#: Written by the rule-family agents: {finding_id: anomaly}.
STATE_FINDINGS = "findings"
#: Written by these tools: {finding_id: {action, rationale}}.
STATE_DECISIONS = "decisions"


def finding_id(anomaly: Anomaly) -> str:
    """Short, stable handle for one finding within one invoice."""
    return f"{anomaly.type.value}:{anomaly.line_id or 'account'}"


# --- Internal helpers --------------------------------------------------------


def _findings(tool_context: ToolContext) -> dict:
    return tool_context.state.get(STATE_FINDINGS) or {}


def _resolve(tool_context: ToolContext, id_: str) -> tuple[Anomaly | None, dict | None]:
    """Look up a finding, or explain what the caller could have meant.

    Refusing an unknown id rather than inventing a finding matters more here
    than anywhere else in the codebase: a tool that quietly accepted a made-up
    id would let the agent dispute a charge the engine never examined.
    """
    findings = _findings(tool_context)
    payload = findings.get(id_)
    if payload is None:
        return None, {
            "status": "unknown_finding",
            "error": f"no finding {id_!r} in this audit",
            "available": sorted(findings),
        }
    return Anomaly.model_validate(payload), None


def _record(tool_context: ToolContext, id_: str, action: str, reason: str) -> None:
    decisions = dict(tool_context.state.get(STATE_DECISIONS) or {})
    decisions[id_] = {"action": action, "rationale": reason}
    tool_context.state[STATE_DECISIONS] = decisions


# --- Reading the account -----------------------------------------------------


def get_contract(tool_context: ToolContext) -> dict:
    """The contracted terms for the account under audit.

    Read this before judging a rate or a plan size. The invoice is not a source
    of contractual truth: an invoice that overcharges states the wrong rate as
    fact, which is precisely the error being looked for.

    Returns status "no_contract" when nothing is on file. That is a real answer,
    not a failure - findings that depend on contracted terms cannot be defended
    without it, and should go to a human instead of to the carrier.
    """
    contract = tool_context.state.get(STATE_CONTRACT)
    if not contract:
        return {
            "status": "no_contract",
            "detail": (
                "No contract on file for this account. Rate and plan-sizing "
                "findings cannot be defended without it."
            ),
        }

    return {
        "status": "ok",
        "account_id": contract["account_id"],
        "carrier": contract["carrier"],
        "effective_from": contract["effective_from"],
        "effective_to": contract.get("effective_to"),
        "plans": [
            {
                "plan_name": plan["plan_name"],
                "monthly_rate": plan["monthly_rate"],
                "allowances": plan["allowances"],
            }
            for plan in contract["plans"]
        ],
        "lines": [
            {
                "line_id": line["line_id"],
                "plan_name": line["plan_name"],
                "activated_on": line.get("activated_on"),
                "deactivated_on": line.get("deactivated_on"),
            }
            for line in contract["lines"]
        ],
        "addons": contract["addons"],
    }


def get_usage_history(tool_context: ToolContext) -> dict:
    """Consumption and spend per cycle for this account, oldest first.

    Use it to sanity-check a pattern before disputing it. A line activated
    partway through the window has a short history, and three quiet cycles mean
    something different when there were only three cycles to be quiet in.
    """
    history = tool_context.state.get(STATE_HISTORY) or []
    invoice = tool_context.state.get(STATE_INVOICE)

    cycles = []
    for record in [*history, invoice]:
        if not record:
            continue
        header = record["invoice"]["header"]
        cycles.append(
            {
                "period": header["billing_period_end"][:7],
                "total_amount": header["total_amount"],
                "lines": len(record["invoice"]["service_lines"]),
                "usage": [
                    {
                        "line_id": usage["line_id"],
                        "metric": usage["metric"],
                        "included": usage["included"],
                        "consumed": usage["consumed"],
                        "overage": usage["overage"],
                    }
                    for usage in record["invoice"]["usage_records"]
                ],
            }
        )

    return {
        "status": "ok",
        "cycles_available": len(cycles),
        "cycles": cycles,
        "note": (
            "A pattern rule needs "
            f"{config.PATTERN_CYCLES} consecutive cycles; fewer cycles than that "
            "on file means the pattern could not have been established."
        ),
    }


def get_extraction_warnings(tool_context: ToolContext) -> dict:
    """What the extractor was unsure about on this invoice.

    Soft inconsistencies do not stop an extraction, but they change how much a
    finding is worth trusting. If the printed charges do not add up to the
    printed total, a finding drawn from those charges deserves a person, not a
    letter to the carrier.
    """
    invoice = tool_context.state.get(STATE_INVOICE) or {}
    provenance = invoice.get("provenance", {})
    return {
        "status": "ok",
        "warnings": provenance.get("warnings", []),
        "repair_attempts": provenance.get("attempts", 1),
        "repair_notes": provenance.get("repair_notes", []),
    }


# --- Acting on findings ------------------------------------------------------


def list_findings(tool_context: ToolContext) -> dict:
    """Every finding the rule engine produced for this invoice.

    The amounts shown were computed by the engine from the invoice and the
    contract. They are here to be read and explained - not to be recalculated,
    adjusted or restated. Decide what to do with each one.
    """
    findings = _findings(tool_context)
    decisions = tool_context.state.get(STATE_DECISIONS) or {}

    return {
        "status": "ok",
        "escalation_threshold": config.ESCALATION_CONFIDENCE_THRESHOLD,
        "findings": [
            {
                "finding_id": id_,
                "type": payload["type"],
                "line_id": payload["line_id"],
                "summary": payload["summary"],
                "recovered_amount": payload["recovered_amount"],
                "months_affected": payload["months_affected"],
                "confidence": payload["confidence"],
                "remedy": Anomaly.model_validate(payload).remedy.value,
                "engine_suggests_review": payload["needs_human_review"],
                "evidence": payload["evidence"],
                "decided": decisions.get(id_, {}).get("action"),
            }
            for id_, payload in sorted(findings.items())
        ],
    }


def flag_anomaly(finding_id: str, rationale: str, tool_context: ToolContext) -> dict:
    """Contest a charge with the carrier.

    Only for findings whose remedy is "dispute" - a charge the carrier had no
    right to make. Use when the evidence would hold up if the carrier pushed
    back: the contract says one thing, the invoice says another, and the
    history shows it was not a one-off.

    Findings whose remedy is "optimise" are refused here. Those were billed
    exactly as contracted, and demanding a refund for them would be rejected
    and would weaken the claims that are genuine.

    Args:
        finding_id: id of a finding from list_findings. Not a description.
        rationale: why this evidence supports a dispute, in one or two
            sentences. It is quoted to a human reviewer, so state the reason -
            not the amount, which is already recorded.
    """
    anomaly, error = _resolve(tool_context, finding_id)
    if error:
        return error

    if anomaly.remedy is not Remedy.DISPUTE:
        return {
            "status": "not_disputable",
            "finding_id": finding_id,
            "error": (
                f"{anomaly.type.value} was billed as contracted - the carrier owes "
                "nothing. This is money the customer is wasting on their own plan "
                "configuration."
            ),
            "use_instead": "recommend_account_action",
        }

    _record(tool_context, finding_id, "dispute", rationale)
    return {
        "status": "flagged_for_dispute",
        "finding_id": finding_id,
        "recovered_amount": format(anomaly.recovered_amount, "f"),
        "note": "Amount taken from the rule engine's record, not from this call.",
    }


def recommend_account_action(
    finding_id: str, action: str, rationale: str, tool_context: ToolContext
) -> dict:
    """Recommend a change to the customer's own account.

    For findings billed correctly but costing the customer money: a line nobody
    uses, a plan too large for its consumption, a plan too small for it. The
    carrier is not at fault and there is nothing to contest - the saving comes
    from cancelling, downgrading or upgrading.

    Args:
        finding_id: id of a finding from list_findings.
        action: what the customer should do - "cancel", "downgrade" or
            "upgrade". Say which plan, when the engine identified one.
        rationale: why, in one or two sentences. No amounts.
    """
    anomaly, error = _resolve(tool_context, finding_id)
    if error:
        return error

    if anomaly.remedy is not Remedy.DISPUTE:
        _record(tool_context, finding_id, "optimise", f"{action}: {rationale}")
        return {
            "status": "recommended",
            "finding_id": finding_id,
            "action": action,
            "monthly_saving": format(anomaly.monthly_amount(), "f"),
        }

    return {
        "status": "not_an_optimisation",
        "finding_id": finding_id,
        "error": (
            f"{anomaly.type.value} is a charge the carrier should not have made. "
            "Changing the customer's plan does not recover it."
        ),
        "use_instead": "flag_anomaly",
    }


def escalate_for_review(finding_id: str, reason: str, tool_context: ToolContext) -> dict:
    """Send a finding to a person instead of to the carrier.

    The right call when something is off that the engine cannot see: the
    extraction was shaky, the contract is missing or expired, the line is too
    new for the pattern to mean what it appears to mean. An escalation is not a
    failure - a disputed charge that turns out to be correct costs the customer
    more credibility than a missed one costs them money.

    Args:
        finding_id: id of a finding from list_findings.
        reason: what a reviewer needs to check, specifically.
    """
    anomaly, error = _resolve(tool_context, finding_id)
    if error:
        return error

    _record(tool_context, finding_id, "escalate", reason)

    content_hash = tool_context.state.get(STATE_CONTENT_HASH)
    queued = None
    if content_hash:
        anomaly.needs_human_review = True
        try:
            queued = store.enqueue_review(
                content_hash, anomaly.account_id, anomaly, reason
            )
        except Exception as failure:  # noqa: BLE001 - the decision still stands
            return {
                "status": "escalated_not_persisted",
                "finding_id": finding_id,
                "error": str(failure),
                "note": "Decision recorded in this session but not written to the queue.",
            }

    return {"status": "escalated", "finding_id": finding_id, "review_id": queued}


def dismiss_finding(finding_id: str, reason: str, tool_context: ToolContext) -> dict:
    """Drop a finding without disputing or escalating it.

    Only for findings that are explainable from evidence already in front of
    you - a line the contract shows was cancelled mid-cycle, an add-on the
    contract does entitle after all. If the reason is "probably fine",
    escalate instead: uncertainty belongs with a person.

    Args:
        finding_id: id of a finding from list_findings.
        reason: the evidence that explains the charge.
    """
    _, error = _resolve(tool_context, finding_id)
    if error:
        return error

    _record(tool_context, finding_id, "dismiss", reason)
    return {"status": "dismissed", "finding_id": finding_id}


# --- Reading decisions back --------------------------------------------------


def decided_anomalies(state: dict, action: str) -> list[Anomaly]:
    """Findings the agent settled on one way, for the dispute writer downstream."""
    findings = state.get(STATE_FINDINGS) or {}
    decisions = state.get(STATE_DECISIONS) or {}
    return [
        Anomaly.model_validate(findings[id_])
        for id_, decision in decisions.items()
        if decision["action"] == action and id_ in findings
    ]


def disputed_total(state: dict) -> Decimal:
    """Sum of everything contested with the carrier. Computed, never generated."""
    return sum(
        (anomaly.recovered_amount for anomaly in decided_anomalies(state, "dispute")),
        Decimal(0),
    )


def optimisation_total(state: dict) -> Decimal:
    """Sum of everything the customer can save by changing their own plans."""
    return sum(
        (anomaly.recovered_amount for anomaly in decided_anomalies(state, "optimise")),
        Decimal(0),
    )
