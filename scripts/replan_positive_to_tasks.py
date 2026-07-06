"""Replan positive call-result rows: CRM todo -> tasks.task.add.

Run: docker compose exec web python scripts/replan_positive_to_tasks.py --import-id 6
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.database import SessionLocal
from app.services.auth_service import resolve_portal_id
from app.dependencies import get_call_result_classifier_instance
from app.services.call_results.orchestrator import CallResultOrchestrator
from app.services.call_results.replan_service import replan_positive_to_tasks


def main() -> int:
    parser = argparse.ArgumentParser(description="Replan positive rows to tasks.task.add")
    parser.add_argument("--import-id", type=int, required=True, help="Call result import ID")
    parser.add_argument("--json", action="store_true", help="Print report as JSON")
    args = parser.parse_args()

    settings = get_settings()
    portal_id = resolve_portal_id(settings)
    db = SessionLocal()
    try:
        classifier = get_call_result_classifier_instance(settings)
        orch = CallResultOrchestrator(db, settings, portal_id, classifier)
        report = replan_positive_to_tasks(orch, args.import_id)
    finally:
        db.close()

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"Import #{report.import_id}")
        print(f"  Positive rows found:     {report.found}")
        print(f"  Replanned (new task):    {report.replanned}")
        print(f"  Skipped:                 {report.skipped}")
        print(f"  Already had task:        {report.already_had_task}")
        print(f"  Pending todos disabled:  {report.todos_disabled}")
        if report.errors:
            print("  Errors:")
            for err in report.errors:
                print(f"    - {err}")

    return 1 if report.errors and report.replanned == 0 and report.found == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
