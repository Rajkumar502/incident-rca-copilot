# Runbook: Flaky Test Triage
Symptom: a single test fails once but passes on rerun, with no related code
or infra change nearby, and overall suite pass rate stays high.
Root cause pattern: test flakiness (timing, animation, network jitter), not
a real regression. Do not recommend rollback based on this signal alone.
