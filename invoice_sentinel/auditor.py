"""The auditor: load context, run the rules in parallel, then judge.

Three stages, and the split between them is the point.

    LoadAuditContext    Firestore -> session state. Deterministic.
    rule_engine         the three rule families, concurrently. Deterministic.
    audit_judgment      an LlmAgent deciding what to do with each finding.

The rule families run as plain agents rather than as three LlmAgents each
calling one tool. Three model calls whose only job is to invoke a function
would be tokens spent on nothing, and would dress deterministic work up as
reasoning - the exact confusion this project exists to avoid. They still run
concurrently under a ParallelAgent, so the graph shows what actually happens.

The model earns its place in the third stage, where the question stops being
arithmetic and starts being judgement: this finding is worth R$ 239,60 and the
engine is 0.9 sure - do we put it in front of the carrier, in front of a person,
or neither? That question needs the things the engine cannot see, and it is the
only question here a language model is asked.
"""

from __future__ import annotations

from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent, ParallelAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from . import audit_tools, config, store
from .anomaly import Anomaly
from .audit_tools import (
    STATE_CONTENT_HASH,
    STATE_CONTRACT,
    STATE_FINDINGS,
    STATE_HISTORY,
    STATE_INVOICE,
)
from .contract import Contract
from .rules import AuditContext, RULE_FAMILIES, finalise_findings, run_rule_family
from .schema import CanonicalInvoice


def _event(ctx: InvocationContext, author: str, text: str, state_delta=None) -> Event:
    return Event(
        invocation_id=ctx.invocation_id,
        author=author,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
        actions=EventActions(state_delta=state_delta or {}),
    )


# --- Stage 1: context --------------------------------------------------------


class LoadAuditContext(BaseAgent):
    """Fetch the contract and the earlier cycles this account was billed.

    Both come from Firestore, never from the filesystem and never from the
    invoice itself. The invoice is the document under suspicion; letting it
    supply the terms it is being checked against would make every conformance
    rule vacuous.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        invoice_payload = state.get(STATE_INVOICE)
        if not invoice_payload:
            return

        canonical = CanonicalInvoice.model_validate(invoice_payload)
        account_id = canonical.invoice.header.account_id
        period_end = canonical.invoice.header.billing_period_end

        contract = store.get_contract(account_id)
        history = store.get_history(account_id, before_period=period_end.isoformat())

        notes = [
            f"Account {account_id}, cycle {period_end:%Y-%m}.",
            f"Contract: {'on file' if contract else 'NOT ON FILE'}.",
            f"History: {len(history)} earlier cycle(s).",
        ]
        if canonical.provenance.warnings:
            notes.append(f"Extraction warnings: {len(canonical.provenance.warnings)}.")

        yield _event(
            ctx,
            self.name,
            " ".join(notes),
            state_delta={
                STATE_CONTRACT: contract.model_dump(mode="json") if contract else None,
                STATE_HISTORY: [record.model_dump(mode="json") for record in history],
                STATE_CONTENT_HASH: canonical.content_hash,
            },
        )


# --- Stage 2: the rules ------------------------------------------------------


def _audit_context(state: dict) -> AuditContext | None:
    """Rebuild the engine's view from session state.

    Returns None when the contract is missing: every rule compares the invoice
    against contracted terms, and running them against a fabricated empty
    contract would produce confident findings with nothing behind them.
    """
    invoice_payload = state.get(STATE_INVOICE)
    contract_payload = state.get(STATE_CONTRACT)
    if not invoice_payload or not contract_payload:
        return None

    return AuditContext(
        invoice=CanonicalInvoice.model_validate(invoice_payload).invoice,
        contract=Contract.model_validate(contract_payload),
        history=[
            CanonicalInvoice.model_validate(record).invoice
            for record in state.get(STATE_HISTORY) or []
        ],
    )


def nothing_was_extracted(state) -> bool:
    """Whether this run has an invoice at all.

    A run can reach the auditor with nothing to audit — somebody opened the Web
    UI and said hello. Every stage below checks this and stays silent, because
    the alternative is what the deployed agent used to do: seven stages each
    announcing that they had nothing to do, ending with a judgement that the
    invoice "looks clean". Reporting a clean bill of health for an invoice
    nobody sent is worse than saying nothing at all — it is a false statement
    about a document that does not exist.

    The intake stage has already explained what to attach, so silence here is
    not silence to the user.
    """
    return not state.get(STATE_INVOICE)


class RuleFamilyAgent(BaseAgent):
    """One family of rules. Deterministic, and concurrent with its siblings.

    Writes to its own state key and merges nothing. Three agents running
    concurrently that each read the shared findings dict and write it back
    would lose whichever updates landed while they were working - the classic
    read-modify-write race, and it would silently drop real findings. MergeFindings
    combines them afterwards, when the concurrency is over.
    """

    family: str = ""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        if nothing_was_extracted(state):
            return

        context = _audit_context(state)
        if context is None:
            # An invoice with no contract behind it is worth saying out loud:
            # the account exists and cannot be audited yet.
            yield _event(
                ctx, self.name, f"{self.family}: skipped, no contract on file for this account."
            )
            return

        found = run_rule_family(self.family, context)

        # No review flag and no suppression here: both are decisions about the
        # whole set of findings, and this agent can only see its own family.
        summary = (
            f"{self.family}: {len(found)} finding(s)"
            + (" - " + ", ".join(audit_tools.finding_id(a) for a in found) if found else "")
        )
        yield _event(
            ctx,
            self.name,
            summary,
            state_delta={
                f"{STATE_FINDINGS}_{self.family}": [a.model_dump(mode="json") for a in found]
            },
        )


class MergeFindings(BaseAgent):
    """Combine the families and apply the decisions that span them.

    Suppression is the reason this cannot happen inside a family: a dormant line
    should be cancelled, not moved to a smaller plan, and reporting both claims
    the same money twice. rules.finalise_findings is the same tail run_all_rules
    uses, so the concurrent path and the single-threaded path reach identical
    conclusions - which is what makes the eval harness a valid check on the agent.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        if nothing_was_extracted(state):
            return

        collected = [
            Anomaly.model_validate(payload)
            for family in RULE_FAMILIES
            for payload in state.get(f"{STATE_FINDINGS}_{family}") or []
        ]
        findings = finalise_findings(collected)

        suppressed = len(collected) - len(findings)
        note = f"{len(findings)} finding(s) after merge"
        if suppressed:
            note += f"; {suppressed} suppressed as redundant"
        review = sum(1 for a in findings if a.needs_human_review)
        if review:
            note += f"; {review} below the confidence threshold"

        yield _event(
            ctx,
            self.name,
            note + ".",
            state_delta={
                STATE_FINDINGS: {
                    audit_tools.finding_id(a): a.model_dump(mode="json") for a in findings
                }
            },
        )


def build_rule_engine() -> ParallelAgent:
    """The three families, running concurrently."""
    return ParallelAgent(
        name="rule_engine",
        description="Runs the three deterministic rule families over the invoice.",
        sub_agents=[
            RuleFamilyAgent(
                name=f"rules_{family}",
                description=f"Deterministic {family.replace('_', ' ')} rules.",
                family=family,
            )
            for family in RULE_FAMILIES
        ],
    )


# --- Stage 3: judgement ------------------------------------------------------

JUDGMENT_INSTRUCTION = """\
You are a telecom billing auditor working for the customer, not the carrier.

A deterministic rule engine has already examined this invoice against the signed
contract and the account's earlier cycles. It computed every amount. Your job is
not to find errors or to check its arithmetic - it is to decide what should
happen to each finding.

Start by calling list_findings. If it returns nothing, do not go looking for
something to report - but before you say anything, call get_contract, because
"nothing was found" and "nothing could be checked" are different answers and
only one of them is good news.

  get_contract returns "ok"            The engine compared this invoice against
                                       the contract and the account's earlier
                                       cycles and found nothing. Say the invoice
                                       checks out, and stop.

  get_contract returns "no_contract"   Nothing was checked. Three of the five
                                       rules need contracted terms and were
                                       skipped, so an overcharge would have gone
                                       straight past you. Never call this
                                       invoice clean, correct or in order. Say
                                       plainly that you cannot audit it without
                                       the signed contract, and ask for it: the
                                       person attaches that PDF and says
                                       "contract", and every invoice afterwards
                                       is audited against it. Then stop.

Every finding carries a "remedy" field, and it decides which action applies:

  remedy "dispute"    the carrier billed something it had no right to - an
                      add-on with no entitlement, a rate that does not match
                      the contract. The carrier owes the customer money.
                      -> flag_anomaly

  remedy "optimise"   the carrier billed exactly as contracted. Nothing is owed.
                      The customer is losing money to their own plan
                      configuration - a line nobody uses, a plan too large, a
                      plan too small.
                      -> recommend_account_action, with "cancel", "downgrade"
                         or "upgrade"

Do not confuse the two. Demanding a refund for a plan the customer themselves
chose invites a flat rejection, and it drags the genuine claims down with it.

Two other actions exist for any finding:

  escalate_for_review a person needs to look before anything is sent
  dismiss_finding     evidence in front of you explains the charge

Before deciding, gather what the engine could not see:

  get_contract              is a contract even on file? has it expired? was the
                            line activated recently enough that a three-cycle
                            pattern could not have formed?
  get_usage_history         how many cycles actually exist? a pattern claimed
                            over more cycles than are on file is not a pattern
  get_extraction_warnings   did the printed charges reconcile with the printed
                            total? a finding drawn from figures that did not add
                            up belongs with a person

Escalate rather than act when: there is no contract on file, the extraction
reported warnings touching the affected line, the line is newer than the pattern
being claimed, or the engine already marked the finding for review.

Dismiss only when specific evidence explains the charge. "Probably fine" is not
evidence - escalate instead. A dispute that turns out to be wrong costs the
customer more credibility with their carrier than a missed error costs them
money, and that asymmetry should drive every close call.

Never state, recompute or adjust a monetary amount. The amounts belong to the
engine; you decide what happens to them. Your rationale explains the reasoning,
not the arithmetic.

Finish with a short summary: how many charges you are disputing with the
carrier, how many plan changes you are recommending, how many you sent for
review, how many you dismissed, and the single most important thing a human
should know about this account.
"""


def _skip_judgment_without_an_invoice(callback_context) -> types.Content | None:
    """Keep the model out of a run that has nothing to judge.

    The rule stages can simply not yield; an LlmAgent cannot, because it always
    calls the model, and a model asked to summarise an audit that never happened
    obliges — the deployed agent answered "The invoice looks clean" to someone
    who had only said hello. That sentence is not a harmless no-op: it is a
    clean bill of health for a document nobody sent, produced by the one stage
    in this graph a reader is most likely to believe.

    Returning content here skips the agent's own run entirely, so the tokens are
    not spent either. Empty parts because the intake stage has already said the
    useful thing.
    """
    if nothing_was_extracted(callback_context.state):
        return types.Content(role="model", parts=[])
    return None


def build_judgment_agent() -> LlmAgent:
    return LlmAgent(
        model=config.MODEL_ID,
        name="audit_judgment",
        before_agent_callback=_skip_judgment_without_an_invoice,
        description=(
            "Decides whether each computed finding is disputed, escalated to a "
            "human, or dismissed."
        ),
        instruction=JUDGMENT_INSTRUCTION,
        tools=[
            audit_tools.list_findings,
            audit_tools.get_contract,
            audit_tools.get_usage_history,
            audit_tools.get_extraction_warnings,
            audit_tools.flag_anomaly,
            audit_tools.recommend_account_action,
            audit_tools.escalate_for_review,
            audit_tools.dismiss_finding,
        ],
    )


# --- The auditor ------------------------------------------------------------


class PersistFindings(BaseAgent):
    """Write the findings and the decisions behind them to Firestore.

    Runs after judgement rather than after the rules, so what lands in the
    database is what the auditor concluded, not merely what the engine noticed.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        if nothing_was_extracted(state):
            return

        content_hash = state.get(STATE_CONTENT_HASH)
        findings = state.get(STATE_FINDINGS) or {}
        if not content_hash or not findings:
            yield _event(ctx, self.name, "No findings to persist.")
            return

        disputed = audit_tools.decided_anomalies(state, "dispute")
        optimisations = audit_tools.decided_anomalies(state, "optimise")
        account_id = next(iter(findings.values()))["account_id"]
        saved = store.save_anomalies(
            content_hash,
            account_id,
            [Anomaly.model_validate(payload) for payload in findings.values()],
        )

        yield _event(
            ctx,
            self.name,
            f"Persisted {len(saved)} finding(s). "
            f"Disputing {len(disputed)} with the carrier "
            f"({audit_tools.disputed_total(state)}); "
            f"recommending {len(optimisations)} plan change(s) "
            f"({audit_tools.optimisation_total(state)}).",
        )


def build_auditor() -> SequentialAgent:
    """Context, then rules, then judgement, then persistence."""
    return SequentialAgent(
        name="auditor",
        description=(
            "Audits one extracted invoice against the contract and the account's "
            "history, and decides what to do with each finding."
        ),
        sub_agents=[
            LoadAuditContext(
                name="load_audit_context",
                description="Loads the contract and earlier cycles from Firestore.",
            ),
            build_rule_engine(),
            MergeFindings(
                name="merge_findings",
                description="Combines the rule families and suppresses redundant findings.",
            ),
            build_judgment_agent(),
            PersistFindings(
                name="persist_findings",
                description="Writes findings and decisions to Firestore.",
            ),
        ],
    )
