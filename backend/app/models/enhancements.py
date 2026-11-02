from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.tables import Base, new_id


class PromptVersion(Base):
    __tablename__ = "media_prompt_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("media_users.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    instruction: Mapped[str] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSONB, default=dict)
    digest: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PromptExperiment(Base):
    __tablename__ = "media_prompt_experiments"
    __table_args__ = (UniqueConstraint("owner_id", "stage"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("media_users.id"), index=True)
    stage: Mapped[str] = mapped_column(String(30))
    control_id: Mapped[str] = mapped_column(ForeignKey("media_prompt_versions.id"))
    treatment_id: Mapped[str] = mapped_column(ForeignKey("media_prompt_versions.id"))
    percentage: Mapped[int] = mapped_column(Integer, default=50)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class GenerationRating(Base):
    __tablename__ = "media_generation_ratings"
    task_id: Mapped[str] = mapped_column(ForeignKey("media_tasks.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("media_users.id"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    dimensions: Mapped[dict] = mapped_column(JSONB, default=dict)
    comment: Mapped[str] = mapped_column(Text, default="")
    prompt_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    result_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationCase(Base):
    __tablename__ = "media_evaluation_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("media_users.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("media_tasks.id"))
    name: Mapped[str] = mapped_column(String(150))
    context: Mapped[dict] = mapped_column(JSONB)
    expected: Mapped[dict] = mapped_column(JSONB, default=dict)
    rules: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvaluationRun(Base):
    __tablename__ = "media_evaluation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("media_users.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    cases: Mapped[list] = mapped_column(JSONB)
    variants: Mapped[list] = mapped_column(JSONB)
    results: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeChunk(Base):
    __tablename__ = "media_knowledge_chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_id: Mapped[str] = mapped_column(
        ForeignKey("media_knowledge.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("media_users.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(JSONB)
    embedding_model: Mapped[str | None] = mapped_column(String(200))


class RateBucket(Base):
    __tablename__ = "media_rate_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer)


class SafetyAudit(Base):
    __tablename__ = "media_safety_audits"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("media_tasks.id"), index=True)
    stage: Mapped[str] = mapped_column(String(50))
    verdict: Mapped[str] = mapped_column(String(30))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OAuthAttempt(Base):
    __tablename__ = "media_oauth_attempts"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(100))
    verifier: Mapped[str] = mapped_column(String(150))
    browser_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExternalIdentity(Base):
    __tablename__ = "media_external_identities"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    issuer: Mapped[str] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(300))
    user_id: Mapped[str] = mapped_column(ForeignKey("media_users.id"))
