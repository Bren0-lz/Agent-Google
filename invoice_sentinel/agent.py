"""Root agent exposed to `adk web` and `adk deploy`.

One invoice in, a defensible dispute out, with nobody in the loop:

    invoice_sentinel                 SequentialAgent
      invoice_extractor              PDF -> canonical schema, Firestore
      auditor                        SequentialAgent
        load_audit_context           contract and history from Firestore
        rule_engine                  ParallelAgent
          rules_unused_capacity      deterministic
          rules_excess_usage         deterministic
          rules_contract_conformance deterministic
        merge_findings               suppression, review flags, ranking
        audit_judgment               LlmAgent - the only judgement call here
        persist_findings             findings and decisions to Firestore
      dispute_writer                 two documents, every figure verified

Two of the three stages that touch a language model are pure transcription and
pure composition. The one place a model is asked to decide anything is
audit_judgment, and it decides what to *do* with amounts it is structurally
unable to author.

State contract for a run: set `source_uri` (and optionally `profile_key`) before
starting, and the extractor takes it from there.
"""

from google.adk.agents import SequentialAgent

from .auditor import build_auditor
from .dispute_writer import DisputeWriterAgent
from .extractor_agent import ExtractorAgent

root_agent = SequentialAgent(
    name="invoice_sentinel",
    description=(
        "Audits a telecom invoice end to end: extracts it, checks it against the "
        "contract and the account's history, decides what to dispute, and drafts "
        "the correspondence."
    ),
    sub_agents=[
        ExtractorAgent(
            name="invoice_extractor",
            description=(
                "Transcribes an invoice PDF into the canonical schema and stores it "
                "in Firestore, keyed by the hash of the source document."
            ),
        ),
        build_auditor(),
        DisputeWriterAgent(
            name="dispute_writer",
            description=(
                "Drafts the carrier dispute letter and the customer summary, and "
                "verifies every figure against the rule engine's output."
            ),
        ),
    ],
)
