"""Root agent exposed to `adk web` and `adk deploy`.

Provisional: the Day-2 target is a SequentialAgent chaining Extractor ->
Auditor -> DisputeWriter. Until the auditor tools exist, the extractor is the
root so the developer UI shows the real pipeline stage rather than boilerplate.
"""

from .extractor_agent import ExtractorAgent

root_agent = ExtractorAgent(
    name="invoice_extractor",
    description=(
        "Transcribes a telecom invoice PDF into the canonical schema and stores "
        "it in Firestore, keyed by the hash of the source document."
    ),
)
