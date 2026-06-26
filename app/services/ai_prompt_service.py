"""CRUD for AI chat prompt templates."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AiPromptTemplate

TITLE_MAX_LEN = 120
PROMPT_MAX_LEN = 4000

DEFAULT_PROMPTS: list[tuple[str, str]] = [
    (
        "Сделки по стадии",
        "Покажи 20 сделок из воронки 15 на стадии NEW: ID, название, ответственный, сумма",
    ),
    (
        "Список воронок",
        "Покажи список всех воронок (категорий) сделок с ID и названием",
    ),
    (
        "Стадии воронки",
        "Покажи все стадии сделок для воронки с ID 15",
    ),
    (
        "Новые контакты",
        "Найди контакты, созданные за последний месяц: имя, телефон, email. Лимит 50",
    ),
    (
        "Компании по городу",
        "Покажи компании из Москвы — название и ИНН. Лимит 50",
    ),
    (
        "Активные пользователи",
        "Сколько активных пользователей в Bitrix24? Покажи список с ID и именем",
    ),
    (
        "Поля сделки",
        "Покажи поля сущности «сделка»: код и название поля",
    ),
    (
        "Выгрузка по региону",
        "Запусти выгрузку сделок по региону Москва в воронке 15",
    ),
    (
        "Выгрузка по стадии",
        "Запусти выгрузку сделок по стадии NEW в воронке 15, лимит 100",
    ),
    (
        "Полная выгрузка воронки",
        "Запусти полную выгрузку всех сделок воронки 15, лимит 5000",
    ),
    (
        "ЛПР для Tomoru по региону",
        "Выбери ЛПР по Томской области и подготовь результат для выгрузки в Tomoru",
    ),
]

LPR_TOMORU_PROMPT: tuple[str, str] = (
    "ЛПР для Tomoru по региону",
    "Выбери ЛПР по Томской области и подготовь результат для выгрузки в Tomoru",
)


class AiPromptService:
    def ensure_defaults(self, db: Session) -> None:
        count = db.scalar(select(func.count()).select_from(AiPromptTemplate))
        if not count:
            for idx, (title, prompt) in enumerate(DEFAULT_PROMPTS):
                db.add(AiPromptTemplate(title=title, prompt=prompt, sort_order=idx))
            db.commit()
        # Идемпотентно гарантируем наличие промпта ЛПР/Tomoru даже на уже
        # заполненной базе (добавляем, только если такого названия ещё нет).
        self.ensure_prompt_exists(db, *LPR_TOMORU_PROMPT)

    def ensure_prompt_exists(self, db: Session, title: str, prompt: str) -> None:
        exists = db.scalar(
            select(func.count())
            .select_from(AiPromptTemplate)
            .where(AiPromptTemplate.title == title)
        )
        if exists:
            return
        max_order = db.scalar(select(func.max(AiPromptTemplate.sort_order))) or -1
        db.add(AiPromptTemplate(title=title, prompt=prompt, sort_order=max_order + 1))
        db.commit()

    def list_prompts(self, db: Session) -> list[AiPromptTemplate]:
        return list(
            db.scalars(
                select(AiPromptTemplate).order_by(AiPromptTemplate.sort_order, AiPromptTemplate.id)
            )
        )

    def create_prompt(self, db: Session, title: str, prompt: str) -> AiPromptTemplate:
        title = title.strip()
        prompt = prompt.strip()
        self._validate(title, prompt)
        max_order = db.scalar(select(func.max(AiPromptTemplate.sort_order))) or -1
        item = AiPromptTemplate(title=title, prompt=prompt, sort_order=max_order + 1)
        db.add(item)
        db.commit()
        db.refresh(item)
        return item

    def update_prompt(self, db: Session, prompt_id: int, title: str, prompt: str) -> AiPromptTemplate:
        item = self._get_or_raise(db, prompt_id)
        title = title.strip()
        prompt = prompt.strip()
        self._validate(title, prompt)
        item.title = title
        item.prompt = prompt
        db.commit()
        db.refresh(item)
        return item

    def delete_prompt(self, db: Session, prompt_id: int) -> None:
        item = self._get_or_raise(db, prompt_id)
        db.delete(item)
        db.commit()

    @staticmethod
    def _get_or_raise(db: Session, prompt_id: int) -> AiPromptTemplate:
        item = db.get(AiPromptTemplate, prompt_id)
        if not item:
            raise LookupError("Промпт не найден")
        return item

    @staticmethod
    def _validate(title: str, prompt: str) -> None:
        if not title:
            raise ValueError("Название не может быть пустым")
        if not prompt:
            raise ValueError("Текст запроса не может быть пустым")
        if len(title) > TITLE_MAX_LEN:
            raise ValueError(f"Название не длиннее {TITLE_MAX_LEN} символов")
        if len(prompt) > PROMPT_MAX_LEN:
            raise ValueError(f"Текст запроса не длиннее {PROMPT_MAX_LEN} символов")
