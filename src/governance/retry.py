"""
Generic retry-with-exponential-backoff for transient failures.

Deliberately generic (not Jira/HTTP-specific) so it can wrap any async
call — the MCP tool calls in src/ingestion/collector.py today, potentially
other transient operations later. Callers decide what's retryable via
`is_retryable`, since "should I retry this" depends entirely on what kind
of failure it was (network blip vs. a permanent 404 vs. bad credentials),
not on this module.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from src import config

logger = logging.getLogger("incident_rca_retry")

T = TypeVar("T")


@dataclass
class RetryOutcome:
    """What actually happened, for the caller to log — not just the
    final value, since "succeeded on attempt 3 after 2 transient 503s"
    is meaningfully different from "succeeded on attempt 1" for an
    operator reading the ingestion log."""
    value: object
    attempts: int
    succeeded: bool
    last_error: Exception | None = None


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    is_retryable: Callable[[Exception], bool],
    max_attempts: int | None = None,
    base_delay: float | None = None,
    max_delay: float = 8.0,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> RetryOutcome:
    """
    Calls `fn()` (a zero-arg async callable — callers use a closure or
    functools.partial), retrying with exponential backoff + jitter on
    exceptions `is_retryable` says are worth retrying. Stops immediately
    (no retry) on a non-retryable exception, since retrying a permanent
    failure (bad credentials, a 404 for a ticket that doesn't exist)
    only adds latency with zero chance of success.

    max_attempts/base_delay default to config.RETRY_MAX_ATTEMPTS /
    config.RETRY_BASE_DELAY_SECONDS if not passed explicitly, so callers
    don't need to know about config.py unless they want to override it.
    """
    max_attempts = max_attempts if max_attempts is not None else config.RETRY_MAX_ATTEMPTS
    base_delay = base_delay if base_delay is not None else config.RETRY_BASE_DELAY_SECONDS

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            value = await fn()
            return RetryOutcome(value=value, attempts=attempt, succeeded=True)
        except Exception as e:  # noqa: BLE001 — classification is the caller's job via is_retryable
            last_error = e
            if attempt >= max_attempts or not is_retryable(e):
                break
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay += random.uniform(0, delay * 0.25)  # jitter, avoids retry storms if many
            # calls fail at once (e.g. a shared dependency has an outage)
            if on_retry:
                on_retry(attempt, e, delay)
            else:
                logger.info("Retryable failure on attempt %d/%d: %s. Retrying in %.2fs.",
                            attempt, max_attempts, e, delay)
            await asyncio.sleep(delay)

    return RetryOutcome(value=None, attempts=attempt, succeeded=False, last_error=last_error)
