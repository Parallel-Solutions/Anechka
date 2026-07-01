"""Shared logic for enqueueing Bitrix CRM import runs."""

from __future__ import annotations

from app.models import ENTITY_LEAD, SyncRun
from app.repositories.sync_repository import SyncRepository


class ConcurrentImportError(Exception):
    """An import is already pending or running for this portal."""


class FullImportNotConfirmedError(Exception):
    """Full import requires explicit confirmation."""


def resolve_import_mode(sync_repo: SyncRepository, portal_id: str, mode: str) -> str:
    if mode != "incremental":
        return mode
    cp = sync_repo.get_checkpoint(portal_id, "entities", ENTITY_LEAD)
    if cp is None or cp.cursor_time is None:
        return "full"
    return mode


def enqueue_import(
    sync_repo: SyncRepository,
    portal_id: str,
    *,
    mode: str = "incremental",
    requested_by: str | None = None,
    analyze_metadata: bool = True,
    confirm_full: bool = False,
) -> SyncRun:
    if mode == "full" and not confirm_full:
        raise FullImportNotConfirmedError()

    if sync_repo.has_active_run(portal_id):
        raise ConcurrentImportError()

    resolved_mode = resolve_import_mode(sync_repo, portal_id, mode)
    return sync_repo.create_run(
        portal_id,
        resolved_mode,
        requested_by=requested_by,
        statistics={"analyze_metadata": analyze_metadata},
    )
