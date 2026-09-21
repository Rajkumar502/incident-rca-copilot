# Incident RCA Copilot

A governed multi-agent system that ingests incidents, alerts, deploys, and test failures, retrieves similar past incidents and runbooks, dispatches specialist agents to analyze logs/diffs/tests/metrics, and produces a **cited** root-cause hypothesis, risk score, and rollback recommendation — gated behind human approval before anything risky ships.

Built with **LangGraph**, **MCP**, **Pydantic**, and a **RAG** layer over historical incidents.

> **Companion project:** [`agentic-playwright-framework`](../agentic-playwright-framework) established the same governance pattern — separate generation from review, gate risky actions behind human sign-off, track cost, classify failure root causes into actionable buckets — for the SDLC/test-automation domain. This repo applies that pattern to production incident response, on the stack production-ops tooling actually uses.

> **Project status:** this is a learning/portfolio project, not a production deployment. The governance architecture — confidence gating, citation grounding, human-approval enforcement, retry/backoff, the review queue — is real, tested, and the focus of the project. The individual vendor integrations (Jira, GitHub, Grafana, Splunk) are built to production-shape and unit-tested against mocked HTTP, but only Jira has been exercised against a real instance so far (see `ARCHITECTURE.md` Section H, which documents a real bug this found and fixed). Treat the integrations as "designed correctly, mostly mock-verified" rather than "battle-tested against live traffic." See [`DEMO.md`](./DEMO.md) for a full walkthrough with real captured output.

---

## Why this exists

Engineering teams spend too long reading logs, failed tests, deploy diffs, and tickets before knowing whether an incident is a real regression and whether a rollback is safe. This copilot compresses that triage time — but it is built to **fail closed**, not to auto-pilot production. Every claim in a report must trace to a real citation; every rollback recommendation or high-severity report requires a human to approve before write-back.

---

## Architecture at a glance

```
Ingestion (Jira, CI/CD, Grafana/Splunk, Git, test results)
        → Triage (severity + affected services)
        → RAG retrieval (similar past incidents + runbooks)
        → Specialist agents (log / code diff / test failure / metrics), via MCP tools
        → Synthesizer (cited RootCauseHypothesis)
        → Confidence gate (3 tiers — see ARCHITECTURE.md)
        → Human approval checkpoint (LangGraph interrupt(), mandatory for
          rollback recs or sev1/sev2)
        → Write-back (Jira comment + Markdown report + dashboard event)
```

Full design rationale, schemas, and the confidence-gating model: see [`ARCHITECTURE.md`](./ARCHITECTURE.md).

---

## Repository layout

```
src/
├── graph/            # LangGraph StateGraph, state, and node implementations
│   └── nodes/specialists/   # LogAnalyst, CodeDiff, TestFailure, Metrics agents
├── schemas/          # Pydantic models — the contract every node writes to
├── mcp_tools/        # MCP tool wrappers (Jira, GitHub, observability, vectorstore)
├── ingestion/         # real evidence collection over the MCP protocol
├── webhook/           # automatic trigger — Jira webhook receiver
├── rag/              # embedding + retrieval over past incidents/runbooks/real history
├── config.py          # single common place .env is loaded and credentials are read
├── governance/        # cost/iteration circuit breaker, retry/backoff, audit log
└── review/            # review queue — list/approve/reject reports waiting on a human
synthetic_data/        # synthetic incident generator (6 archetypes, labeled)
eval/                  # hallucination + unsafe-recommendation + calibration suites
dashboard/              # observability UI for traced agent decisions
```

---

## Status

**The full pipeline runs end-to-end and is tested.** 65 automated tests pass (`pytest eval/ tests/`), plus the hallucination and calibration suites (run as scripts, not pytest, since they exercise the graph directly). Uses a deterministic mock LLM by default so it's runnable offline; swap in real Gemini by setting `GEMINI_API_KEY`.

- [x] Pydantic schemas, graph state, cost/iteration circuit breaker — verified to trip
- [x] Three-tier confidence gate — verified against 18 synthetic fixtures + adversarial injection tests
- [x] Synthetic incident generator, 6 labeled archetypes
- [x] RAG retrieval (`SimpleTfidfStore`, zero-network) + `ChromaStore` (production swap-in, same interface) + **real-incident history feedback loop** (`src/rag/history.py` — every human-approved published report is recorded and becomes retrievable evidence for future incidents)
- [x] Four specialist agents, LangGraph assembly, human-approval checkpoint, write-back — full graph runs on all 18 fixtures
- [x] **MCP tool wrappers** — Jira (read/write/create/transition), GitHub (commits/CI runs), Grafana/Splunk (alerts/logs) — real REST implementations, each verified by unit tests against a mocked HTTP transport (`tests/`)
- [x] **MCP server** (`src/mcp_tools/server.py`) registering the above via the `mcp` SDK — compatible with both mcp v1 (`FastMCP`) and v2+ (`MCPServer`, renamed 2026-07-28) via an automatic import shim
- [x] **Real ingestion layer** (`src/ingestion/`) — fetches live Jira/GitHub/Grafana/Splunk data **over the actual MCP client-server protocol** (not direct function imports) and normalizes it into the same `IncidentEvent` schema the synthetic demo uses, so the rest of the graph runs completely unchanged
- [x] **Dashboard** (`dashboard/app.py`) — Streamlit trace viewer with two view modes (Simple/plain-English, Technical/detailed trace) and two incident sources (synthetic demo fixtures, or a real Jira ticket by issue key); started and confirmed serving (HTTP 200) during development
- [x] **Confidence calibration suite** — caught and fixed a real miscalibration bug (see below)
- [x] Hallucination suite: 0 violations across 18 fixtures. Unsafe-recommendation suite: 4/4 pass including adversarial prompt-injection cases.
- [x] **Automatic trigger** (`src/webhook/server.py`) — a FastAPI webhook server receives Jira automation events and calls `analyze_real_incident()` without a human running a CLI command; secret-header auth, label-based opt-in, and idempotent de-duplication, all verified
- [x] **Retry/backoff on transient tool failures** (`src/governance/retry.py`) — MCP tools now return a classified error envelope (`http_5xx`/`network`/`rate_limited` = retryable; `config`/`http_4xx` = fail fast) so a transient outage recovers automatically while a permanent failure doesn't waste time retrying
- [x] **Review queue** (`src/review/queue.py`, dashboard's "🗂️ Review Queue" tab, `python -m src.review.cli`) — a single place to see and act on every report waiting on human approval, whether it came from the dashboard, the CLI, or a webhook-triggered run; approving publishes and records history, rejecting archives with reviewer + reason and never touches Jira or history

**What "verified" means for each piece, honestly:**
| Component | Verification |
|---|---|
| Graph logic, gates, schemas, synthesis | Actually run end-to-end against all 18 synthetic fixtures |
| Mock LLM path | Actually run, real output inspected |
| Real Gemini | Not called — no live key/network to Google's API in this environment |
| Jira/GitHub/Grafana/Splunk tools | Real code, correct request/response handling proven via mocked-HTTP unit tests — never called a live instance |
| **MCP client-server round trip** | **Actually run** — a real `mcp.client.Client` connected to the real `MCPServer`, discovered all 5 tools over the wire, invoked one, and correctly received a network error back through the MCP protocol's error handling |
| **Real ingestion → real graph, end to end** | **Actually run** — `analyze_real_incident()` fetched a Jira ticket + correlated GitHub commit over the MCP protocol (mocked HTTP transport only), normalized them into `IncidentEvent`s, and fed the unmodified graph, producing a report whose citations point to the **real Jira key and commit sha**, not synthetic IDs (`tests/test_real_ingestion_integration.py`) |
| **`.env` auto-loading + real Jira write-back** | **Actually run** — confirmed a `.env` file at the repo root is picked up automatically (no `export` needed), that a real shell-exported var still correctly overrides a conflicting `.env` value, and that `build_graph()` auto-selects `RealJiraClient` and posts a correctly-formatted comment when Jira credentials are present, all with zero code changes to the graph itself |
| **Real-Jira ADF description parsing** | **Actually run against a real Jira instance, and found a real bug** — the first live run against an actual ticket crashed (`KeyError: slice(...)`) because Jira's v3 API returns `description` as a nested Atlassian Document Format tree, not a plain string; fixed in `jira_tool.py`, reproduced the exact crash with the real issue key and confirmed the fix resolves it, added regression tests |
| **Dashboard real-Jira mode** | **Actually run** — exercised the exact code path the dashboard's "Analyze" button triggers (`collect_incident_events()` → `build_graph()`) against a mocked transport using a real issue key and realistic ADF payload, confirmed correct decision log and report |
| **RAG-history feedback loop** | **Actually run, two-incident proof** — a first real incident was approved and published, confirmed it was recorded to `incident_history.jsonl`; a second, unrelated-ticket incident with a similar failure pattern was then run and its retrieval step **actually found the first one** (`history:OPS-1`) via real TF-IDF similarity — proving retrieval quality genuinely improves as the system analyzes more real incidents, not just in theory |
| **Automatic trigger (Jira webhook)** | **Actually run** — the real FastAPI/uvicorn server was booted and hit with real HTTP requests (not just `TestClient`); secret validation, label filtering, and idempotency all verified; a full end-to-end test confirmed a correctly-shaped webhook call actually runs `analyze_real_incident()` in the background and produces a report with the real issue key flowing through — closing the gap between "the pipeline works" and "the pipeline runs without a human typing a command" |
| **Retry/backoff on transient tool failures** | **Actually run, and found a real classification problem along the way** — discovered the MCP protocol flattens *any* tool exception (a 503, a 404, missing credentials) into an identical generic error on the client side, making smart retry impossible; fixed by having every tool return a classified success/error envelope before the exception ever crosses the protocol boundary. Verified: a transient 503 recovers after 2 retries and succeeds on the 3rd attempt; a permanent 404 and a missing-credentials error both fail **fast with zero retries** (not wasted latency); a persistently-failing service exhausts its attempts and degrades to `None` gracefully rather than crashing. A real off-by-one bug in the attempt-counting logic was also caught and fixed by these tests, not just written correctly on the first try. |
| **Review queue** | **Actually run, full lifecycle** — created a real blocked report, confirmed it appears in `list_blocked()`; approving it correctly published to (mock) Jira, recorded it to RAG history, and removed it from the queue; rejecting a separate report confirmed it correctly did **not** touch Jira and did **not** get recorded to history, and was archived with the reviewer name and reason; a corrupt report file was confirmed to be skipped rather than crashing the whole listing; the dashboard's Review Queue tab was booted with a real blocked report present and confirmed to render (HTTP 200) |
| **GitHub tool — live, zero-mock test** | **Actually called the real `api.github.com`, no token, no mocking at all.** `github_tool.fetch_commit()` correctly fetched and parsed a real commit from `octocat/Hello-World`. A second real-world test against a busier repo (`pytorch/pytorch`) hit GitHub's real unauthenticated rate limit (a 403, not a 429 — GitHub doesn't use the "standard" rate-limit status code) — and the full classification + retry system correctly recognized it as non-retryable and failed fast after 1 attempt, rather than wasting three exponential-backoff retries against an hourly limit that can't recover in seconds. This was an unplanned real-world edge case, not a designed test, and the system handled it correctly. |
| **Jira priority sync** | **Actually run, all 4 severities.** Confirmed `write_comment()` posts the comment first, then sets Jira's own `priority` field via a real PUT request shape (`sev1→Highest`, `sev2→High`, `sev3→Medium`, `sev4→Low`), and confirmed a failed priority update (e.g. a Jira project with a custom priority scheme) never blocks the comment that already succeeded — the publish still completes either way. |
| **Triage severity classification — a real false-positive bug found and fixed** | **Found on real synthetic data, not invented.** The original keyword matcher used plain substring containment, so `"downstream analytics job..."` (an actual fixture describing a database connection-pool issue) matched `"down"` inside `"downstream"` and was misclassified as sev1 — a full outage — when nothing was down. Fixed with word-boundary regex matching for single words (phrases are unaffected, since a false match inside a longer phrase isn't realistically possible). Reproduced the exact bug against the real fixture, confirmed the fix resolves it while still correctly classifying genuine sev1/sev2 language, and added 8 permanent regression tests. |
| **CI workflow** | **Actually would have failed before this check** — simulated the exact GitHub Actions steps in a genuinely clean virtualenv and found `pip install -e ".[dev]"` doesn't install `mcp` or `fastapi`, which 3 test files need; fixed to `pip install -e ".[dev,mcp,webhook]"` and reran all 4 CI steps in a clean venv end to end, confirming they now actually pass |
| Dashboard | Actually started, actually served HTTP 200; full interactive click-through not exercised (headless environment) |
| Chroma backend | Real code against the real `chromadb` API shape; not run — needs network to fetch the embedding model on first use |

**Calibration bug found and fixed during eval:** the specialist confidence heuristic originally scaled with evidence *count* only, so the "ambiguous" archetype (weak, conflicting evidence) still scored 0.65 confidence — above its intended low band. Fixed by discounting confidence when an analysis's own language signals a weak/mixed finding ("no strong signal", "mixed", "no single cause"). After the fix, ambiguous cases correctly fall below the publishable-confidence gate and route to "insufficient evidence" instead of auto-publishing a shaky guess. This is exactly the kind of thing the eval suite exists to catch.

---

## Token efficiency & governance

Measured, not estimated, from `token-audit.json` on the full 18-fixture corpus:

| Metric | Value |
|---|---|
| LLM calls per incident | avg **2.2** (not 4 — agents with no relevant evidence never run) |
| Input tokens per call | avg **93**, max **105** |
| Output tokens per call | avg **20**, max **27** |
| Total cost, 18-incident corpus | **$0.0007** (mock pricing) |

**Where the savings come from, layered (defense in depth, not one setting):**

1. **Selective dispatch** — `BaseSpecialist.analyze()` returns `None` immediately if an agent's source types have no evidence in the incident (`src/graph/nodes/specialists/base.py`). A db-exhaustion incident never calls the code-diff or test-failure agents at all — this is why avg calls/incident is 2.2, not 4.
2. **Narrow evidence per agent** — each specialist only ever sees its own source-type slice, never the full incident dump.
3. **Self-match exclusion in retrieval** — `retrieval.py` filters the current incident's own id out of its RAG results, so the pipeline never wastes a retrieval slot (and the tokens to inject it) matching an incident against itself.
4. **Per-field size caps, applied before any token count is estimated:**
   - `MAX_PAYLOAD_CHARS_PER_EVENT = 240` — any single event's payload is truncated before entering a prompt
   - `MAX_EVIDENCE_EVENTS_PER_CALL = 6` — hard cap on events per specialist call, oldest/lowest-priority dropped past that, **logged** (`SPECIALIST[...]: capped evidence to 6 event(s)...`) rather than silently truncated
   - `MAX_RETRIEVED_DOCS = 2`, `MAX_RETRIEVED_DOC_CHARS = 180` — RAG context injected into specialist prompts is capped to a couple of short excerpts, not full retrieved documents
5. **Hard prompt-token ceiling as a backstop** — `PROMPT_TOKEN_CEILING = 600` is estimated before every call; if the per-field caps above still weren't enough, RAG context is dropped first (evidence is never dropped silently, since evidence is the grounding source) and the drop is logged. In practice the per-field caps keep every measured prompt under this ceiling already — verified by a stress test with 20 verbose events and 5 large retrieved docs, which correctly triggered the event-count cap (capped to 6, logged) without needing the ceiling backstop, confirming the layering works as intended rather than relying on one gate.
6. **Cost + iteration circuit breaker** (`CostMeter`) — caps both cumulative dollar cost and call count per incident; a stuck retry loop is treated as a governance failure even at near-zero marginal cost, not just a budget one.

**What's deliberately *not* optimized yet, honestly:**
- No prompt caching for the static system prompts (Gemini supports context caching; worth it at real production incident volume, not implemented since it needs a live API to validate).
- No cross-specialist deduplication — if two agents' evidence overlaps, both still pay for their own call. At this scale (2.2 calls/incident, sub-$0.001) it isn't worth the added complexity; would revisit if specialist count or evidence volume grows.
- RAG retrieval itself (embedding + similarity search) isn't token-metered — only LLM calls are. Fine for the TF-IDF backend (no API cost); would need extending once `ChromaStore`'s embedding calls are live.

---

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for full design rationale.

---

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# copy the env template and fill in whichever credentials you have — all
# optional; the project runs fully offline with none of them set (see
# "Configuration" below)
cp .env.example .env

# generate the synthetic demo dataset (6 labeled archetypes)
python synthetic_data/generate_incidents.py

# run everything
pytest eval/ tests/ -v
python eval/hallucination_suite.py
python eval/confidence_calibration.py

# analyze a synthetic incident end-to-end
python -m src.cli analyze-incident INC-xxxxxx              # auto-reject stub (safe default, no human present)
python -m src.cli analyze-incident INC-xxxxxx --interactive # prompts you for approval in the terminal

# browse traces in the dashboard — supports both synthetic demo incidents
# and real Jira tickets (pick "🔗 Real Jira ticket" in the sidebar, enter
# an issue key), in either Simple (plain-English) or Technical view
pip install -e ".[dashboard]"
streamlit run dashboard/app.py

# use real Gemini instead of the offline mock — set GEMINI_API_KEY in .env
pip install -e ".[gemini]"
python -m src.cli analyze-incident INC-xxxxxx

# create a Jira ticket directly from the CLI (needs JIRA_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env)
python -m src.mcp_tools.jira_tool create OPS "Checkout API 500s" "Started after deploy" Bug

# run the MCP tool server (needs live Jira/GitHub/Grafana/Splunk credentials in .env)
# stdio-based — waits for an MCP client to connect (e.g. Claude Desktop, MCP
# Inspector). It won't print anything or exit on its own; that's normal.
pip install -e ".[mcp]"
python -m src.mcp_tools.server

# analyze a REAL Jira incident end to end — real ingestion over the MCP
# protocol, real evidence, feeding the unmodified graph, and (if Jira
# credentials are set) a real write-back comment. No separate server
# process needed; the collector runs the MCP client and server in-process.
pip install -e ".[mcp]"
python -m src.ingestion.collector OPS-123 checkout-api acme checkout
#                                  ^ticket ^service     ^gh-owner ^gh-repo

# review everything waiting on a human decision — from the dashboard, the
# CLI, or a webhook-triggered run
python -m src.review.cli list
python -m src.review.cli approve OPS-123 alice
python -m src.review.cli reject OPS-123 bob "insufficient evidence, escalating"
```

Incident IDs come from `synthetic_data/generated/synthetic_incidents.json` after running the generator (for the offline demo path). For the real-ingestion path, use your actual Jira issue key instead.

### Configuration

All configuration is loaded through a single common module, `src/config.py` — every credential-reading call in the project (`jira_tool.py`, `github_tool.py`, `observability_tool.py`, `llm/client.py`, `writeback.py`, `ingestion/collector.py`) goes through it rather than reading `os.environ` directly, so there is exactly one place that knows how configuration is loaded.

**`.env` is loaded automatically** — copy `.env.example` to `.env`, fill in whatever you have, and it's picked up the moment anything imports from `src` (via `src/__init__.py` → `src/config.py`). No manual `export` needed. A real, already-`export`ed environment variable always takes precedence over `.env` (so a real deployment's configuration is never silently overridden by a leftover file).

```
GEMINI_API_KEY=...           # optional — real Gemini instead of the offline mock
JIRA_URL=...                 # these three together enable real Jira reads + write-back
JIRA_EMAIL=...
JIRA_API_TOKEN=...
GITHUB_TOKEN=...             # optional — enables commit/CI correlation in real ingestion
GRAFANA_URL=...              # optional — enables Grafana alert collection
GRAFANA_API_KEY=...
SPLUNK_URL=...               # optional — enables Splunk log collection
SPLUNK_TOKEN=...
VECTOR_STORE=tfidf           # "tfidf" (default, zero-infra) | "chroma" (needs the [chroma] extra)
COST_CEILING_USD=0.25        # per-incident circuit breaker

WEBHOOK_SECRET=...           # optional — shared secret the webhook caller must send (see below)
WEBHOOK_TRIGGER_LABEL=incident  # Jira label that opts an issue into automatic analysis
WEBHOOK_SERVICE_FIELD=...    # optional — a Jira custom field id (e.g. customfield_10050) holding the service name
WEBHOOK_GITHUB_OWNER=...     # optional — default GitHub owner/repo for webhook-triggered runs
WEBHOOK_GITHUB_REPO=...
```

Every credential is optional — the project runs completely offline (mock LLM, mock Jira write-back, TF-IDF retrieval, synthetic data) with none of them set. Each one you add unlocks exactly one real integration: Jira credentials → real reads and write-back; add `GITHUB_TOKEN` → commit correlation; add Grafana/Splunk → those sources join real ingestion; add `GEMINI_API_KEY` → real LLM reasoning instead of the mock.

### Automatic trigger (webhook)

```bash
pip install -e ".[webhook,mcp]"
uvicorn src.webhook.server:app --host 0.0.0.0 --port 8000
```

In Jira Cloud: **Project settings → Automation** → create a rule triggered on "Issue created" or "Issue updated", condition "Labels contains `incident`" (or whatever `WEBHOOK_TRIGGER_LABEL` you set), action "Send web request" to `http://<your-server>:8000/webhook/jira`, with header `X-Webhook-Secret: <your WEBHOOK_SECRET>`. Raw Jira Cloud webhooks don't support custom auth headers — **Automation for Jira** (built into Jira Cloud) does, which is why it's the recommended path over a bare webhook.

The server responds immediately (so Jira/Automation doesn't time out) and runs the full analysis — real ingestion, triage, specialists, gate, risk/rollback, and write-back if approved — in the background. Duplicate deliveries for the same issue are automatically skipped within the server's lifetime.

**Webhook-triggered runs never auto-approve.** There's no human to prompt in a background webhook handler, so it always uses the safe-default approver — any report needing sign-off (a rollback recommendation, or sev1/sev2) stays blocked in the review queue (`reports/<id>_BLOCKED.json`) exactly as if it had been rejected. A person still has to review it — via the dashboard's real-Jira mode, or `python -m src.cli analyze-incident --interactive` — and re-run the analysis with an actual approval before it publishes. The webhook automates *detection and analysis*, not the approval decision.

---

## Design principles

1. **Fail closed, not open.** A node that can't produce a grounded citation doesn't guess — it routes to "insufficient evidence."
2. **Confidence changes *what*, never *whether*.** High confidence affects the content of a recommendation; it never bypasses the human-approval gate for rollback or sev1/sev2 incidents.
3. **Least-privilege tools.** Specialist agents get narrow MCP tool allowlists — a log-analysis agent cannot write to Jira.
4. **Everything is auditable.** Every graph run keeps a decision log and a token/cost trail, in the same spirit as the prior repo's FinOps dashboard.

See [`DEMO.md`](./DEMO.md) for a full walkthrough with real, captured command output.
