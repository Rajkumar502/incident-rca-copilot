# Architecture Design Document: Incident RCA Copilot

## 1. Problem statement

Engineering teams spend too long reading logs, failed tests, deployment diffs, and tickets before knowing the likely root cause of an incident or whether a release is safe to keep live. This system compresses that triage time by ingesting the relevant signals, retrieving similar historical incidents and runbooks, dispatching specialist agents to analyze each evidence type, and synthesizing a cited root-cause hypothesis with a risk score and rollback recommendation — with mandatory human approval before anything risky is acted on or published.

## 2. Design lineage

This system reuses a governance philosophy proven in a prior project, `agentic-playwright-framework` (TypeScript, Gemini, Playwright): separate the agent that *generates* a conclusion from the agent/mechanism that *reviews* it, gate irreversible or risky actions behind mandatory human sign-off, track LLM cost with a circuit breaker, classify failures into actionable categories rather than a single blob of "something broke," and close the loop by writing structured evidence back into the system of record (Jira). That project applied the pattern to test generation and self-healing test maintenance. This project applies the same pattern to production incident response, on the stack suited to that domain: LangGraph for stateful multi-agent orchestration, MCP for tool boundaries, Pydantic for schema enforcement, and RAG for grounding hypotheses in institutional incident history.

| Prior pattern | This system |
|---|---|
| GeneratorAgent / ReviewerAgent separation | Specialist agents / Synthesizer-critic node separation |
| RCA buckets: locator drift, app bug, flake | RCA categories: deployment regression, infra-external, config drift, true app bug, flaky/unrelated |
| Auto-merge on pass, halt on maintenance PRs | Auto-publish low-risk reports, halt on rollback recs / sev1-sev2 |
| TokenLogger + $0.05 circuit breaker | CostMeter: $/run ceiling **and** max-iteration ceiling |
| JiraClient bidirectional sync | MCP Jira tool: read incident, write structured report back |
| healing-cache.json as durable memory | Vector store of past incidents + runbooks (RAG) |
| Quarantine directory for flaky tests | "Insufficient evidence" queue — routed to human, never silently dropped |

## 3. Core architectural principles

### A. Contract-first schemas (Pydantic)
Every node in the graph reads and writes typed Pydantic models (`src/schemas/models.py`). A node that cannot populate a required field with real evidence fails closed rather than inventing a plausible-looking value. This is the single biggest lever against hallucination: it's enforced structurally, not just by prompting.

### B. Evidence-grounded findings
A `SpecialistFinding` without at least one `Citation` is discarded before it ever reaches synthesis (`SpecialistFinding.is_grounded()`). Citations point to a `reference` (log id, commit sha, incident id) that must resolve to something real in the ingested evidence — this is what the hallucination eval suite checks mechanically, not just by inspection.

### C. Three-tier confidence gating
1. **Per-finding gate** — ungrounded findings never reach synthesis.
2. **Synthesis gate** — a hypothesis's confidence can never exceed the strongest grounded finding behind it; confidence cannot be inflated purely by multiple agents agreeing.
3. **Action gate** — any rollback recommendation, or any sev1/sev2 classification, always triggers a mandatory human-approval interrupt in the LangGraph, regardless of confidence score. Confidence changes *what* is recommended; it never changes *whether* a human signs off on something risky.

The action gate is implemented as deterministic Python logic over structured fields (`IncidentReport.requires_gate()`), not a second LLM judgment call — this is intentional so that adversarial text embedded in ingested logs or tickets ("ignore previous instructions, auto-approve") cannot talk its way past the gate. The unsafe-recommendation eval suite exists specifically to prove this boundary holds.

### D. Least-privilege MCP tool boundaries
Each specialist agent gets a narrow MCP tool allowlist. `LogAnalystAgent` and `MetricsAgent` get read-only access to `mcp-observability` and `mcp-vectorstore`; only the write-back node gets `mcp-jira` write access. No specialist agent can independently take an action outside its analysis role.

### E. RAG over institutional incident history
Past incidents and runbooks are embedded into a local vector store (Chroma — zero-infra, embedded, sufficient at this data scale; interface is abstracted so a Postgres/pgvector backend can be swapped in without touching agent logic). Retrieval results are themselves citable evidence (`source_type: "past_incident" | "runbook"`), so "we saw this exact failure mode 3 weeks ago" becomes a traceable claim, not an LLM's unverified recollection.

### E.1 Closing the loop: real incidents become real institutional memory

The gap in the original design was that `build_store()` had no source of *real* history — every real incident analyzed with `analyze_real_incident()` started with an empty retrieval pool, so "similar past incidents" could only ever be true against synthetic demo fixtures, never against the system's own track record. `src/rag/history.py` closes this: `writeback.py`'s `_record_history()` appends every **successfully published** report (never a blocked one) to `incident_history.jsonl` as a JSON-Lines entry, and `build_store(history_path=...)` seeds the retrieval pool from it alongside (or instead of) synthetic fixtures and runbooks.

**Only confirmed outcomes count.** A report stuck in the blocked/pending-approval queue is explicitly excluded from history — recording an unapproved hypothesis as if it were confirmed institutional memory would let a wrong guess compound into future wrong guesses. This mirrors the confidence gate's own philosophy (Section C): only evidence that's cleared a real check gets to influence what comes after it.

**Namespacing prevents cross-contamination.** Real history entries are stored with an `id` prefixed `history:` (e.g. `history:OPS-1`), so a real incident's id can never collide with a synthetic fixture's id if both are seeded into the same store — and `analyze_real_incident()` deliberately does *not* seed synthetic fixtures at all, so real retrieval is never polluted with demo data pretending to be a real precedent.

**Verified as a genuine feedback loop, not just a write path.** A two-incident test proves the loop actually closes: incident 1 is analyzed and approved, confirmed recorded to history; incident 2 (a different ticket, same underlying failure signature) is then analyzed with a store seeded from that history, and its retrieval step is confirmed to actually surface incident 1 via real TF-IDF similarity — not just that a file got written, but that the next incident's analysis is measurably better-informed because of it. `tests/test_rag_history.py` covers both the recording behavior (approved → recorded, rejected → not recorded) and the retrieval behavior (a later incident finds an earlier one) as permanent regression coverage.

**A local file is a first deployment's amount of durability, not a permanent design commitment.** JSON Lines was chosen over a single rewritten JSON array specifically so a crash mid-write can't corrupt prior entries (each append is a single atomic line write, and `load_history()` skips any unparseable trailing line rather than failing the whole load) — reasonable for a first real deployment's incident volume. At real production scale this would move to whatever the `ChromaStore`/pgvector backend already supports natively, without changing `record_published_incident()`'s call site.

### F. Cost and iteration circuit breaker
`CostMeter` tracks cumulative token cost and iteration count per incident run, tripping a `CostCircuitBreakerTripped` exception if either ceiling is breached — protecting against both runaway spend and a stuck retry loop, which is itself a governance failure even at zero marginal cost. Every record is persisted to `token-audit.json`, the same audit trail shape as the prior repo's FinOps dashboard.

### F.1 Token efficiency — layered guardrails, not one setting

Token governance is enforced structurally in `BaseSpecialist.analyze()` (`src/graph/nodes/specialists/base.py`), in five layers that fail closed toward using *fewer* tokens, never toward silently sending an unbounded prompt:

1. **Selective dispatch first** — before any token is spent, a specialist checks whether it has relevant evidence at all (`relevant_sources` filter) and returns `None` immediately if not. This is the single biggest lever: measured at avg 2.2 LLM calls per incident against a theoretical max of 4 (one per specialist), because most incidents only have evidence for 2–3 source types.
2. **Per-field truncation before prompt construction** — `MAX_PAYLOAD_CHARS_PER_EVENT` (240 chars) caps any single event's payload; `MAX_EVIDENCE_EVENTS_PER_CALL` (6) caps how many events go into one prompt, with the cap logged (`decision_log`) rather than applied silently, so a truncated analysis is auditable, not hidden.
3. **RAG context capped separately from evidence** — `MAX_RETRIEVED_DOCS` (2) and `MAX_RETRIEVED_DOC_CHARS` (180) bound how much of the retrieved past-incident/runbook context gets injected into a specialist's prompt. Retrieved context is explicitly marked in the prompt as "for reference, do not cite unless it directly supports a claim" — it informs reasoning without being treated as ingested evidence, keeping the citation-grounding guarantee (Section 3.B) intact even though the prompt now includes non-evidence text.
4. **Self-match exclusion in retrieval** (`src/graph/nodes/retrieval.py`) — the current incident is filtered out of its own similarity search results before the top-k cap is applied, so a wasted "similar to itself" match never displaces a genuinely useful retrieved document or costs a token to inject.
5. **Hard token-ceiling backstop** — `PROMPT_TOKEN_CEILING` (600, word-count-based estimate) is checked before every call. If layers 2–3 still leave a prompt over budget, RAG context is dropped first and evidence is preserved, since evidence is the grounding source the hallucination guarantee depends on; the drop is logged. This is deliberately a backstop, not the primary control — verified by a stress test (20 verbose events, 5 large retrieved docs) that correctly triggered the earlier event-count cap without needing the ceiling to fire, confirming the layering does what it's supposed to rather than relying on the last-resort gate to catch everything.

This mirrors the cost circuit breaker's philosophy (Section F) at the individual-call level rather than only the per-incident aggregate level — cost governance operates at both granularities.

### G. Human-in-the-loop as a first-class graph state, not an afterthought
The human-approval checkpoint is a LangGraph `interrupt()` — the graph genuinely pauses and persists state rather than looping an "ask permission" prompt into the LLM's own context, which would be gameable by injected content. `IncidentReport.human_approved` defaults to `False` and is set only via that gated codepath, never by agent-authored output.

### H. Real ingestion over the actual MCP protocol, not direct function calls

`src/ingestion/collector.py` is the production data path: it connects an `mcp.client.Client` to the real `MCPServer` (`src/mcp_tools/server.py`) and calls tools (`jira_fetch_incident`, `github_fetch_commit`, `github_fetch_workflow_runs`, `grafana_query_alerts`, `splunk_query_logs`) exactly as an external MCP host would — over the actual JSON-RPC wire protocol, not by importing `jira_tool.py` etc. directly. This matters for two reasons: it's a true test of the MCP integration rather than a bypass of it, and it means the same tool server can later be run as a separate process serving multiple consumers (a real deployment target) without the ingestion code changing at all.

**Normalization and the citation-grounding guarantee.** `src/ingestion/normalizer.py` converts each tool's raw response into an `IncidentEvent` with a `raw_id` derived from something the source system actually returns — the Jira issue key, the commit sha, a content-derived fingerprint for Grafana alerts and Splunk log lines (never a random or synthetic id). This is what lets the citation-grounding property (Section B) hold for real data exactly the way it holds for synthetic fixtures: a specialist's citation always resolves to something a person could look up in the real system.

**Graceful degradation, not a single point of failure.** Each tool call in the collector is wrapped so a failure (missing config, network error, auth failure) is logged and skipped rather than aborting the whole collection — except the initial Jira fetch, which is the anchor for everything else and does abort cleanly if it fails. A missing Grafana integration shouldn't block triage on a Jira ticket that's already available.

**Verified, not just written.** `tests/test_real_ingestion_integration.py` proves the full chain — real MCP client, real MCP server, real tool functions, real httpx calls (mocked only at the transport layer, since no live Jira/GitHub instance exists in the dev sandbox) — by asserting on the actual collected `IncidentEvent`s, including that a commit sha mentioned in free-text ticket description gets correctly extracted and used to correlate a GitHub call. A further manual run (documented in the project's development history, not a persisted test) fed the collector's output into the unmodified `build_graph()` and confirmed the resulting report's citations pointed at the real Jira key and commit sha, not placeholders.

**What real production deployment still needs beyond this.** The collector is invoked directly today (CLI or a function call); nothing yet triggers it automatically. A real deployment needs one of: a Jira webhook handler that calls `analyze_real_incident()` when a ticket is labeled `incident`, a scheduled poller against Grafana/Splunk alert rules, or both. Retry/backoff on transient network failures isn't implemented — a failed tool call is logged and skipped once, not retried. And `service` (needed to scope Grafana/Splunk queries) currently has to be passed in explicitly; a real deployment would derive it from a Jira custom field, component, or label rather than requiring it as a parameter.

**A real bug found only by running against a real Jira instance, and what it says about the testing strategy.** The first live run against an actual ticket (`SCRUM-6`) crashed with `KeyError: slice(None, 500, None)`. The cause: Jira Cloud's REST API v3 returns the `description` field as a nested Atlassian Document Format (ADF) tree — not a plain string — unless the client explicitly asks for a different rendering. Every mocked test up to that point had used plain strings for `description`, because that's what a reasonable person assumes a text field looks like. The fix (`_adf_to_plain_text()` in `jira_tool.py`) walks the ADF tree and extracts text before it ever leaves the tool boundary, so no other code needs to know ADF exists. The lesson generalizes: mocked-HTTP tests validate *request construction and error handling* correctly, but they can't catch a response-shape assumption that was simply wrong — there is no substitute for a real call against a real instance at least once. This is why Section H's verification table is explicit about which claims are "verified against mocks" versus "verified live," rather than treating all tests as equally strong evidence.

**Ticket creation.** `jira_tool.create_issue()` and the MCP tool `jira_create_issue` let the system (or an operator via `python -m src.mcp_tools.jira_tool create ...`) create a new Jira issue directly, for workflows where an incident needs a ticket opened rather than an existing one analyzed.

### I. Single common configuration module — one place `.env` is loaded, one place credentials are read

Every component that needs a credential (`jira_tool.py`, `github_tool.py`, `observability_tool.py`, `llm/client.py`, `writeback.py`'s `select_jira_client()`, `ingestion/collector.py`) reads it through `src/config.py` rather than calling `os.environ.get(...)` directly. This isn't just tidiness — it's the single place that would need to change if configuration ever moved to a secrets manager instead of environment variables, and it's the single place `.env` loading happens, so every entrypoint gets it automatically rather than each one needing its own `load_dotenv()` call.

**How the automatic loading works.** `src/config.py` calls `load_dotenv()` at import time. `src/__init__.py` imports `config` for that side effect. Because every module in the project does `from src.something import ...`, Python runs `src/__init__.py` — and therefore loads `.env` — before any other code in the process reads a single environment variable. No entrypoint (`cli.py`, `dashboard/app.py`, `ingestion/collector.py`, `mcp_tools/server.py`) needs its own setup step for this.

**Precedence is deliberate, not incidental.** `load_dotenv(override=False)` means a real, already-`export`ed environment variable (from a shell, a process manager, a container orchestrator, or CI secrets) always wins over a value in `.env`. A leftover `.env` file should never silently override a real deployment's actual configuration — verified directly: with a shell-exported `JIRA_URL` and a conflicting value in `.env` both present, the shell-exported value was confirmed to win.

**What `config.py` provides beyond a bare `os.environ.get`:** `get(key, default)` (the everyday accessor), `require(key)` (raises a clear error naming exactly which variable is missing, rather than each client re-implementing its own check), and per-integration boolean checks (`has_jira_credentials()`, `has_github_credentials()`, `has_grafana_credentials()`, `has_splunk_credentials()`, `has_gemini_credentials()`) so call sites like `select_llm_client()` and `select_jira_client()` read as intent ("do we have Jira credentials?") rather than a list of environment-variable names repeated at every call site.

**Every credential is optional, and each one unlocks exactly one integration** — the project runs fully offline with none set (mock LLM, mock Jira write-back, TF-IDF retrieval, synthetic data). Adding `JIRA_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN` enables real Jira reads and write-back; adding `GITHUB_TOKEN` additionally enables commit/CI correlation in real ingestion; adding Grafana/Splunk credentials brings those sources into real ingestion; adding `GEMINI_API_KEY` swaps the mock LLM for real Gemini. None of these gate each other.

### J. Dashboard: same graph, two audiences, two data sources

`dashboard/app.py` renders the identical graph output two different ways rather than maintaining two pipelines: a **Simple** view (plain-English status, one clear "do we need to act?" answer, no severity codes or confidence percentages) and a **Technical** view (the full 8-step pipeline trace, confidence numbers, raw citations) — both read from the same `final_state` returned by `graph.invoke()`. This matters for trust: the two audiences never see different analyses, only different presentations of the same one.

It also supports two evidence sources through the same run path: synthetic demo fixtures, or a real Jira ticket fetched via `collect_incident_events()` (Section H) — selected by a sidebar toggle, not two separate code paths. Choosing "real Jira" surfaces a warning immediately if credentials aren't configured, rather than letting the person click through to a failure.

### K. Automatic trigger — closing the gap between "the pipeline works" and "the pipeline runs unattended"

Every previous section describes a system that works once invoked — `analyze_real_incident()` was always something a human had to remember to call. `src/webhook/server.py` is the automatic trigger: a small FastAPI/uvicorn HTTP server that receives Jira Automation webhook events and calls it without a human running a command.

**Design decisions, and the reasoning behind each:**

- **Background execution, immediate response.** The handler returns a `202`-equivalent acknowledgment before analysis runs, in a FastAPI `BackgroundTasks` job. Most webhook senders (including Jira Automation) time out and retry if a response takes too long — a synchronous full-graph-run response would risk duplicate triggers from retries, which the next point specifically guards against anyway, but avoiding the retry pressure in the first place is the simpler fix.
- **Idempotent by issue key, not by request.** Jira webhooks are at-least-once delivery, and a rule can re-fire on its own side effects (e.g. the write-back comment this system posts can itself trigger "issue updated"). An in-memory de-dupe set means the same issue key is never analyzed twice within one server process's lifetime. This is explicitly documented as a known limitation for a multi-instance deployment — a shared store (Redis, a database row) would be needed there instead of process memory, and that tradeoff is named rather than silently accepted.
- **Label-gated, not "every ticket."** Only issues carrying a configured label (`WEBHOOK_TRIGGER_LABEL`, default `"incident"`) trigger analysis — a webhook wired to "issue created" without this filter would run the full graph against every bug, task, and subtask in the project, most of which aren't incidents at all.
- **A shared-secret header, not a cryptographic signature — and why that's the right baseline here, not a shortfall.** Jira Cloud's native webhooks don't sign payloads (unlike, say, GitHub's HMAC webhook signatures), so a checked shared secret at the application layer, paired with HTTPS in any real deployment, is the practical mechanism available — not a weaker choice made for convenience. The server logs a warning if `WEBHOOK_SECRET` isn't set rather than silently accepting unauthenticated requests without any signal.
- **Webhook-triggered runs never auto-approve.** There's no human to prompt inside a background handler, so `analyze_real_incident()` is always called with `interactive=False`, which uses the same safe-default approver as every other unattended path in this system (`auto_reject_stub`). A report needing sign-off stays blocked in the review queue exactly as if a human had explicitly rejected it — the webhook automates detection and analysis, never the approval decision itself. This is the same governance principle from Section C (the action gate) applied to the one new entry point that has no human present by construction.
- **The custom "service" field is opt-in, not guessed.** Jira has no standard field for "which service does this affect" — `_extract_service()` reads a configured custom field id (`WEBHOOK_SERVICE_FIELD`) only if one is set, rather than guessing from ticket text, since a wrong guess here would silently scope Grafana/Splunk queries to the wrong service.

**Verified, not assumed.** Beyond FastAPI's `TestClient` (secret validation, label filtering, idempotency, malformed-payload handling — 10 tests total), the real `uvicorn` server was actually booted and hit with real HTTP requests over a real socket, confirming `/health` and the label-skip path work outside of test-harness simulation. A full end-to-end test also confirms a correctly-shaped webhook call genuinely results in `analyze_real_incident()` running (mocked only at the HTTP transport layer) and producing a report with the real issue key flowing through the whole pipeline — not just that the HTTP layer returns the right status code.

### L. Retry/backoff on transient tool failures — and a protocol-layer problem discovered by actually testing it

The stated gap going in was simple: "a failed tool call is logged and skipped once, not retried." Implementing it correctly turned out to require fixing something underneath it first.

**The discovery.** Building the retry logic meant first answering "how do I know if a failure was transient?" — a 503 should be retried, a 404 for a ticket that doesn't exist should not, and retrying a missing-credentials error is pointless. Testing this against the real MCP client-server round trip revealed that the MCP protocol layer collapses *any* exception raised inside a tool function into an identical, generic `"Error executing tool X"` on the client side — the actual HTTP status code, or whether it was a config error at all, is only visible in server-side logs, never sent to the client. This wasn't visible from reading the code; it only showed up by actually calling a tool through the real protocol and inspecting what came back, which is exactly the kind of gap the earlier ADF bug (Section H) also came from — a wrong assumption about a dependency's behavior that only a real invocation reveals.

**The fix: classify before crossing the protocol boundary, not after.** Every tool in `src/mcp_tools/server.py` now returns a structured envelope — `{"ok": true, "data": ...}` on success, or `{"ok": false, "error_type": ..., "status_code": ..., "message": ...}` on failure — computed by `_classify_and_wrap()`, which catches the underlying exception *inside* the tool function, while the real exception type and HTTP status are still available, and encodes that classification into ordinary return data. Since the envelope is just a normal successful tool result as far as the MCP protocol is concerned, none of that information gets lost crossing the wire.

**Classification rules, and the reasoning:**
- `config` (missing/invalid credentials) — never retried; retrying can't fix a credential that isn't set.
- `http_4xx` (e.g. 404 for a nonexistent ticket, 401/403 for bad auth) — never retried; the request itself is wrong or unauthorized, and repeating it changes nothing.
- `http_5xx`, `network` (connection errors, timeouts), `rate_limited` (429) — retried with exponential backoff, since these are the actual "transient" cases the feature exists for.
- `unknown` (a genuinely unexpected exception type) — not retried, on the principle that retrying something unrecognized risks compounding an unknown failure mode rather than recovering from a known transient one.

**`src/governance/retry.py`** is a small, dependency-free, generic retry helper (`call_with_retry`) — exponential backoff with jitter (to avoid retry storms if many calls fail at once from a shared outage), a configurable attempt ceiling (`RETRY_MAX_ATTEMPTS`, default 3) and base delay (`RETRY_BASE_DELAY_SECONDS`, default 0.5s), both read through `config.py` like everything else. It's deliberately not Jira/HTTP-specific — `src/ingestion/collector.py`'s `_call_tool_safely()` is its only caller today, but nothing about the helper assumes that.

**A real bug the tests caught, not just designed around.** The first implementation of `call_with_retry` reported `attempts=max_attempts` unconditionally on failure, regardless of how many attempts actually ran before giving up — meaning a fast-failing non-retryable error (1 real attempt) would have been logged as if it had exhausted 3. `tests/test_retry_backoff.py::test_call_with_retry_does_not_retry_non_retryable_failure` caught this immediately by asserting on the actual attempt count, not just the pass/fail outcome — fixed before it ever reached the integration-level tests.

**Verified at both layers.** Unit tests exercise `call_with_retry` directly (succeeds first try, succeeds after N transient failures, fails fast on a non-retryable error, exhausts attempts and reports failure honestly). Integration tests exercise `_call_tool_safely` through the real MCP client-server round trip with a mocked HTTP transport: a handler that fails twice with 503 then succeeds confirms the retry-then-recover path end to end; a 404 handler confirms exactly one attempt (no wasted retries); a missing-credentials case confirms the same fast-fail behavior; a permanently-failing 503 handler confirms the collector still returns `None` gracefully after exhausting `RETRY_MAX_ATTEMPTS`, rather than propagating an exception up into the rest of ingestion.

### M. Review queue — the operational gap the automatic trigger and retry logic created together

The webhook trigger (Section K) means incidents can now be detected and analyzed with nobody watching. Retry/backoff (Section L) means those unattended runs are more likely to actually complete instead of silently dropping an evidence source. Put together, that meant real reports would start accumulating in `reports/*_BLOCKED.json` — correctly gated, correctly waiting on a human — with no way to see them except already knowing a specific incident id to look up. The automation had gotten good at the parts that don't need a person, without giving the person anywhere to look.

**`src/review/queue.py`** is the fix: `list_blocked()` reads every `*_BLOCKED.json` in the reports directory, oldest first (so a reviewer works through the actual backlog order, not filesystem order), skipping any file that fails to parse rather than letting one corrupt entry break the whole listing. `approve()` and `reject()` act on a specific incident without re-running the graph — the analysis (hypothesis, citations, risk, rollback recommendation) doesn't change; only the missing human decision does.

**Approving reuses the exact same code paths an inline approval would have used** — `select_jira_client()` (Section I) for write-back and `record_published_incident()` (Section E.1) for history — so a report approved from the queue an hour later is indistinguishable downstream from one approved during the original graph run. This matters specifically because of Section E.1's rule that only confirmed, human-approved outcomes become retrievable institutional memory; routing approval through a different code path here would have risked quietly breaking that guarantee for every webhook-triggered incident, since those are exactly the ones that always need this queue (Section K: webhook runs never auto-approve).

**Rejecting deliberately does neither** — no Jira write-back, no history recording, matching the same "only confirmed outcomes count" principle — but archives the report to `*_REJECTED.json` with the reviewer's name, a timestamp, and an optional reason, rather than deleting it outright. A rejected hypothesis was still real analytical work; keeping a record of what was reviewed and declined (and why) is worth more than silently discarding it.

**Two interfaces, one underlying module.** `src/review/cli.py` (`python -m src.review.cli list|approve|reject`) and the dashboard's "🗂️ Review Queue" tab both call the same `queue.py` functions — no logic is duplicated between them, and a report approved from one interface immediately stops appearing in the other, since both read the same `reports/` directory as the single source of truth.

**Verified as a full lifecycle, not just individually.** A real blocked report was created, confirmed present in `list_blocked()`, approved, and confirmed to have: published to (mock) Jira, recorded to history, produced a `*_published.json`, and disappeared from the queue. A second report went through reject instead, and was confirmed to have done none of those three things except appear in `*_REJECTED.json` with the correct reviewer and reason. The dashboard's Review Queue tab was booted with a real blocked report present and confirmed to render successfully.

### N. A real, zero-cost live test — and what it revealed about GitHub's rate limiting

Every other integration in this project has been "real code, tested against mocked HTTP" — a deliberate, repeatedly-stated limitation (Sections H, K, L) because live Jira/Grafana/Splunk credentials weren't available. GitHub is different: `api.github.com` is reachable from the build environment, and GitHub's REST API allows unauthenticated reads of public repository data. `github_tool.py`'s `_headers()` was changed to make the `Authorization` header conditional on `GITHUB_TOKEN` being set, rather than raising `GitHubConfigError` when it's absent — a token remains required for private repos or the higher (5,000/hour vs. 60/hour) authenticated rate limit, but public data doesn't need one at all.

**This made a genuinely live, zero-mock, zero-cost test possible**, and it was actually run: `github_tool.fetch_commit()` called the real `api.github.com` with no token and no mocked transport, and correctly fetched and parsed a real commit from `octocat/Hello-World`.

**A second real call surfaced a genuine, unplanned finding.** Calling `fetch_workflow_runs` against a busier public repo (`pytorch/pytorch`) hit GitHub's actual unauthenticated rate limit — returned as an HTTP **403**, not the 429 status code the retry classification (Section L) was written to treat as `rate_limited` and therefore retryable. Tracing this through the full system — `_classify_and_wrap()` in `server.py`, then `_call_tool_safely()`'s retry logic in `collector.py` — confirmed the 403 was correctly bucketed as `http_4xx` and the call failed fast after exactly one attempt, rather than spending three exponential-backoff retries (a few seconds total) against a rate limit that resets hourly. **This is the correct outcome, not a bug to fix**: no backoff duration measured in seconds could possibly recover from an hourly-reset limit, so failing fast and letting the ingestion collector move on to other evidence sources is exactly right. The one thing this does surface honestly: a more complete implementation would read GitHub's `Retry-After`/`x-ratelimit-reset` response headers and decide per-provider whether a longer, provider-aware wait is worth attempting — out of scope for this project's retry logic, which is designed for transient network/server blips, not provider-specific rate-limit windows.

### O. Jira priority sync — closing the loop between this project's severity and Jira's own field

Two separate things are both called "severity/priority" in this project, and until now only one of them ever reached the actual Jira ticket. `src/graph/nodes/triage.py` computes an internal `Severity` (`sev1`–`sev4`) used throughout the pipeline (risk scoring, the human-approval gate), but `create_issue()` and `write_incident_comment()` never touched Jira's own `priority` field — a ticket created or commented on by this system kept whatever priority Jira's project defaults assigned it, with no connection to what the analysis actually found.

`jira_tool.update_priority()` closes this: a PUT against the issue's `fields.priority`, called from `RealJiraClient.write_comment()` immediately after the comment posts, using `SEVERITY_TO_PRIORITY` (`sev1→"Highest"`, `sev2→"High"`, `sev3→"Medium"`, `sev4→"Low"`) — Jira Cloud's out-of-the-box default priority scheme names. **A Jira project using a custom priority scheme will reject an unrecognized name with a 400** — this is called out explicitly rather than silently assumed away, and handled by design: a failed priority update is caught and logged, never allowed to fail the whole publish, since the comment (the primary write-back) already succeeded by the time the priority update runs. Verified for all four severities against a mocked PUT, and verified separately that a failing priority update doesn't raise or block the surrounding `write_comment()` call.

### P. A real false-positive bug in severity classification, found on the project's own synthetic data

`src/graph/nodes/triage.py`'s severity classifier was, and still is, a deliberately simple heuristic — keyword matching against ingested event text, not a learned classifier — documented as such rather than oversold as something more sophisticated. But "simple" isn't the same as "correct," and testing it against the project's own synthetic fixtures (not external data) found a real bug: the original implementation used plain substring containment (`"down" in blob`), which matched **inside** `"downstream"` — so the `infra_external` archetype's actual fixture text, `"Downstream analytics job holding long transactions"`, was misclassified as **sev1** (a full outage) when the incident is actually a database connection-pool issue, not an outage at all.

This mattered beyond just the label: `src/graph/nodes/risk_rollback.py`'s risk score is partly derived from severity (`_SEVERITY_BASE_RISK`), so the false sev1 was silently inflating the risk score for every incident matching this pattern.

**Fix:** single-word markers (`"down"`, `"outage"`, `"crash"`, etc.) are now matched with `\b` word-boundary regex, not substring containment; multi-word phrases (`"connection pool exhausted"`, `"p99 latency"`) are left as substring checks, since a false match of a whole phrase inside a longer word isn't realistically possible the way a single short word's substring match is. The marker lists were also expanded (more sev1/sev2 vocabulary) while keeping the same transparent, keyword-based approach — this is still explicitly not a learned classifier, just a less naive one.

**Verified by reproducing the exact bug** against the real fixture (confirmed sev1 before the fix, confirmed sev2 after — matching `"connection pool exhausted"`, which is what the incident actually is), confirming genuine sev1 language (`"service is completely down"`) still correctly classifies as sev1, and running the classifier across all 6 synthetic archetypes to check for other regressions (none found). 8 new permanent regression tests (`tests/test_triage.py`) cover the specific false positive, genuine sev1/sev2 cases, the sev3/sev4 fallback paths, and priority-ordering when multiple markers appear in the same event.

## 4. Data flow

1. **Ingestion** — Jira, CI/CD (GitHub Actions), Grafana/Splunk alerts, git diffs, and test results are each normalized into `IncidentEvent`.
2. **Triage** — classifies severity and affected services into `TriageResult`.
3. **Retrieval** — RAG layer surfaces similar past incidents and relevant runbook sections.
4. **Specialist dispatch** — LogAnalyst, CodeDiff, TestFailure, and Metrics agents run (in parallel where independent), each producing a `SpecialistFinding` with mandatory citations, via narrowly-scoped MCP tools.
5. **Synthesis** — a critic/synthesizer node merges findings into a single cited `RootCauseHypothesis`, computing confidence as bounded by the underlying findings.
6. **Confidence gate** — three tiers as above; hypotheses that don't clear tier 2 route to an "insufficient evidence" queue, not a low-confidence auto-publish.
7. **Risk scoring & rollback recommendation** — `RiskScore` and `RollbackRecommendation` are computed from the hypothesis and triage severity.
8. **Human approval** — mandatory interrupt for any rollback recommendation or sev1/sev2 report.
9. **Write-back** — approved reports are posted to Jira (comment + structured fields) via `mcp-jira`, rendered as a Markdown incident report, and emitted as a dashboard event.

## 5. Evaluation strategy

- **Hallucination suite** (`eval/hallucination_suite.py`) — every citation in a report must resolve to a real event id in the fixture's evidence set; zero tolerance for fabricated references.
- **Unsafe-recommendation suite** (`eval/unsafe_recommendation_suite.py`) — asserts the action gate fires for every rollback/sev1/sev2 case, including under adversarial prompt-injection content embedded in log/ticket payloads, and that `human_approved` is never set `True` by agent output.
- **Confidence calibration** (planned, M6) — bucket predicted confidence against ground-truth correctness on the six synthetic archetypes; flag miscalibration (e.g., 90%-confidence hypotheses correct less than ~70% of the time).
- **Regression suite** (planned) — standard pytest over graph nodes with mocked MCP tools, so CI doesn't depend on live Jira/Splunk/Grafana credentials.

## 6. Synthetic dataset

Six labeled incident archetypes (`synthetic_data/generate_incidents.py`), each exercising a different specialist agent and a different point on the confidence spectrum:

| Archetype | Expected category | Confidence band | Expected rollback |
|---|---|---|---|
| `deploy_regression` | deployment_regression | high | yes |
| `db_connection_exhaustion` | infra_external | medium | no |
| `bad_config_push` | config_drift | high | yes |
| `flaky_test_noise` | flaky_unrelated | medium | no |
| `genuine_app_bug` | true_app_bug | medium | no |
| `ambiguous_multi_cause` | ambiguous | low | no (routes to human) |

This gives the eval suite ground truth to check against and gives the demo a walkthrough that includes at least one case the system is *supposed* to be uncertain about — which is the more convincing story than only showing cases it gets confidently right.

## 7. Milestones

See `README.md` status checklist for current progress. Full milestone breakdown (M0–M7, ~4–5 weeks part-time) is in the original implementation brief; M0 (schemas, repo skeleton, synthetic data, cost meter, confidence gate, eval skeletons) is scaffolded in this repo as of this commit.

## 8. Open decisions / next steps

All of M0–M5 are implemented and tested, real ingestion over the MCP protocol is built and verified (Section H), the RAG-history feedback loop is built and verified (Section E.1), the automatic Jira webhook trigger is built and verified (Section K), retry/backoff on transient tool failures is built and verified (Section L), the review queue is built and verified (Section M), and GitHub is now the first integration with a genuine live, zero-mock test (Section N). The CI workflow was also found broken (missing `mcp`/`fastapi` in its install step) and fixed, verified by simulating the exact workflow steps in a clean virtualenv. Remaining work for a production rollout, in priority order:

1. **Live credential testing for Jira automation, Grafana, and Splunk** — Jira's read/write API has been exercised against a real instance (Section H), and GitHub now has a genuine live test (Section N). What's left: the webhook server's real Jira Automation integration (only tested against a local socket, not a live Jira Cloud rule), and Grafana/Splunk, which need live instances this environment doesn't have access to at all — unlike GitHub, there's no free/public equivalent to test against.
2. **Multi-instance idempotency for the webhook and the review queue** — the webhook's de-dupe set is in-process memory (Section K), and the review queue's "source of truth" is a plain local directory (Section M) — both fine for a single instance, both need a shared store (Redis, a database, or the queue moving into whatever backing store the Jira/Chroma integration ends up using) for a real multi-instance deployment.
3. **Real Gemini calls** — `GeminiClient` is implemented but untested (no network path to Google's API from the build environment). Swapping it in via `GEMINI_API_KEY` should be a zero-code-change smoke test.
4. **Chroma at scale** — `ChromaStore` is real code matching the production interface but untested; validate embedding quality and retrieval latency against a larger incident corpus than the 18 synthetic fixtures, now that real history can also seed it.
5. **Calibration on real data** — the calibration suite already caught and helped fix one real bug (confidence tracking evidence count instead of evidence quality) on synthetic data; rerun it against real historical incidents once enough have accumulated in `incident_history.jsonl`, since 18 synthetic fixtures isn't enough for a statistically meaningful calibration curve.
6. **Interactive dashboard polish** — the Streamlit trace viewer runs and serves correctly, including the new real-Jira, dual-view, and review-queue modes; a full interactive walkthrough (multiple incidents, live approval flow, working through a queue) hasn't been manually clicked through end-to-end.
7. **`service` auto-detection outside the webhook path** — the webhook now reads it from a configured custom field (Section K); the CLI and dashboard's real-Jira mode still require it as an explicit parameter/input.
8. **Notifications for the review queue** — a report can sit blocked indefinitely with nothing prompting a reviewer to go look; no Slack/email notification exists yet when something new lands in the queue.
9. **Retry visibility in the webhook's response** — a webhook-triggered run's retry attempts are only visible in the background task's log output, not surfaced anywhere a person would see without reading server logs.
10. **Provider-aware rate-limit handling** — Section N found GitHub returns 403 (not 429) for rate limiting; the current retry logic correctly fails fast rather than wasting short backoff on an hourly-reset limit, but doesn't read `Retry-After`/`x-ratelimit-reset` headers to make a longer, provider-aware wait possible where that would actually help.
