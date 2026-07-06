"""Replan positive call-result rows from CRM todo to Bitrix tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.models import CallResultImportRow
from app.services.call_results.action_planner import PlannedAction

if TYPE_CHECKING:
    from app.services.call_results.orchestrator import CallResultOrchestrator


@dataclass
class ReplanPositiveReport:
    import_id: int
    found: int = 0
    replanned: int = 0
    skipped: int = 0
    already_had_task: int = 0
    todos_disabled: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "import_id": self.import_id,
            "found": self.found,
            "replanned": self.replanned,
            "skipped": self.skipped,
            "already_had_task": self.already_had_task,
            "todos_disabled": self.todos_disabled,
            "errors": list(self.errors),
        }


def is_positive_row(row: CallResultImportRow) -> bool:
    if row.primary_outcome == "positive":
        return True
    signals = row.business_signals or {}
    return bool(signals.get("positive"))


def _row_actions(orch: CallResultOrchestrator, import_id: int, row_id: int):
    return [a for a in orch.repo.list_actions(import_id) if a.import_row_id == row_id]


def _has_succeeded_task(actions) -> bool:
    return any(
        a.method == "tasks.task.add" and a.execution_status == "succeeded"
        for a in actions
    )


def _has_task_action(actions) -> bool:
    return any(a.method == "tasks.task.add" for a in actions)


def replan_positive_to_tasks(orch: CallResultOrchestrator, import_id: int) -> ReplanPositiveReport:
    """Disable pending CRM todos and ensure tasks.task.add plans for positive rows."""
    imp = orch.repo.get_import(import_id)
    report = ReplanPositiveReport(import_id=import_id)
    if imp is None:
        report.errors.append(f"Import #{import_id} not found")
        return report

    if orch.matcher._deals_by_id is None:
        orch.matcher.build_indexes()

    for row in orch.repo.list_rows(import_id):
        if not is_positive_row(row):
            continue
        report.found += 1
        try:
            _replan_row(orch, imp, row, report)
        except Exception as exc:
            report.errors.append(f"row {row.id}: {exc}")

    orch.db.commit()
    return report


def _replan_row(orch: CallResultOrchestrator, imp, row: CallResultImportRow, report: ReplanPositiveReport) -> None:
    actions = _row_actions(orch, imp.id, row.id)

    if _has_succeeded_task(actions):
        report.already_had_task += 1
        _disable_pending_todos(actions, report)
        return

    if not row.matched_deal_id:
        report.skipped += 1
        report.errors.append(f"row {row.id}: no matched_deal_id — task not planned")
        return

    _disable_pending_todos(actions, report)

    if _has_task_action(actions):
        for action in actions:
            if action.method == "tasks.task.add" and action.execution_status != "succeeded":
                action.is_enabled = True
        report.skipped += 1
        return

    deal_id = row.matched_deal_id
    deal = orch.matcher.get_deal(deal_id)
    assigned = deal.assigned_by_id if deal else None
    planned = [
        PlannedAction(
            method="tasks.task.add",
            action_type="task",
            operation_type="bitrix_add_task",
            payload={},
            human_summary="Задача: положительный результат обзвона",
            sort_order=0,
        )
    ]
    orch._persist_planned_actions(
        imp,
        row,
        planned,
        deal_id=deal_id,
        assigned_by_id=assigned,
    )
    report.replanned += 1


def _disable_pending_todos(actions, report: ReplanPositiveReport) -> None:
    for action in actions:
        if action.method != "crm.activity.todo.add":
            continue
        if action.execution_status == "succeeded":
            continue
        if action.is_enabled:
            action.is_enabled = False
            report.todos_disabled += 1
