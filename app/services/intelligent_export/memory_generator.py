"""Turn a read-only portal :class:`PortalProfile` into candidate planner memory.

The generator emits a *small*, prioritized set of ``IeMemoryEntry`` candidates
(``kind`` in instruction/alias) that capture facts the field catalog does not
already carry: how phones/e-mails are stored, how often relations are populated,
which fields are too sparse to default, and value conventions.

Governance is preserved end-to-end: every candidate is stored with
``status="proposed"`` and ``source="import"`` and only reaches the planner once a
human approves it. Idempotency is achieved with a content hash kept inside
``value_json["profile_hash"]`` (the model has no hash column): re-running never
duplicates rows, only bumps the version of still-``proposed`` entries, and never
overrides a human decision (approved/rejected/archived/deprecated).

No sensitive values (raw phone/e-mail) are ever embedded — only aggregates.

On UF_* aliases: the catalog already exposes ``display_name`` to the planner, so
we do not duplicate opaque codes. We emit a low-priority alias only when a UF
field has an *informative* human name, because an ``alias``-kind entry also
drives relevance routing in ``service._select_catalog`` (keeping the field in
scope under the catalog budget) — value the bare catalog descriptor cannot
guarantee. Aliases sort last and are dropped first by the output cap.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.services.export_plan.catalog import FieldCatalog
from app.services.intelligent_export.db_profiler import (
    ENTITY_TYPE_IDS,
    PortalProfile,
)

# Human labels for assistant-facing content.
_LABEL_NOM = {1: "лиды", 2: "сделки", 3: "контакты", 4: "компании"}
_LABEL_GEN = {1: "лидов", 2: "сделок", 3: "контактов", 4: "компаний"}
_FK_LABEL = {"contactId": "контактом", "companyId": "компанией"}

_PRIORITY_PHONE_VS_FM = 300
_PRIORITY_FK_LINK = 280
_PRIORITY_LOW_FILL = 150
_PRIORITY_PHONE_FORMAT = 120
_PRIORITY_UF_ALIAS = 100

_CONTENT_MAX_CHARS = 300
_MAX_LOW_FILL_PER_ENTITY = 5
_MIN_PHONE_FORMAT_SAMPLE = 10
_PHONE_FORMAT_DOMINANT = 0.6


@dataclass
class MemoryCandidate:
    kind: str
    key: str
    content: str
    priority: int
    value_json: dict = field(default_factory=dict)
    scope: str = "project"


def _pct(share: float) -> str:
    return f"{share * 100:.0f}"


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _CONTENT_MAX_CHARS else text[: _CONTENT_MAX_CHARS - 1].rstrip() + "…"


def _candidate_hash(kind: str, key: str, content: str, metrics: dict) -> str:
    payload = json.dumps(
        {"kind": kind, "key": key, "content": content, "metrics": metrics},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _make(kind: str, key: str, content: str, priority: int, metrics: dict) -> MemoryCandidate:
    content = _clip(content)
    value_json = {
        "metrics": metrics,
        "profile_hash": _candidate_hash(kind, key, content, metrics),
        "generated_by": "db_profiler",
    }
    return MemoryCandidate(kind=kind, key=key, content=content, priority=priority, value_json=value_json)


def build_candidates(
    profile: PortalProfile,
    catalog: FieldCatalog,
    *,
    low_fill_threshold: float = 0.10,
    link_threshold: float = 0.30,
    min_rows: int = 20,
    max_entries: int = 20,
) -> list[MemoryCandidate]:
    candidates: list[MemoryCandidate] = []

    for entity_type_id in ENTITY_TYPE_IDS:
        ent = profile.entities.get(entity_type_id)
        if ent is None or ent.total_seen == 0:
            continue
        nom = _LABEL_NOM.get(entity_type_id, str(entity_type_id))
        gen = _LABEL_GEN.get(entity_type_id, nom)
        enough_rows = ent.total_seen >= min_rows

        # 1) PHONE vs FM storage convention
        phone = ent.multifield.get("PHONE", {})
        fm = ent.multifield.get("FM", {})
        email = ent.multifield.get("EMAIL", {})
        if phone.get("present") and fm.get("present"):
            candidates.append(
                _make(
                    "instruction",
                    f"profile:{entity_type_id}:phone_vs_fm",
                    f"Телефоны {gen} хранятся в типизированном поле PHONE; контейнер FM "
                    f"содержит и e-mail (часто первым элементом). phone_normalize применяй "
                    f"только к полю PHONE, не к FM.",
                    _PRIORITY_PHONE_VS_FM,
                    {
                        "phone_fill_rate": round(phone.get("fill_rate", 0.0), 4),
                        "email_fill_rate": round(email.get("fill_rate", 0.0), 4),
                        "fm_fill_rate": round(fm.get("fill_rate", 0.0), 4),
                    },
                )
            )

        # 2) Sparse FK relations
        if enough_rows:
            for label, stats in ent.fk_link_shares.items():
                share = stats.get("share", 0.0)
                if share < link_threshold:
                    fk_label = _FK_LABEL.get(label, label)
                    candidates.append(
                        _make(
                            "instruction",
                            f"profile:{entity_type_id}:fk:{label}",
                            f"У большинства записей «{nom}» связь с {fk_label} отсутствует "
                            f"({label} заполнен лишь в {_pct(share)}%). Если нужны данные "
                            f"связанной сущности, добавляй фильтр на наличие связи "
                            f"({label} != 0), иначе строки уйдут в ошибки.",
                            _PRIORITY_FK_LINK,
                            {"share": round(share, 4), "linked": stats.get("linked", 0), "total": ent.total_seen},
                        )
                    )

        # 3) Sparse fields — don't include by default
        if enough_rows:
            sparse = sorted(
                ((code, rate) for code, rate in ent.fill_rates.items() if rate < low_fill_threshold),
                key=lambda t: (t[1], t[0]),
            )[:_MAX_LOW_FILL_PER_ENTITY]
            for code, rate in sparse:
                entry = catalog.get(entity_type_id, code)
                display = entry.display_name if entry else code
                name_part = f" ({display})" if display and display != code else ""
                candidates.append(
                    _make(
                        "instruction",
                        f"profile:{entity_type_id}:low_fill:{code}",
                        f"Поле {code}{name_part} заполнено лишь в {_pct(rate)}% записей «{nom}» — "
                        f"не включай его в колонки по умолчанию.",
                        _PRIORITY_LOW_FILL,
                        {"fill_rate": round(rate, 4), "code": code},
                    )
                )

        # 4) Phone value-format hint
        pf = ent.phone_format
        if pf and pf.get("sample", 0) >= _MIN_PHONE_FORMAT_SAMPLE:
            starts_8 = pf.get("starts_with_8", 0.0)
            if starts_8 >= _PHONE_FORMAT_DOMINANT:
                candidates.append(
                    _make(
                        "instruction",
                        f"profile:{entity_type_id}:phone_format",
                        f"Телефоны {gen} обычно начинаются с 8 ({_pct(starts_8)}% выборки) — "
                        f"учитывай это при нормализации и фильтрации номеров.",
                        _PRIORITY_PHONE_FORMAT,
                        {"starts_with_8": round(starts_8, 4), "sample": pf.get("sample", 0)},
                    )
                )

    # 5) UF_* aliases (only when an informative human name exists)
    for entity_type_id, code, display, informative in profile.uf_fields:
        if not informative:
            continue
        candidates.append(
            _make(
                "alias",
                f"profile:uf_alias:{entity_type_id}:{code}",
                f"UF-поле {code} — это «{display}».",
                _PRIORITY_UF_ALIAS,
                {"code": code, "entity_type_id": entity_type_id},
            )
        )

    candidates.sort(key=lambda c: (-c.priority, c.key))
    return candidates[: max(0, max_entries)]


def upsert_candidates(repo, candidates: list[MemoryCandidate]) -> dict[str, list]:
    """Idempotently persist candidates as proposed/import project memory.

    - new natural key -> create (status=proposed, source=import)
    - same profile_hash -> skip (no churn)
    - changed hash but still proposed -> update (version auto-bumps)
    - any human-decided status -> skip (governance preserved)
    """
    created: list = []
    updated: list = []
    skipped: list = []
    for cand in candidates:
        existing = repo.find_memory(scope=cand.scope, kind=cand.kind, key=cand.key)
        if existing is None:
            entry = repo.create_memory(
                scope=cand.scope,
                kind=cand.kind,
                key=cand.key,
                content=cand.content,
                value_json=cand.value_json,
                status="proposed",
                source="import",
                priority=cand.priority,
            )
            created.append(entry)
            continue
        existing_hash = (existing.value_json or {}).get("profile_hash")
        if existing_hash == cand.value_json.get("profile_hash"):
            skipped.append(existing)
            continue
        if existing.status != "proposed":
            skipped.append(existing)
            continue
        entry = repo.update_memory(
            existing.id,
            content=cand.content,
            value_json=cand.value_json,
            priority=cand.priority,
        )
        updated.append(entry)
    return {"created": created, "updated": updated, "skipped": skipped}


def generate_memory(db, repo, portal_id: str, settings) -> dict[str, list]:
    """Profile the portal, build candidates and upsert them. Returns the upsert
    result dict with lists of ``IeMemoryEntry`` (created/updated/skipped)."""
    from app.services.intelligent_export.db_profiler import PortalProfiler

    catalog = FieldCatalog.load(db, portal_id)
    profiler = PortalProfiler(db, portal_id, catalog)
    profile = profiler.profile(sample_cap=settings.ie_profile_sample_cap)
    candidates = build_candidates(
        profile,
        catalog,
        low_fill_threshold=settings.ie_profile_low_fill_threshold,
        link_threshold=settings.ie_profile_link_threshold,
        min_rows=settings.ie_profile_min_rows,
        max_entries=settings.ie_memory_generate_max,
    )
    return upsert_candidates(repo, candidates)
