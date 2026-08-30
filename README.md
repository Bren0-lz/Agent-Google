# Invoice Sentinel

**An autonomous agent that audits recurring B2B telecom invoices — and then acts on what it finds.**

[![Gemini 3.5 Flash](https://img.shields.io/badge/Gemini-3.5%20Flash-4285F4)](https://cloud.google.com/vertex-ai)
[![Google ADK 2.7.1](https://img.shields.io/badge/Google%20ADK-2.7.1-34A853)](https://google.github.io/adk-docs/)
[![Cloud Run](https://img.shields.io/badge/Cloud%20Run-deployed-4285F4)](https://cloud.google.com/run)
[![Firestore](https://img.shields.io/badge/Firestore-native-FBBC04)](https://cloud.google.com/firestore)
[![Tests](https://img.shields.io/badge/tests-203%20passing-34A853)](#a--zero-credentials-90-seconds)
[![License: MIT](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

> **Live service:** https://invoice-sentinel-474711060457.us-central1.run.app
> **Built for:** [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/) — track *The Taskmaster*

---

## The friction

I work at a B2B telecom consultancy that audits phone bills for small and mid-sized companies.
Every month, analysts open invoice PDFs one by one, compare each line against the signed contract
and against what the account consumed in previous cycles, and write up whatever does not add up.

It is slow, it does not scale, and it is exactly the kind of work that gets skipped when the month
is busy — which is when the money leaks. The business exists because no company can audit its own
phone bill.

This agent does that job end to end.

---

## What it does

**One invoice PDF in. A defensible dispute letter out. Nobody in the loop.**

1. **Reads the PDF** — Gemini 3.5 Flash multimodal extraction into a canonical schema, with a
   deterministic repair loop when Pydantic rejects the output.
2. **Cross-checks it** — every line against the contracted plan and against the account's previous
   billing cycles. Five rules in three families, pure Python, `Decimal` arithmetic.
3. **Decides what to do** — and this is where it stops being a report generator. It separates
   *what the carrier owes* from *what the customer is wasting*, drafts the carrier dispute letter
   and a separate customer summary, verifies every figure in both, and escalates what it is not
   sure about to a human.

---

## Results

Measured on 15 synthetic invoices across 4 accounts and 2 visually distinct carrier layouts
(Brazilian and American). Every number below is reproducible with the commands in
[Run it yourself](#run-it-yourself).

### Extraction — `scripts/eval_extraction.py`

| Metric | Result |
|---|---|
| Schema validity | **15 / 15** invoices (including all 4 American-layout) |
| Field accuracy | **99.55 %** — 1781 / 1789 values |
| Needed a repair round | **0** |
| Content hashes match ground truth | **15 / 15** |

### Audit — `scripts/eval_audit.py`

| Metric | Result |
|---|---|
| Anomalies found | **5 / 5** — recall **100 %** |
| Exact on the money | **5 / 5** |
| False positives | **0** — precision **100 %**, including a clean control account |
| Recovered total | **1 036.10** vs 1 036.10 expected |

### Full pipeline, in production

```
disputed with the carrier :     99.96
plan optimisation         :    936.14
total                     :  1 036.10   = ground truth, to the cent
```

### The proof that actually matters

**99.55 % extraction accuracy still yields 100 % detection and exact money.** The eight residual
field errors are `quantity` / `unit_amount` on a fee line whose columns the American template never
prints — the model reads the page correctly; the fixture is the one at fault. They do not move a
single finding.

That whole chain is locked by [`tests/test_extracted_audit.py`](tests/test_extracted_audit.py),
which runs **offline, with no credentials and no tokens**, against the cached extractions committed
in [`data/extracted/`](data/extracted/). Clone the repo, run `pytest`, and you have verified the
claim yourself in under two minutes.

---

## Architecture

![Architecture](assets/architecture.svg)

Three stages call the model on an audit. Two of those are pure transcription and pure
composition; the single place a model is asked to *decide* anything is `audit_judgment`, and it
decides what to do with amounts it is structurally unable to author. (A fourth call exists but
runs at most once per account: `contract_extractor` transcribes a signed contract the first time
somebody sends one.)

Ahead of all of it sits `intake`, which is deliberately **not** an `LlmAgent`. Deciding whether
an attachment is a contract or a bill, and which carrier issued it, is pattern matching — a model
call whose only job is to route would be tokens spent on nothing, the same reasoning that keeps
the rule families deterministic.

The two paths worth tracing first are the ones that do **not** end in a document.
`escalate_for_review()` puts a finding the agent is not confident about into `review_queue` for a
person. And `amount_guard` gates the letter itself: a figure the rule engine never computed sends
the draft back for a rewrite, and if it survives the rewrite budget the dispute is stored as
`blocked` rather than as a draft somebody might later mistake for reviewed.

Two structural notes worth the reader's time:

- **The rule families are not `LlmAgent`s.** Three model calls whose only job is to invoke a
  function would be tokens spent on nothing, and would dress deterministic work up as reasoning.
  They run concurrently under a `ParallelAgent`, so the graph shows what actually happens.
- **`merge_findings` exists because concurrency is not free.** If each family read the shared
  findings dict and wrote it back, the last writer would erase the others — a textbook lost update.
  Each family writes its own key and the merge combines them, applying the same suppression tail
  the single-threaded path uses. Without it the concurrent path would reach different conclusions
  than the eval, and the eval would stop being a valid check on the agent.

---

## Design principles

### 1. No money figure ever comes out of an LLM

This is the load-bearing claim of the project, and it is enforced in **three layers** rather than
promised in a prompt:

| Layer | Mechanism |
|---|---|
| **Structural** | The rule engine is pure Python with `Decimal`. No module under [`invoice_sentinel/rules/`](invoice_sentinel/rules/) imports an LLM client. |
| **In the tool signatures** | No auditor tool accepts a monetary value as an argument. `flag_anomaly(finding_id, rationale)` cannot be talked into disputing R$ 4,000 that nobody computed. A test asserts this with `inspect.signature`. |
| **In the generated prose** | [`amount_guard.py`](invoice_sentinel/amount_guard.py) checks every money-shaped number in the letter against the set the engine actually computed. Invented one? The model is told *which* number and rewrites it. Insisted? The dispute is stored as `blocked`, never `draft`. |

The third layer is the easiest to forget and the one that matters most: the letter is the only
artefact that is generated prose, addressed to a third party, signed by the customer.

### 2. Disputing is not the same as optimising

**Three of the five rules are not the carrier's fault.** A dormant line, an oversized plan and a
chronic overage are all billed *correctly* under the signed contract — the waste belongs to the
customer's own configuration.

| Rule | Remedy | Action |
|---|---|---|
| `zombie_line` | `optimise` | cancel the line |
| `plan_tier_mismatch` | `optimise` | downgrade |
| `chronic_overage` | `optimise` | upgrade |
| `orphan_addon` | **`dispute`** | contest it |
| `rate_drift` | **`dispute`** | contest it |

This lives in [`anomaly.py`](invoice_sentinel/anomaly.py) as `Remedy` / `CARRIER_ERRORS`, not in a
prompt — it is domain truth, not model behaviour. And `flag_anomaly` **refuses** to dispute an
`optimise` finding, with an error explaining why.

> **How this rule got written.** On the auditor's very first run, the model escalated everything
> instead of disputing it, saying: *"the carrier billed in accordance with the signed contract…
> these are optimisation opportunities."* It was right. I had given it dispute / escalate / dismiss
> and no category for "billed correctly, money wasted anyway". Demanding a refund for a plan the
> customer chose invites a flat rejection — and drags the two legitimate claims down with it.
>
> The consequence is two documents instead of one: a letter to the carrier containing only what the
> carrier owes, and an executive summary for the customer with both kinds kept separate.

### 3. Knowing when it is unsure

`escalate_for_review` fires when the agent sees something the rule engine cannot: extraction
warnings touching the affected line, a line newer than the pattern being claimed, or confidence
below `ESCALATION_CONFIDENCE_THRESHOLD` (0.75). An agent that knows when it is unsure demonstrates
judgement; blind automation does not.

**Not knowing at all is a separate answer, and it needed separate machinery.** With no contract on
file, three of the five rules are skipped, `list_findings` comes back empty, and there is nothing
to escalate — `escalate_for_review` takes a `finding_id`, and a rule that never ran produces none.
So the empty result branches on `get_contract` instead: findings *and* a contract means the invoice
checks out; no contract means the agent says it could not audit it and asks for the document.

> **How this one got found.** The deployed agent answered a brand-new account — the first thing
> anyone tries — with *"the invoice looks clean"*. The test invoice was overcharged by 5.00, which
> showed up the moment the contract was filed. Silence from a rule that never ran is not evidence
> of a correct bill, and the one stage a reader is most likely to believe was reporting it as if it
> were. The same mistake had already been fixed once for the *no invoice* case
> ([`nothing_was_extracted`](invoice_sentinel/auditor.py)); the *no contract* case had been left out
> of it.

---

## Run it yourself

Four paths, cheapest first.

**Prerequisites.** **Python 3.10 or newer** — `google-adk` 2.7.1 and `google-genai` both declare
it; developed and tested on 3.12.9. Path A needs nothing else: no Google Cloud project, no
credentials, no billing. Paths C and D additionally need the
[gcloud CLI](https://cloud.google.com/sdk/docs/install) and a project with billing enabled.

```bash
git clone https://github.com/Bren0-lz/invoice-sentinel.git
cd invoice-sentinel
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

### A · Zero credentials (90 seconds)

```bash
pytest
```

**203 tests, fully offline.** No Google Cloud project, no API key, no billing. This includes
`test_extracted_audit.py`, which replays the committed extractions in `data/extracted/` through the
real rule engine and asserts the exact recovery figures — the end-to-end claim, verified without
spending a token.

### B · Reproduce the published metrics

```bash
python -m scripts.eval_extraction --cache data/extracted
python -m scripts.eval_audit      --cache data/extracted
```

Both read the cached extractions, so they cost nothing and are deterministic. Add
`--report out.json` to either for the full machine-readable breakdown.

Other useful flags:

```bash
python -m scripts.eval_extraction --account ACC-US-77120 --limit 1   # cheap iteration
python -m scripts.eval_extraction --cache data/extracted --refresh   # re-extract (calls Gemini)
python -m scripts.eval_audit      --ground-truth                     # isolate rule bugs from extraction bugs
```

### C · Full deploy to your own Google Cloud project

> ### ⚠️ The region rule — read this before you debug anything
> **Infrastructure runs in `us-central1`. Gemini calls go to the `global` endpoint.**
> The latest Gemini 3.x models are only served globally, so `GOOGLE_CLOUD_LOCATION=global` is
> deliberate and must not be "fixed" to match the infra region. If an error mentions a region,
> check this first. It cost an hour once already.

```powershell
gcloud auth login
.\deploy.ps1 -EnableApis      # first time: APIs + buckets + Firestore composite indexes
.\deploy.ps1                  # every deploy after that
```

`deploy.ps1` is the single source of truth for deployment and doubles as the spin-up instruction.
Pass `-ProjectId`, `-Region`, `-ServiceName`, `-Bucket` or `-ArtifactBucket` to point it at your
own project. With `-EnableApis` it:

1. enables `aiplatform`, `run`, `firestore`, `pubsub`, `storage`, `secretmanager`, `cloudbuild`
   and `artifactregistry`;
2. creates the raw-invoice bucket with uniform bucket-level access — per-object ACLs are a
   liability on a bucket holding customer billing documents;
3. creates the artifact bucket, on the same terms and deliberately not the same bucket: one holds
   documents the carrier issued, the other holds documents written on the customer's behalf, and
   they do not belong under one retention or access story. It backs
   `--artifact_service_uri`, without which the ADK falls back to an in-memory service and the
   letter and summary vanish with the next revision;
4. creates the Firestore composite indexes from the committed
   [`firestore.indexes.json`](firestore.indexes.json). `get_history` fails outright without the
   `account_id` + `billing_period_end` index, and an index that exists only because someone clicked
   a link in an error message is not a reproducible setup;
5. deploys, then **verifies the deploy actually took** by comparing `latestCreatedRevisionName`
   against `latestReadyRevisionName`. `adk deploy` swallows a failing `gcloud run deploy` and still
   exits 0, which is how a container that dies on startup gets mistaken for a working deploy.

Every deploy passes `--min-instances`, which defaults to **1**. A cold start on this image runs
about 16 seconds, and the first click of a demo should not look like a hang; the cost is one idle
Cloud Run instance. `-MinInstances 0` scales to zero instead.

The container needs exactly three environment variables, which `deploy.ps1` sets via
`--env-vars-file` — a local `.env` does **not** travel into the image:

```
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=<your-project>
GOOGLE_CLOUD_LOCATION=global
```

Locally you do not need a `.env` either. `config.configure_genai_backend()` fills all three at
import time with `os.environ.setdefault`, so `adk web`, the tests and the container behave
identically. Values you set yourself always win.

### D · Run the agent locally

Nothing has to be deployed to run the agent on your own machine — but it still calls Vertex AI and
Firestore, so it needs a project: the one path C provisioned, or an existing one. The Google client
libraries read *application-default* credentials, which `gcloud auth login` does **not** write:

```powershell
gcloud auth application-default login
```

Then seed the contracts and history, and open the agent:

```bash
python -m scripts.seed_firestore
adk web invoice_sentinel
```

`adk web` prints the local URL to open. It serves the same dev-ui as the deployed service, running
the same pipeline against the same Firestore collections — the only difference is where the
container lives.

---

## Auditing your own invoice

Everything above audits the synthetic dataset. To audit a bill of your own, open the agent
and **attach the PDF to the message**. No bucket, no `curl`, no seeding.

```
[attach fatura-julho.pdf]  "vantel"
```

That is the whole interaction. The `intake` stage reads the attachment, hands it to the
extractor with the profile you named, and the rest of the graph runs exactly as it does for
the dataset.

**The two documents come back as attachments.** The carrier letter and the customer summary are
written to Firestore and also attached to the session, so they are in the **Artifacts** tab of the
UI to read and download — `ACC-…-carrier-letter.md` and `ACC-…-customer-summary.md`. A dispute the
`amount_guard` blocked is deliberately **not** attached: its prose contains a figure the engine
never computed, and a downloadable file is exactly the draft someone later mistakes for a reviewed
one. That run gives you the `WITHHELD` message and nothing to download.

Two things it will tell you rather than guess about:

**It audits against a signed contract.** An invoice on its own cannot be wrong — it can only
disagree with something. Send one for an account with no contract on file and the agent says it
could not audit it and asks for the document — it will not call an unchecked invoice clean. If the
account is new, attach the contract too and say `contract`:
Gemini transcribes it into the same `Contract` schema `seed_firestore` writes, it is filed
under the account id, and every invoice you send afterwards is audited against it. Note what
this does *not* change: the contracted rates become an input the rule engine compares
against, in `Decimal`, in pure Python. No figure in a dispute letter has ever been quoted
from a model, and that is still true here.

**One invoice is already enough to find money.** `orphan_addon` and `rate_drift` — the two
`dispute` rules, the ones where the carrier owes you — need only the contract and the bill
in front of them. The three `optimise` rules claim a *pattern*, so they stay quiet until the
account has `PATTERN_CYCLES` (3) consecutive cycles on file. Send three months and the plan
sizing findings light up on their own.

> ⚠️ **Only two carriers are supported today**: `br-vantel-empresas` and
> `us-northwind-wireless`. A bill from Vivo, Claro or AT&T will be **refused**, not guessed
> at — `profile_for()` raises on an unknown key rather than reading a Brazilian invoice with
> American separator hints, which produces plausible, wrong numbers that nothing downstream
> would catch. A new carrier is a new entry in [`profiles.py`](invoice_sentinel/profiles.py)
> and nothing else.
>
> The refusal is checked against the document, not against the request. Naming a carrier
> the bill was not issued by — typing `northwind` over a Vantel invoice — is refused for
> the same reason, by comparing the carrier printed on the page against the profile it was
> asked to read with. Naming the wrong one used to be accepted silently, which is the
> failure mode `profile_for()` was written to prevent, arriving through the door nobody had
> locked.
>
> **You do not have to name one.** `intake` reads the text layer of page one and picks the
> profile off the letterhead — deterministically, with a regex and no model call, since the
> issuer's name is printed where a bill starts by definition. It gets all 16 documents in
> and around this repository right, and it is what turns naming a carrier from an
> instruction into an exception.
>
> The exception matters, because reading the page is not always possible. A scan carries no
> text layer, and there the agent **asks** rather than falling back to a default: assuming
> the Brazilian profile for an American bill is exactly the plausible-and-wrong reading the
> refusals exist to prevent, and it used to cost a full extraction before anything noticed.
> Your answer then stands until the model has actually read the page, where the carrier is
> checked once more.

### Driving the deployed API directly

```bash
URL=https://invoice-sentinel-474711060457.us-central1.run.app

curl -X POST "$URL/apps/invoice_sentinel/users/judge/sessions/demo" \
  -H "Content-Type: application/json" \
  -d '{"source_uri":"gs://agent-hackton-invoices-raw/ACC-BR-1041-2026-07.pdf",
       "profile_key":"br-vantel-empresas"}'

curl -X POST "$URL/run" -H "Content-Type: application/json" \
  -d '{"appName":"invoice_sentinel","userId":"judge","sessionId":"demo",
       "newMessage":{"role":"user","parts":[{"text":"Audit this invoice."}]}}'
```

> ⚠️ The body of the session POST **is** the state. Do not wrap it in `{"state": {...}}` — that
> produces `state.state`, and the extractor will not find `source_uri`.

---

## Project layout

```
deploy.ps1                    deploy + one-time infra setup; the spin-up instruction
firestore.indexes.json        composite indexes, committed on purpose
requirements-dev.txt          runtime + reportlab/pypdf/pytest

invoice_sentinel/
  requirements.txt            RUNTIME ONLY - this is what the container installs
  config.py                   model id, regions, collections, every rule threshold
  schema.py                   canonical schema + Money + Quantity + content_hash
  contract.py                 the contracted truth
  anomaly.py                  AnomalyType, Remedy, Evidence, Anomaly
  profiles.py                 carrier extraction profiles
  intake.py                   reads what the person attached; the way in
  extractor.py                source resolution, prompt, repair loop, transient retry
  contract_extractor.py       a signed contract PDF -> Contract
  extractor_agent.py          ADK shell around the extractor
  store.py                    Firestore: invoices, contracts, anomalies, reviews, disputes
  audit_tools.py              the auditor's tools + state keys
  auditor.py                  the auditor graph
  amount_guard.py             verifies every figure in generated prose
  dispute.py                  Dispute, DisputeStatus, DisputeDocuments
  dispute_writer.py           generation + guard + agent
  agent.py                    root_agent: the full SequentialAgent
  rules/                      five rules in three families - pure Python, zero LLM

scripts/                      dev-only; never enters the container
  synthetic/                  the dataset generator
  extraction_cache.py         extractions cached by content_hash
  eval_extraction.py          field-by-field extraction accuracy
  eval_audit.py               anomaly precision / recall
  seed_firestore.py           uploads contracts and history

data/synthetic/               15 PDFs, 4 contracts, ground_truth.json
data/extracted/               15 cached extractions - the offline evidence
data/independent/             one invoice + contract nobody here designed
tests/                        203 tests, all offline
```

Two layout decisions that are load-bearing:

- **`invoice_sentinel/requirements.txt` holds runtime dependencies only.** Every extra package in
  the image is cold-start latency. ReportLab and pytest live in the root
  `requirements-dev.txt`. pypdf was one of them until intake started reading the carrier's name
  off page one, which is the bar for crossing over: it replaced a model call, not a convenience.
- **Nothing in `scripts/` may be imported by `invoice_sentinel/`.** That folder does not exist
  inside the container. It is why the carrier profiles live in the package rather than next to the
  dataset generator that also uses them.

---

## The dataset

15 invoices · 4 accounts · up to 4 billing cycles each · **zero real customer data**.

| Account | Customer | Layout | Cycles | Planted findings |
|---|---|---|---|---|
| `ACC-BR-1041` | Aurora Logística | BR | 4 | `zombie_line` (239.60), `chronic_overage` (176.54) |
| `ACC-BR-2087` | Meridiano Saúde | BR | 4 | `plan_tier_mismatch` (520.00), `rate_drift` (20.00) |
| `ACC-BR-3312` | Cortez Advocacia | BR | 3 | **clean** — false-positive control |
| `ACC-US-77120` | Cascadia Freight | US | 4 | `orphan_addon` (79.96) |
| | | | | **Total recoverable: 1,036.10** |

**Ground truth is by construction, not by annotation.** Each PDF is born from a validated
`ExtractedInvoice` and only then rendered, so `expected_invoice` in `ground_truth.json` is literally
what went in. That is what makes field-by-field extraction accuracy possible at all, rather than
just "did it parse".

The two PDF templates are deliberately unalike — Brazilian (A4, purple, opens with a line summary,
`R$ 1.234,56`, `31/07/2026`) and American (Letter, blue, opens with a usage summary, `$1,234.56`,
`07/31/2026`). An extractor that has only ever seen one layout proves nothing.

> ⚠️ **Do not regenerate the dataset.** `content_hash` values are the Firestore document keys and
> the anchor of every published metric. Regenerating would invalidate all of it.

### The document nobody here designed

Everything above comes out of one generator, and that is a weaker claim than it looks: a template
and the code that reads it can share the same wrong assumption and stay green forever.

So [`data/independent/`](data/independent/) holds a Brazilian invoice and its signed contract built
**independently**, from public material about how carriers here actually bill — a cycle that does
not start on the first (26/07–25/08), taxes itemised because Lei 12.741/2012 requires it,
identifiers in the shape a real bill prints them (`(11) 97412-3308`, `4.812.663-5`). Nobody
designing it knew which rules would read it.

Auditing it the first time surfaced **three defects at once, and all three were false positives**:
taxes computed *por dentro* counted as money billed twice, and a legitimate add-on accused of being
unauthorised because the invoice and the contract spell the same product differently. None of the
15 synthetic invoices could have caught any of them.

[`tests/test_independent_document.py`](tests/test_independent_document.py) now locks that document
green, offline, against the committed extraction — including that the carrier is read off a
letterhead this repository did not typeset. It is the false-positive control with the generator
taken out of the loop.

---

## Google Cloud footprint

| Service | Role |
|---|---|
| **Vertex AI** (Gemini 3.5 Flash) | Multimodal PDF extraction, audit judgement, document drafting |
| **Google ADK 2.7.1** | Agent graph: `SequentialAgent`, `ParallelAgent`, tools, session state |
| **Cloud Run** | Hosts the agent and the ADK API server; `--trace_to_cloud` enabled, message content excluded |
| **Firestore** (Native) | Canonical invoices, contracts, anomalies, disputes, review queue |
| **Cloud Storage** | Two buckets, uniform access and public access prevention enforced: documents staged for the `gs://` path, and the ADK artifact store holding the generated letters |
| **Cloud Trace** | Per-run agent traces: timings, token counts, tool calls — never the invoice |
| **IAM** | A dedicated runtime service account, `invoice-sentinel-run`, not the project default |

Not in the table, because they are not deployed: **Pub/Sub** ingestion and **Secret Manager**.
`store.py` and `extractor.py` are written to survive a redelivery, and the idempotency below is
what would make an at-least-once queue safe — but no topic exists, and the agent holds no secret
to store: it authenticates as its service account.

Invoices in the bucket are read **by reference** (`Part.from_uri`) rather than uploaded again, and
persisted keyed by the SHA-256 of the source bytes. `save_invoice` uses `create()`, not `set()`, so
the same PDF submitted twice loses the race by design — **idempotency is a property of the key, not
a check the caller has to remember to perform.**

---

## Security and privacy

A telephone invoice is personal data — line numbers, employee names, an itemised record of who
called whom. This section states what is true of the deployed service today, then what a
production engagement would add. The order matters: the guarantees below are properties of the
design, not promises in a policy.

### What the design already guarantees

**The uploaded PDF is never stored.** Not "deleted after N days" — never written. `intake` reads
the attachment out of the message, the extractor works on those bytes in memory, and they are gone
when the request ends. `upload://<sha256>` records the hash, not the document. There is no
retention rule to get wrong, because there is nothing retained.

**Nothing logs the invoice.** The package contains no `print`, no logger, and no logging
configuration. Cloud Trace is enabled, because latency, token counts and failures are worth
having — but `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false` keeps the prompt and the completion off
the span. The ADK's default is the opposite, and it sets this value itself only on the Agent
Engine deployment path; on Cloud Run it is `deploy.ps1` that has to say it.

**The runtime identity is scoped to the job.** The service runs as `invoice-sentinel-run` with
Firestore, Vertex AI, log-writing and trace-writing at the project level, and object access
granted *on the two buckets* rather than across the project. It is not the default Compute
service account, which carries `roles/editor`. `deploy.ps1` refuses to deploy if the account is
missing rather than falling back to the default in silence.

**Derived documents expire.** The dispute letter and the customer summary carry the account
number and every disputed figure, and they are the one artefact that used to outlive everything
else: the session that produced them is in-memory and does not survive a revision, but the object
in the bucket had no end date. They are now deleted after 30 days (`artifact-lifecycle.json`).

The structured audit record — the canonical invoice, its findings and the dispute — stays in
Firestore with no expiry, deliberately: an audit you cannot re-examine later is not an audit. That
is the data a retention policy belongs to, and setting one is a customer decision, not a default
this repository should pick.

**The agent is built to run in the customer's own project.** Project, region, model endpoint and
both bucket names are environment variables; `deploy.ps1` takes `-ProjectId`, `-Region` and
`-ArtifactBucket`. A per-customer deployment is a flag, not a refactor:

> *The agent is designed to run inside the customer's own Google Cloud project — invoices never
> leave their perimeter.*

That is the architectural answer to data custody, and it is stronger than any assurance the
operator of a shared service could offer: there is nothing to trust them with.

### What is deliberately open, and why

The hosted demo runs `--allow-unauthenticated` so that a reviewer can open the URL and use it.
Two consequences, stated rather than buried:

- Anyone who reaches the endpoint can spend Vertex AI quota. The ceiling is structural —
  `--max-instances=2 --concurrency=8` in `deploy.ps1`, plus a billing budget alert.
- A generated letter is readable by anyone holding its session UUID, which travels in the URL.
  The 30-day lifecycle bounds that window.

Both are acceptable for a demo of synthetic invoices and unacceptable for a real engagement.
Closing it is one reversible command:

```bash
gcloud run services remove-iam-policy-binding invoice-sentinel \
  --member=allUsers --role=roles/run.invoker
```

### What production would add

- **PII minimisation.** `ServiceLine.line_id` (the MSISDN) and `assigned_to` are persisted whole.
  Masking to the last digits is the obvious hardening — and the reason it is not done here is
  worth stating plainly: the dispute letter has to identify the line for the carrier to act on it.
  Masking at rest means keeping the mapping somewhere, which moves the problem rather than solving
  it. The right design is per-tenant separation, not redaction.
- **Data residency.** Firestore and both buckets are in `us-central1`. A Brazilian customer would
  want `southamerica-east1`; `-Region` already parameterises it, but the Gemini endpoint stays
  `global` and that is a conversation, not a flag.
- **CMEK** on Firestore and Cloud Storage, so the customer holds the key to their own data.
- **VPC Service Controls**, so a leaked credential still cannot move data out of the perimeter.
- **Per-tenant isolation** — bucket prefix and Firestore collection per customer, with conditional
  IAM — which the per-project deployment above makes largely moot.
- **Audit trail.** Cloud Audit Logs recording who read which invoice.

---

## Limitations and roadmap

Honest scope, so nothing here is oversold:

- **The dataset is synthetic.** A deliberate choice — no real customer billing data was used — but
  it means the extraction numbers describe two well-formed templates, not the long tail of real
  carrier PDFs.
- **Two carrier profiles** ship today (`br-vantel-empresas`, `us-northwind-wireless`). A new
  carrier needs a profile; `profile_for()` refuses unknown keys rather than guessing.
- **Eight known field errors** are `quantity` / `unit_amount` on the US template's fee section,
  which prints only Description and Amount. Documented as `UNPRINTED_FIELDS` in
  `eval_extraction.py`, and deliberately *not* masked away: excluding `charges[].quantity` wholesale
  would stop scoring the Qty/Rate columns the template does print.
- **The five rules are structurally universal**, but jurisdiction-specific charges (ICMS, FUST,
  Anatel) belong in an extraction profile, not in the rule engine.

---

## License

[MIT](LICENSE). Built for the
[All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/) — track *The
Taskmaster*.
