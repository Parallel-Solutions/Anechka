"""Curated Tomoru chat starters for the intelligent export dialog list."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOMORU_TRIGGER = re.compile(
    r"(tomoru|туморо|тумороу|обзвон|для\s+tomoru|номера\s+для)",
    re.I,
)


@dataclass(frozen=True)
class TomoruChatPrompt:
    id: str
    category: str
    title: str
    prompt: str
    purpose: str
    preview_checks: tuple[str, ...]
    step: int | None = None
    scenario_id: str | None = None
    follow_up_prompt: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "prompt": self.prompt,
            "purpose": self.purpose,
            "preview_checks": list(self.preview_checks),
            "step": self.step,
            "scenario_id": self.scenario_id,
            "follow_up_prompt": self.follow_up_prompt,
        }

    @property
    def tooltip(self) -> str:
        checks = "; ".join(self.preview_checks)
        return f"{self.purpose} Проверить в preview: {checks}."


_PROMPTS: tuple[TomoruChatPrompt, ...] = (
    # 1) Базовые
    TomoruChatPrompt(
        id="basic_all",
        category="basic",
        title="Номера для Tomoru — все сделки КП",
        prompt="Выгрузи номера для обзвона в Tomoru по активным сделкам коммерческого предложения",
        purpose="Полный список телефонов по всей воронке КП без дополнительных фильтров.",
        preview_checks=(
            "кол-во строк > 0",
            "только колонка «Телефон» в формате 7XXXXXXXXXX",
            "нет повторяющихся номеров",
        ),
    ),
    TomoruChatPrompt(
        id="basic_open",
        category="basic",
        title="Tomoru — открытые сделки КП",
        prompt="Подготовь список телефонов для тумороу — все открытые сделки КП",
        purpose="Быстрая стандартная выгрузка, когда нужны все неархивные сделки воронки.",
        preview_checks=(
            "архивные сделки не попали",
            "телефоны 11 цифр, начинаются с 7",
        ),
    ),
    TomoruChatPrompt(
        id="basic_no_archive",
        category="basic",
        title="Tomoru — без архива",
        prompt="Нужна выгрузка для Tomoru: номера по сделкам без архива",
        purpose="Минимальный запрос — система сама настроит воронку, контакт и формат.",
        preview_checks=(
            "один лист «Номера», без листа отчёта",
            "дублей телефонов между сделками нет",
        ),
    ),
    # 2) По регионам
    TomoruChatPrompt(
        id="region_tomsk",
        category="region",
        title="Tomoru обзвон — Томская область",
        prompt="Tomoru обзвон по Томской области",
        purpose="Региональный обзвон по сделкам с регионом «Томская область».",
        preview_checks=(
            "строки только по Томской области",
            "формат телефона 7XXXXXXXXXX",
        ),
    ),
    TomoruChatPrompt(
        id="region_moscow",
        category="region",
        title="Tomoru — Москва",
        prompt="Номера для тумороу по Москве",
        purpose="Обзвон московских сделок воронки КП.",
        preview_checks=(
            "кол-во строк соответствует открытым сделкам Москвы",
            "нет дублей телефонов",
        ),
    ),
    TomoruChatPrompt(
        id="region_spb",
        category="region",
        title="Tomoru — Санкт-Петербург",
        prompt="Выгрузка для Tomoru — Санкт-Петербург",
        purpose="Список номеров по питерским сделкам для загрузки в Tomoru.",
        preview_checks=(
            "регион = Санкт-Петербург",
            "телефоны только цифры, 11 символов",
        ),
    ),
    TomoruChatPrompt(
        id="region_tver",
        category="region",
        title="Tomoru — Тверская область",
        prompt="Обзвон Tomoru по Тверской области",
        purpose="Региональная кампания по Тверской области.",
        preview_checks=(
            "строки > 0 (если в CRM есть открытые сделки)",
            "формат и дедупликация телефонов",
        ),
    ),
    TomoruChatPrompt(
        id="region_amur",
        category="region",
        title="Tomoru — Амурская область",
        prompt="Номера для обзвона в Tomoru по Амурской области",
        purpose="Обзвон по дальневосточному региону.",
        preview_checks=(
            "фильтр по Амурской области в плане",
            "одна колонка «Телефон»",
        ),
    ),
    # 3) Регион + стадия
    TomoruChatPrompt(
        id="stage_moscow_new",
        category="region_stage",
        title="Tomoru — Москва, стадия Новая",
        prompt="Tomoru по Москве, стадия Новая",
        purpose="Обзвон только новых московских сделок.",
        preview_checks=(
            "стадия «Новая» в фильтрах плана",
            "кол-во строк ≤ всех московских сделок",
        ),
    ),
    TomoruChatPrompt(
        id="stage_tomsk_warm",
        category="region_stage",
        title="Tomoru — Томск, стадия Тёплый",
        prompt="Обзвон Tomoru — Томская область, стадия Тёплый",
        purpose="Прогрев «тёплых» сделок региона.",
        preview_checks=(
            "фильтр STAGE_ID по «Тёплый» (или C15:4)",
            "телефоны без дублей",
        ),
    ),
    TomoruChatPrompt(
        id="stage_spb_code",
        category="region_stage",
        title="Tomoru — СПб, стадия C15:NEW",
        prompt="Выгрузка для тумороу по Санкт-Петербургу на стадии C15:NEW",
        purpose="Точный отбор по коду стадии, когда название может путаться.",
        preview_checks=(
            'в JSON-плане STAGE_ID eq "C15:NEW"',
            "формат телефона корректный",
        ),
    ),
    # 4) Регион + период
    TomoruChatPrompt(
        id="period_moscow_month",
        category="region_period",
        title="Tomoru — Москва, текущий месяц",
        prompt="Tomoru обзвон по Москве за текущий месяц",
        purpose="Свежие московские сделки, созданные с начала месяца.",
        preview_checks=(
            "фильтр DATE_CREATE >= @month_start",
            "строк меньше, чем без периода",
        ),
    ),
    TomoruChatPrompt(
        id="period_tomsk_30d",
        category="region_period",
        title="Tomoru — Томск, 30 дней",
        prompt="Номера для Tomoru по Томской области за последние 30 дней",
        purpose="Недавние сделки региона для оперативного обзвона.",
        preview_checks=(
            "период «30 дней» в плане",
            "телефоны 7XXXXXXXXXX",
        ),
    ),
    TomoruChatPrompt(
        id="period_tver_year",
        category="region_period",
        title="Tomoru — Тверь, с начала года",
        prompt="Выгрузка для обзвона Tomoru по Тверской области с начала года",
        purpose="Годовая выборка по региону.",
        preview_checks=(
            "фильтр от @year_start",
            "кол-во строк и регион соответствуют ожиданию",
        ),
    ),
    # 5) Двухшаговые (только шаг 1 в списке)
    TomoruChatPrompt(
        id="twostep_region",
        category="two_step",
        title="Tomoru — уточнить регион (шаг 1)",
        prompt="Подготовь выгрузку для Tomoru",
        purpose="Сначала базовый Tomoru-план, потом сужение по региону.",
        preview_checks=(
            "после шага 2 — фильтр региона",
            "только номера выбранного региона",
        ),
        step=1,
        scenario_id="region_refine",
        follow_up_prompt="Только Томская область",
    ),
    TomoruChatPrompt(
        id="twostep_limit",
        category="two_step",
        title="Tomoru — ограничить выборку (шаг 1)",
        prompt="Нужны номера для обзвона в тумороу",
        purpose="Пилотный обзвон на малой выборке.",
        preview_checks=(
            "limit ≤ 200",
            "сортировка по дате создания desc",
        ),
        step=1,
        scenario_id="limit_sort",
        follow_up_prompt="Сначала новые сделки, не больше 200",
    ),
    TomoruChatPrompt(
        id="twostep_moscow_refine",
        category="two_step",
        title="Tomoru — Москва (шаг 1)",
        prompt="Выгрузка для Tomoru по Москве",
        purpose="Пошаговое уточнение без перегруженного первого сообщения.",
        preview_checks=(
            "после шага 2 — Москва + период + стадия «Новая»",
            "фильтры в плане соответствуют уточнению",
        ),
        step=1,
        scenario_id="moscow_refine",
        follow_up_prompt="Только сделки за последний месяц, стадия Новая",
    ),
    # 6) Ограниченные выборки
    TomoruChatPrompt(
        id="limit_test_50",
        category="limited",
        title="Tomoru тест — 50 номеров",
        prompt="Тестовый обзвон Tomoru — не больше 50 номеров",
        purpose="Пробная загрузка в Tomoru перед полной кампанией.",
        preview_checks=(
            "≤ 50 строк",
            "формат телефона; нет дублей",
        ),
    ),
    TomoruChatPrompt(
        id="limit_test_moscow_100",
        category="limited",
        title="Tomoru тест — Москва, 100 строк",
        prompt="Номера для тумороу по Москве, лимит 100 строк для проверки",
        purpose="Проверка качества номеров по одному региону на малой выборке.",
        preview_checks=(
            "≤ 100 строк",
            "регион Москва; одна колонка «Телефон»",
        ),
    ),
)

_BY_ID: dict[str, TomoruChatPrompt] = {p.id: p for p in _PROMPTS}


def list_tomoru_prompts() -> list[dict]:
    """Return starters shown at the top of the «Диалоги» list."""
    return [p.to_dict() for p in _PROMPTS]


def get_tomoru_prompt(prompt_id: str) -> TomoruChatPrompt | None:
    return _BY_ID.get(prompt_id)


def list_dialog_starters() -> list[TomoruChatPrompt]:
    """Prompts visible in the dialog list (step-1 only for two-step scenarios)."""
    return list(_PROMPTS)


def has_tomoru_trigger(text: str) -> bool:
    return bool(_TOMORU_TRIGGER.search(text or ""))
