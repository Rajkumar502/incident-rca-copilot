"""CLI: python -m src.review.cli list | approve <ID> <approver> | reject <ID> <reviewer> [reason]"""

from __future__ import annotations

import sys

from src.review.queue import QueueEntryNotFound, approve, list_blocked, reject


def _print_entry(entry) -> None:
    r = entry.report
    print(f"  {entry.incident_id}  [{r.triage.severity.value}]  "
          f"risk={r.risk.score}  rollback={r.rollback.recommended}  "
          f"confidence={r.hypothesis.confidence:.2f}")
    print(f"    {r.hypothesis.statement[:120]}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage:\n"
              "  python -m src.review.cli list\n"
              "  python -m src.review.cli approve <INCIDENT_ID> <approver_name>\n"
              "  python -m src.review.cli reject <INCIDENT_ID> <reviewer_name> [reason]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "list":
        entries = list_blocked()
        if not entries:
            print("Nothing waiting on review.")
            return
        print(f"{len(entries)} report(s) waiting on review:\n")
        for entry in entries:
            _print_entry(entry)
        return

    if command == "approve":
        if len(sys.argv) < 4:
            print("Usage: python -m src.review.cli approve <INCIDENT_ID> <approver_name>")
            sys.exit(1)
        try:
            report = approve(sys.argv[2], sys.argv[3])
            print(f"Approved and published {report.incident_id}.")
        except QueueEntryNotFound as e:
            print(f"Error: {e}")
            sys.exit(1)
        return

    if command == "reject":
        if len(sys.argv) < 4:
            print("Usage: python -m src.review.cli reject <INCIDENT_ID> <reviewer_name> [reason]")
            sys.exit(1)
        reason = sys.argv[4] if len(sys.argv) > 4 else ""
        try:
            report = reject(sys.argv[2], sys.argv[3], reason)
            print(f"Rejected {report.incident_id}.")
        except QueueEntryNotFound as e:
            print(f"Error: {e}")
            sys.exit(1)
        return

    print(f"Unknown command: {command}")
    sys.exit(1)


if __name__ == "__main__":
    main()
