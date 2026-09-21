# Runbook: Sync vs Async Retry Blocking Event Loop
Symptom: p99 latency spike shortly after a deploy touching retry logic.
Root cause pattern: a retry path was changed from async to a synchronous
call, blocking the event loop under load.
Fix: revert to async retry, or move blocking work to a thread pool.
Rollback is typically safe and fast for this pattern.
