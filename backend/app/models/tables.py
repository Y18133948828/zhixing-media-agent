from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "media_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="editor", server_default="editor")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Base):
    __tablename__ = "media_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(String(36), unique=True, default=new_id)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("media_users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    request_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    input: Mapped[dict] = mapped_column(JSONB)
    # Business projection of the checkpoint, not an alternative execution state.
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    interrupt_id: Mapped[str | None] = mapped_column(String(128))
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Job(Base):
    __tablename__ = "media_jobs"
    __table_args__ = (
        UniqueConstraint("task_id", "request_key"),
        Index(
            "uq_media_active_job",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("media_tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Revision(Base):
    __tablename__ = "media_revisions"
    __table_args__ = (UniqueConstraint("task_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("media_tasks.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    article: Mapped[dict] = mapped_column(JSONB)
    decision: Mapped[dict | None] = mapped_column(JSONB)


class Asset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (UniqueConstraint("task_id", "path"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("media_tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(100))


class Event(Base):
    __tablename__ = "media_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("media_tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Knowledge(Base):
    __tablename__ = "media_knowledge"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("media_users.id"), index=True)
    category: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Feedback(Base):
    __tablename__ = "media_feedback"
    __table_args__ = (UniqueConstraint("task_id", "platform"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("media_tasks.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))
    views: Mapped[int] = mapped_column(Integer)
    likes: Mapped[int] = mapped_column(Integer)
    saves: Mapped[int] = mapped_column(Integer)
    comments: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, default="")


class CallMetric(Base):
    __tablename__ = "media_call_metrics"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("media_tasks.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(36))
    stage: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(200))
    duration_ms: Mapped[float] = mapped_column(Float)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float | None] = mapped_column(Float)
    success: Mapped[bool] = mapped_column(Boolean)
    usage_source: Mapped[str] = mapped_column(String(30))
    modality: Mapped[str] = mapped_column(String(20), default="text", server_default="text")
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
