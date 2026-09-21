# Runbook: Database Connection Pool Exhaustion
Symptom: db_connections_active approaches pool_max, requests queue and time out.
Root cause pattern: usually a long-running query or transaction from a batch
or analytics job holding connections, not an application code regression.
Fix: kill the offending long-running transaction, consider a dedicated pool
for batch workloads. Rollback of the primary service is usually NOT the fix.
