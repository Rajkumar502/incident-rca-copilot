"""
Observability dashboard — with two view modes.

Run: streamlit run dashboard/app.py

Simple view:    plain-English summary for non-technical stakeholders —
                 one traffic-light status, one sentence of what happened,
                 one clear "do we need to act?" answer. No jargon, no
                 internal pipeline steps, no raw citations.
Technical view:  the detailed trace — severity codes, confidence numbers,
                 the 8-step pipeline walkthrough, and raw evidence
                 citations — for engineers who want to verify the work.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.governance.cost_meter import CostMeter
from src.graph.build_graph import build_graph
from src.llm.client import MockLLMClient, select_llm_client
from src.rag.retriever import build_store_from_synthetic
from src.schemas.models import IncidentEvent

SYNTHETIC_PATH = Path(__file__).parent.parent / "synthetic_data" / "generated" / "synthetic_incidents.json"
RUNBOOKS_DIR = Path(__file__).parent.parent / "synthetic_data" / "seed_runbooks"

st.set_page_config(page_title="Incident RCA Copilot", layout="wide", initial_sidebar_state="expanded")

st.markdown(
    """
    <style>
    .step-title { font-size: 1.1rem; font-weight: 600; margin-top: 0.5rem; }
    .step-desc { color: #666; font-size: 0.9rem; margin-bottom: 0.5rem; }
    .big-status { font-size: 1.4rem; font-weight: 700; margin-bottom: 0.25rem; }
    .big-status-sub { color: #555; font-size: 1rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

if not SYNTHETIC_PATH.exists():
    st.error(
        "No demo incidents found yet.\n\n"
        "Run this first, then reload this page:\n\n"
        "`python synthetic_data/generate_incidents.py`"
    )
    st.stop()

fixtures = json.loads(SYNTHETIC_PATH.read_text())
ARCHETYPE_LABELS = {
    "deployment_regression": "🚀 Bad deploy",
    "infra_external": "🗄️ Infra issue (not code)",
    "config_drift": "⚙️ Config change",
    "flaky_unrelated": "🎲 Flaky test (probably not real)",
    "true_app_bug": "🐛 Real application bug",
    "ambiguous": "❓ Unclear / mixed signals",
}
options = {
    f"{ARCHETYPE_LABELS.get(f['ground_truth_category'], f['ground_truth_category'])} "
    f"— {f['service']} ({f['incident_id']})": f
    for f in fixtures
}


def _matches_search(label: str, fixture: dict, query: str) -> bool:
    query = query.lower().strip()
    if not query:
        return True
    haystack = " ".join([
        label, fixture["incident_id"], fixture["service"], fixture["ground_truth_category"],
    ]).lower()
    return query in haystack


# ---------------------------------------------------------------------------
# Sidebar: view mode, incident picker, settings — shared by both views
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("View")
    view_mode = st.radio(
        "Who's this for?",
        ["🙋 Simple (plain English)", "🛠️ Technical (detailed trace)"],
        help="Simple: one clear summary, no jargon. Technical: full pipeline trace, confidence numbers, raw evidence.",
    )
    is_simple = view_mode.startswith("🙋")

    st.divider()
    st.header("1. Choose an incident")

    source_mode = st.radio(
        "Where's the incident coming from?",
        ["🧪 Demo incident (synthetic)", "🔗 Real Jira ticket"],
        help="Demo: pick from pre-built sample incidents. Real: fetch an actual ticket from your Jira instance.",
    )
    is_real_jira = source_mode.startswith("🔗")

    fixture = None
    real_jira_key = None
    real_service = None
    real_gh_owner = None
    real_gh_repo = None

    if is_real_jira:
        import src.config as _cfg
        if not _cfg.has_jira_credentials():
            st.warning(
                "No Jira credentials found. Set JIRA_URL, JIRA_EMAIL, and "
                "JIRA_API_TOKEN in your `.env` file, then reload this page."
            )
        real_jira_key = st.text_input(
            "Jira issue key", placeholder="e.g. SCRUM-6, OPS-42",
            help="The ticket key from your Jira instance.",
        )
        real_service = st.text_input(
            "Service name (optional)", placeholder="e.g. checkout-api",
            help="Needed only to pull Grafana/Splunk data for this service, if you have those configured.",
        )
        col_a, col_b = st.columns(2)
        real_gh_owner = col_a.text_input("GitHub owner (optional)", placeholder="acme")
        real_gh_repo = col_b.text_input("GitHub repo (optional)", placeholder="checkout")
        if not _cfg.has_github_credentials() and (real_gh_owner or real_gh_repo):
            st.caption("⚠️ GITHUB_TOKEN not set — commit correlation will be skipped even if you fill these in.")
    else:
        search_query = st.text_input(
            "🔍 Search incidents",
            placeholder="e.g. checkout, deploy, INC-aba3f7, flaky…",
            help="Filters the list below by incident ID, service name, or category.",
        )
        filtered_options = {label: f for label, f in options.items() if _matches_search(label, f, search_query)}
        if not filtered_options:
            st.warning(f"No incidents match “{search_query}”. Showing all instead.")
            filtered_options = options
        st.caption(f"{len(filtered_options)} of {len(options)} incidents shown")
        choice = st.selectbox("Which incident do you want to investigate?", list(filtered_options.keys()))
        fixture = filtered_options[choice]

    st.header("2. Settings")
    use_real_llm = st.toggle(
        "Use real Gemini AI", value=False,
        help="Off = free offline demo mode (no API key needed). On = calls real Gemini, needs GEMINI_API_KEY set.",
    )
    human_decision = st.radio(
        "If this needs sign-off, what should the reviewer decide?",
        ["🛑 Reject (safe default)", "✅ Approve"],
        help="Risky recommendations always need a person to approve them first. This simulates that answer.",
    )

    st.divider()
    run_disabled = is_real_jira and not real_jira_key
    run_button = st.button(
        "▶️  Analyze this incident", type="primary", use_container_width=True,
        disabled=run_disabled,
    )
    if run_disabled:
        st.caption("Enter a Jira issue key above to enable this.")


# ---------------------------------------------------------------------------
# Plain-language helpers (used only in Simple view)
# ---------------------------------------------------------------------------
SEVERITY_PLAIN = {
    "sev1": ("🔴", "Critical outage"),
    "sev2": ("🟠", "Major issue"),
    "sev3": ("🟡", "Minor issue"),
    "sev4": ("🟢", "Informational, low impact"),
}


def _confidence_words(confidence: float) -> str:
    if confidence >= 0.75:
        return "We're quite confident in this explanation."
    if confidence >= 0.55:
        return "We're fairly confident, but not certain — worth a second look."
    return "We're not very confident yet — treat this as a starting point, not an answer."


def _risk_words(score: int) -> str:
    if score >= 70:
        return "This is high-risk and needs attention soon."
    if score >= 40:
        return "This is moderate risk."
    return "This is low risk."


def _source_type_plain(source_type: str) -> str:
    return {
        "log": "error logs",
        "code_diff": "recent code changes",
        "test": "test results",
        "metric": "system metrics",
        "past_incident": "a similar past incident",
        "runbook": "our runbook documentation",
    }.get(source_type, source_type)


# ---------------------------------------------------------------------------
# Technical-view helpers
# ---------------------------------------------------------------------------
def _find(log: list[str], prefix: str) -> list[str]:
    return [line for line in log if line.startswith(prefix)]


def _humanize_log_line(line: str) -> str:
    text = re.sub(r"^[A-Z_]+(\[[a-z_]+\])?:\s*", "", line)
    return text[0].upper() + text[1:] if text else line


def render_pipeline(log: list[str]) -> None:
    steps = [
        ("1️⃣ Triage", "Figures out how severe this is and which services are affected.", "TRIAGE"),
        ("2️⃣ Similar past incidents", "Looks for incidents we've seen before that look like this one.", "RETRIEVAL"),
        ("3️⃣ Specialist analysis", "Separate experts each look at logs, code changes, tests, and metrics.", "SPECIALIST"),
        ("4️⃣ Root cause hypothesis", "Combines the specialists' findings into one explanation.", "SYNTHESIS"),
        ("5️⃣ Confidence check", "Makes sure the explanation is actually backed by real evidence before trusting it.", "GATE"),
        ("6️⃣ Risk & rollback", "Scores how risky this is and whether rolling back the last change makes sense.", "RISK"),
        ("7️⃣ Human sign-off", "Anything risky (rollback, or a major incident) needs a person to approve it.", "APPROVAL"),
        ("8️⃣ Final report", "Publishes the report, or holds it for review if it wasn't approved.", "WRITEBACK"),
    ]
    for title, description, prefix in steps:
        lines = _find(log, prefix)
        with st.container(border=True):
            st.markdown(f'<div class="step-title">{title}</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="step-desc">{description}</div>', unsafe_allow_html=True)
            if not lines:
                st.caption("Not reached (an earlier step stopped the pipeline).")
                continue
            for line in lines:
                plain = _humanize_log_line(line)
                lowered = line.lower()
                if "blocked" in lowered or "failed" in lowered or "rejected" in lowered:
                    st.warning(plain)
                elif "published" in lowered or "passed" in lowered or "approved" in lowered:
                    st.success(plain)
                else:
                    st.text(plain)


# ---------------------------------------------------------------------------
# Main content
# ---------------------------------------------------------------------------
st.title("🔎 Incident Root-Cause Copilot")

tab_analyze, tab_queue = st.tabs(["🔍 Analyze Incident", "🗂️ Review Queue"])

with tab_analyze:
    if is_simple:
        st.caption("Plain-English summary — no technical detail. Switch to **Technical** in the sidebar for the full trace.")
    else:
        st.caption("Full technical trace — pipeline steps, confidence scores, and raw evidence citations.")

    if not is_simple and fixture is not None:
        st.subheader("📋 What we know so far")
        st.caption("This is the raw evidence ingested for this incident, before any analysis.")

        def _short(payload: dict, limit: int = 100) -> str:
            text = str(payload)
            return text[:limit] + ("…" if len(text) > limit else "")

        evidence_rows = [
            {"Source": e["source"], "Service": e.get("service", "—"), "What happened": _short(e["payload"])}
            for e in fixture["events"]
        ]
        st.table(evidence_rows)

    if run_button:
        llm = select_llm_client() if use_real_llm else MockLLMClient()
        incident_id = None
        events = None
        ingest_log: list[str] = []

        if is_real_jira:
            import asyncio
            from src.ingestion.collector import collect_incident_events

            incident_id = real_jira_key.strip()
            with st.spinner("Fetching real evidence from Jira/GitHub…"):
                events, ingest_log = asyncio.run(
                    collect_incident_events(
                        incident_id,
                        github_owner=real_gh_owner or None,
                        github_repo=real_gh_repo or None,
                        service=real_service or None,
                    )
                )

            if not events:
                st.error(
                    f"Couldn't fetch any evidence for **{incident_id}**. Check the issue key, "
                    "your JIRA_URL/JIRA_EMAIL/JIRA_API_TOKEN in `.env`, and that the ticket exists."
                )
                for line in ingest_log:
                    st.caption(line)
                st.stop()

            if not is_simple:
                st.subheader("📋 What we fetched from Jira/GitHub")
                evidence_rows = [
                    {"Source": e.source, "Service": e.service or "—", "What happened": str(e.payload)[:150]}
                    for e in events
                ]
                st.table(evidence_rows)
        else:
            incident_id = fixture["incident_id"]
            events = [IncidentEvent(**e) for e in fixture["events"]]

        store = build_store_from_synthetic(SYNTHETIC_PATH, RUNBOOKS_DIR)
        cost_meter = CostMeter(incident_id=incident_id)

        def dashboard_approver(_report):
            approved = human_decision.startswith("✅")
            return approved, "dashboard reviewer" if approved else "dashboard reviewer (rejected)"

        graph = build_graph(llm, store, cost_meter, approver=dashboard_approver)

        with st.spinner("Looking into it…" if is_simple else "Running the pipeline…"):
            final_state = graph.invoke({
                "incident_id": incident_id,
                "events": events,
                "decision_log": list(ingest_log),
            })

        report = final_state.get("report")
        st.divider()

        # =======================================================================
        # SIMPLE VIEW
        # =======================================================================
        if is_simple:
            if report is None:
                st.markdown('<div class="big-status">🟡 Not enough information yet</div>', unsafe_allow_html=True)
                st.markdown(
                    '<div class="big-status-sub">We looked into this, but the evidence was too '
                    "unclear to give you a confident answer. A person needs to take a closer look.</div>",
                    unsafe_allow_html=True,
                )
            else:
                emoji, severity_label = SEVERITY_PLAIN.get(report.triage.severity.value, ("⚪", "Unknown"))
                st.markdown(f'<div class="big-status">{emoji} {severity_label}</div>', unsafe_allow_html=True)
                st.markdown(
                    f'<div class="big-status-sub">Affects: {", ".join(report.triage.affected_services)}</div>',
                    unsafe_allow_html=True,
                )

                st.markdown("### What happened")
                st.info(report.hypothesis.statement)
                st.caption(_confidence_words(report.hypothesis.confidence))

                st.markdown("### Do we need to act?")
                needs_action = report.rollback.recommended or report.triage.severity.value in ("sev1", "sev2")
                if not needs_action:
                    st.success("🟢 **No action needed right now.** This looks safe to leave as-is.")
                elif report.human_approved:
                    st.error(
                        "🔴 **Yes — action was approved and is being taken.** "
                        f"{report.rollback.justification if report.rollback.recommended else ''}"
                    )
                else:
                    st.warning(
                        "🟠 **Yes, this needs a decision** — it's waiting for someone to review and approve it "
                        "before anything happens. Nothing has been changed yet."
                    )
                st.caption(_risk_words(report.risk.score))

                with st.expander("ℹ️ How did we figure this out?"):
                    source_types = sorted({
                        _source_type_plain(c.source_type)
                        for f in report.hypothesis.supporting_findings
                        for c in f.citations
                    })
                    if source_types:
                        st.write("We looked at: " + ", ".join(source_types) + ".")
                    st.write(
                        "This was checked automatically, and "
                        + ("a person has reviewed and approved it."
                           if report.human_approved
                           else "still needs a person to review it before anything happens.")
                    )

        # =======================================================================
        # TECHNICAL VIEW
        # =======================================================================
        else:
            st.subheader("🧭 Pipeline walkthrough")
            render_pipeline(final_state.get("decision_log", []))

            st.divider()
            st.subheader("📄 Result")

            if report is None:
                st.info(
                    "**No report was produced.** There wasn't enough solid evidence to form a "
                    "confident explanation, so this was routed for manual review instead of guessing."
                )
            else:
                col1, col2, col3 = st.columns(3)
                col1.metric("Severity", report.triage.severity.value.upper())
                col2.metric("Risk score", f"{report.risk.score} / 100")
                col3.metric("Confidence", f"{report.hypothesis.confidence:.0%}")

                st.markdown("#### What likely happened")
                st.info(report.hypothesis.statement)

                with st.expander("🔬 See the evidence behind this explanation"):
                    for finding in report.hypothesis.supporting_findings:
                        st.markdown(f"**{finding.agent.replace('_', ' ').title()}** (confidence {finding.confidence:.0%})")
                        st.write(finding.summary)
                        for c in finding.citations:
                            st.caption(f"↳ [{c.source_type}] `{c.reference}`: {c.excerpt}")
                        st.markdown("---")

                st.markdown("#### Recommended action")
                if report.rollback.recommended:
                    st.error(f"🔴 **Rollback recommended.** {report.rollback.justification}")
                else:
                    st.success(f"🟢 **No rollback needed.** {report.rollback.justification}")

                st.markdown("#### Was this approved?")
                if report.human_approved:
                    st.success(f"✅ Approved by {report.approver}. This report was published.")
                else:
                    st.warning(f"🛑 Not approved (reviewer: {report.approver}). Held for review, not published.")

            st.divider()
            st.caption(f"💰 Cost of this run: ${cost_meter.cost_usd():.6f} over {cost_meter.iterations} AI call(s).")

    else:
        st.info("👈 Pick an incident and click **Analyze this incident** in the sidebar to begin.")

with tab_queue:
    from src.review.queue import approve, list_blocked, reject

    st.caption(
        "Everything analyzed but not yet approved — from this dashboard, the CLI, or "
        "a webhook-triggered run. Nothing here has been published or acted on yet."
    )

    entries = list_blocked()

    if not entries:
        st.success("✅ Nothing waiting on review right now.")
    else:
        st.warning(f"⏳ {len(entries)} report(s) waiting on a decision.")

        for entry in entries:
            r = entry.report
            emoji, severity_label = SEVERITY_PLAIN.get(r.triage.severity.value, ("⚪", r.triage.severity.value.upper()))
            with st.container(border=True):
                col_info, col_actions = st.columns([3, 1])

                with col_info:
                    st.markdown(f"**{emoji} {entry.incident_id}** — {severity_label}")
                    st.write(r.hypothesis.statement)
                    st.caption(
                        f"Risk: {r.risk.score}/100 · Confidence: {r.hypothesis.confidence:.0%} · "
                        f"Rollback recommended: {'Yes' if r.rollback.recommended else 'No'} · "
                        f"Generated: {r.generated_at.strftime('%Y-%m-%d %H:%M UTC')}"
                    )
                    with st.expander("See evidence"):
                        for finding in r.hypothesis.supporting_findings:
                            st.write(f"**{finding.agent.replace('_', ' ').title()}**: {finding.summary}")
                            for c in finding.citations:
                                st.caption(f"↳ [{c.source_type}] `{c.reference}`: {c.excerpt}")

                with col_actions:
                    reviewer_name = st.text_input(
                        "Your name", key=f"reviewer_{entry.incident_id}", placeholder="e.g. alice",
                    )
                    approve_clicked = st.button(
                        "✅ Approve", key=f"approve_{entry.incident_id}", use_container_width=True,
                        disabled=not reviewer_name,
                    )
                    reject_clicked = st.button(
                        "🛑 Reject", key=f"reject_{entry.incident_id}", use_container_width=True,
                        disabled=not reviewer_name,
                    )

                    if approve_clicked:
                        approve(entry.incident_id, reviewer_name)
                        st.success(f"Approved and published {entry.incident_id}.")
                        st.rerun()

                    if reject_clicked:
                        reject(entry.incident_id, reviewer_name)
                        st.info(f"Rejected {entry.incident_id}.")
                        st.rerun()
