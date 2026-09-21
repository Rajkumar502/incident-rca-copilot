"""
Single common place that loads .env and reads configuration for the whole
project. Every module that needs an environment variable — Jira/GitHub/
Grafana/Splunk credentials, GEMINI_API_KEY, cost ceilings — should import
from here rather than calling `os.environ.get(...)` directly, so there is
exactly one place that knows how configuration is loaded and one place to
change if that ever needs to (e.g. swapping in a secrets manager later).

Loaded automatically: importing anything from `src` runs src/__init__.py,
which imports this module, which calls load_dotenv() immediately — so a
.env file at the repo root is picked up before any other code in the
process reads an environment variable, with no explicit `export` needed
and no per-entrypoint boilerplate.

Precedence: real environment variables (already `export`ed, or set by a
process manager / CI / container orchestrator) always win over .env —
override=False means python-dotenv will not clobber a variable that's
already set. A leftover .env file should never silently override a real
deployment's configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(dotenv_path=ENV_PATH, override=False)


def get(key: str, default: str | None = None) -> str | None:
    """Read a config value. Thin wrapper over os.environ.get() so call
    sites read `config.get("JIRA_URL")` instead of `os.environ.get(...)`
    — same behavior, one obvious place to look when tracing where a
    setting comes from."""
    return os.environ.get(key, default)


def require(key: str) -> str:
    """Read a config value that must be set, or raise a clear error naming
    exactly which variable is missing — used by client constructors
    (JiraConfigError, GitHubConfigError, etc.) instead of each one
    re-implementing its own "is this set" check."""
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"{key} is not set. Set it in your environment or in a .env "
            f"file at {ENV_PATH} (see .env.example)."
        )
    return value


def has_jira_credentials() -> bool:
    return all(get(v) for v in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"))


def has_github_credentials() -> bool:
    return get("GITHUB_TOKEN") is not None


def has_grafana_credentials() -> bool:
    return all(get(v) for v in ("GRAFANA_URL", "GRAFANA_API_KEY"))


def has_splunk_credentials() -> bool:
    return all(get(v) for v in ("SPLUNK_URL", "SPLUNK_TOKEN"))


def has_gemini_credentials() -> bool:
    return get("GEMINI_API_KEY") is not None


# Non-secret settings with sane defaults, read once here rather than
# scattered as magic literals across governance/cost_meter.py callers.
COST_CEILING_USD = float(get("COST_CEILING_USD", "0.25"))
VECTOR_STORE_BACKEND = get("VECTOR_STORE", "tfidf")  # "tfidf" | "chroma"

# Automatic-trigger settings (src/webhook/server.py)
WEBHOOK_SECRET = get("WEBHOOK_SECRET")  # shared secret the caller must send back
WEBHOOK_TRIGGER_LABEL = get("WEBHOOK_TRIGGER_LABEL", "incident")  # Jira label that opts an issue in
WEBHOOK_DEFAULT_SERVICE_FIELD = get("WEBHOOK_SERVICE_FIELD")  # optional Jira custom field id, e.g. "customfield_10050"
WEBHOOK_GITHUB_OWNER = get("WEBHOOK_GITHUB_OWNER")
WEBHOOK_GITHUB_REPO = get("WEBHOOK_GITHUB_REPO")

# Retry/backoff for transient tool failures (src/governance/retry.py,
# used by src/ingestion/collector.py). Kept small by default — an
# ingestion run already has an overall cost/iteration ceiling
# (CostMeter), and retries add latency on top of that, so this defaults
# to a modest 3 attempts rather than an aggressive retry policy.
RETRY_MAX_ATTEMPTS = int(get("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY_SECONDS = float(get("RETRY_BASE_DELAY_SECONDS", "0.5"))
