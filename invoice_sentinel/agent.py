"""Root agent exposed to `adk web` and `adk deploy`.

One invoice in, a defensible dispute out, with nobody in the loop:

    invoice_sentinel                 SequentialAgent
      intake                         what did the person attach, and whose is it
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

Two ways in, and the graph is the same for both. A caller that builds the
session sets `source_uri` (and optionally `profile_key`) before starting — that
is what the eval harness, the README's curl flow and Pub/Sub ingestion do.
A person with a PDF just attaches it to the message, and `intake` works out
what it is. Neither path knows about the other after the extractor.
"""

from google.adk.agents import SequentialAgent

from .auditor import build_auditor
from .dispute_writer import DisputeWriterAgent
from .extractor_agent import ExtractorAgent
from .intake import IntakeAgent

root_agent = SequentialAgent(
    name="invoice_sentinel",
    description=(
        "Audits a telecom invoice end to end: extracts it, checks it against the "
        "contract and the account's history, decides what to dispute, and drafts "
        "the correspondence."
    ),
    sub_agents=[
        IntakeAgent(
            name="intake",
            description=(
                "Reads the PDFs attached to the message: files a signed contract "
                "so the account can be audited at all, and hands an invoice to "
                "the extractor with the carrier profile to read it with."
            ),
        ),
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
