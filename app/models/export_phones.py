"""Export phone registry for call-result matching."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.legacy import utcnow


class ExportPhoneEntry(Base):
    __tablename__ = "export_phone_entries"
    __table_args__ = (
        UniqueConstraint("export_job_id", "phone_hash", name="uq_export_phone_entries_job_hash"),
        Index("ix_export_phone_entries_portal_hash", "portal_id", "phone_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    export_job_id: Mapped[int] = mapped_column(ForeignKey("export_jobs.id", ondelete="CASCADE"), index=True)
    phone_hash: Mapped[str] = mapped_column(String(64), index=True)
    normalized_phone: Mapped[str] = mapped_column(String(32))
    deal_id: Mapped[int] = mapped_column(BigInteger)
    contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    export_mode: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
