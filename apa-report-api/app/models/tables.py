import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow
from app.models.enums import ReportStatus, SubQuestionRole

_STATUS_VALUES = ", ".join(f"'{s.value}'" for s in ReportStatus)
_ROLE_VALUES = ", ".join(f"'{r.value}'" for r in SubQuestionRole)


def _report_fk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),
        CheckConstraint("progress_percent BETWEEN 0 AND 100", name="progress_range"),
        Index("ix_reports_user_created", "user_id", "created_at", "id"),
        Index("ix_reports_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    topic: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ReportStatus.QUEUED.value)
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    # Generation checkpoint: the last generation stage whose output is fully saved.
    checkpoint_stage: Mapped[str | None] = mapped_column(String(32))
    # Worker lease: which worker owns the job and until when (renewed by heartbeat).
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    title: Mapped[str | None] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list[str] | None] = mapped_column(JSONB)
    methodology: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Bumped whenever the finished content changes; part of the export S3 key.
    content_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column()


class Plan(Base):
    __tablename__ = "plans"

    report_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("reports.id", ondelete="CASCADE"), primary_key=True)
    central_question: Mapped[str] = mapped_column(Text, nullable=False)
    interpretation_note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow, onupdate=utcnow)



class SubQuestion(Base):
    __tablename__ = "sub_questions"
    __table_args__ = (
        CheckConstraint(f"role IN ({_ROLE_VALUES})", name="role_valid"),
        CheckConstraint('"order" >= 1', name="order_positive"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    report_id: Mapped[uuid.UUID] = _report_fk()
    order: Mapped[int] = mapped_column("order", Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_unsent", "created_at", postgresql_where="sent_at IS NULL"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_id: Mapped[uuid.UUID] = _report_fk()
    queue: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column()


class Paper(Base):
    """A candidate paper retrieved for a sub-question (search checkpoint)."""

    __tablename__ = "papers"
    __table_args__ = (UniqueConstraint("report_id", "sub_question_id", "work_id", name="uq_papers_report_sq_work"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_id: Mapped[uuid.UUID] = _report_fk()
    sub_question_id: Mapped[str] = mapped_column(String(64), nullable=False)
    work_id: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    seed_connections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    work: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ScreeningResult(Base):
    __tablename__ = "screening_results"
    __table_args__ = (
        UniqueConstraint("report_id", "sub_question_id", "work_id", name="uq_screening_report_sq_work"),
        CheckConstraint("score BETWEEN 0 AND 3", name="score_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_id: Mapped[uuid.UUID] = _report_fk()
    sub_question_id: Mapped[str] = mapped_column(String(64), nullable=False)
    work_id: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False)


class EvidenceCard(Base):
    __tablename__ = "evidence_cards"
    __table_args__ = (UniqueConstraint("report_id", "sub_question_id", "work_id", name="uq_evidence_report_sq_work"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_id: Mapped[uuid.UUID] = _report_fk()
    sub_question_id: Mapped[str] = mapped_column(String(64), nullable=False)
    work_id: Mapped[str] = mapped_column(String(32), nullable=False)
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    design: Mapped[str | None] = mapped_column(Text)
    sample: Mapped[str | None] = mapped_column(Text)
    effect_direction: Mapped[str | None] = mapped_column(String(32))
    claim_strength: Mapped[str | None] = mapped_column(String(32))
    limitations: Mapped[str | None] = mapped_column(Text)
    background_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ReportSection(Base):
    __tablename__ = "report_sections"
    __table_args__ = (UniqueConstraint("report_id", "order", name="uq_report_sections_report_order"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    report_id: Mapped[uuid.UUID] = _report_fk()
    order: Mapped[int] = mapped_column("order", Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    heading: Mapped[str] = mapped_column(Text, nullable=False)
    sub_question_id: Mapped[str | None] = mapped_column(String(64))
    draft_content: Mapped[str] = mapped_column(Text, nullable=False)
    verified_content: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)


class ReportReference(Base):
    __tablename__ = "report_references"
    __table_args__ = (UniqueConstraint("report_id", "work_id", name="uq_report_references_report_work"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    report_id: Mapped[uuid.UUID] = _report_fk()
    work_id: Mapped[str] = mapped_column(String(32), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    apa_citation: Mapped[str] = mapped_column(Text, nullable=False)
    in_text_citation: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    year: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    volume: Mapped[str | None] = mapped_column(String(64))
    issue: Mapped[str | None] = mapped_column(String(64))
    pages: Mapped[str | None] = mapped_column(String(64))
    doi: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    is_open_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
