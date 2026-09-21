# Runbook: Feature Flag Default Change Breaks Flag-Off Path
Symptom: test failures or errors concentrated in the flag-disabled code path
right after a flags.yaml change.
Root cause pattern: default flag value changed unintentionally, exposing an
undertested code path.
Fix: revert the flag default. Rollback is typically safe.
