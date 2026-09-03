"""Relational schema.

Deliberately ordinary tables. The retrieval cleverness lives above this layer;
the storage layer's job is to be boring, exportable, and portable between
SQLite and Postgres without a rewrite, because "we must not be locked to a
cloud provider" starts with not being locked to a proprietary datastore.

Two columns deserve comment.

`clients.identity_encrypted` holds the only direct identifiers in the system,
as a sealed blob. Nothing joins on it, nothing indexes it, and the context
assembler cannot reach it. Erasure under GDPR Article 17 is therefore a
single-row overwrite plus a transcript purge, not an archaeology exercise.

`embeddings.vector` is JSON on SQLite and pgvector on Postgres. The retrieval
code asks the store for nearest neighbours and does not know which it got,
which is what lets the pilot run on a file and production run on a cluster.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ClientRow(Base):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    identity_encrypted: Mapped[bytes | None] = mapped_column(nullable=True)
    locale: Mapped[str] = mapped_column(String(8), default="en")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    pathway: Mapped[str] = mapped_column(String(32), default="standard")
    flags: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    episodes: Mapped[list[EpisodeRow]] = relationship(back_populates="client")


class EpisodeRow(Base):
    __tablename__ = "episodes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    program_id: Mapped[str] = mapped_column(String(64))
    program_version: Mapped[str] = mapped_column(String(32))
    content_hash: Mapped[str] = mapped_column(String(32), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    quit_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sessions_completed: Mapped[list] = mapped_column(JSON, default=list)
    queued_sessions: Mapped[list] = mapped_column(JSON, default=list)
    fired_rules: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[dict] = mapped_column(JSON, default=dict)

    client: Mapped[ClientRow] = relationship(back_populates="episodes")


class SessionRunRow(Base):
    __tablename__ = "session_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(40), index=True)
    session_id: Mapped[str] = mapped_column(String(64))
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default="scheduled")
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step_cursor: Mapped[int] = mapped_column(Integer, default=0)
    followup_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    risk: Mapped[str] = mapped_column(String(16), default="none")
    unmet_mandatories: Mapped[list] = mapped_column(JSON, default=list)
    scratch: Mapped[dict] = mapped_column(JSON, default=dict)


class TurnRow(Base):
    __tablename__ = "turns"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_run_id: Mapped[str] = mapped_column(ForeignKey("session_runs.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(40), index=True)
    step_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class ObservationRow(Base):
    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(40), index=True)
    episode_id: Mapped[str] = mapped_column(String(40), index=True)
    session_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    numeric: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(24), default="extraction")


Index("ix_obs_client_kind_time", ObservationRow.client_id, ObservationRow.kind, ObservationRow.observed_at)


class SummaryRow(Base):
    __tablename__ = "session_summaries"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(40), index=True)
    episode_id: Mapped[str] = mapped_column(String(40), index=True)
    session_run_id: Mapped[str] = mapped_column(String(40))
    session_id: Mapped[str] = mapped_column(String(64))
    headline: Mapped[str] = mapped_column(Text)
    narrative: Mapped[str] = mapped_column(Text)
    key_points: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EmbeddingRow(Base):
    """One vector per recallable item.

    `scope` says what kind of thing was embedded, so semantic recall can be
    restricted to summaries when a session wants the shape of past sessions and
    widened to turns when it wants a specific remembered moment.
    """

    __tablename__ = "embeddings"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    client_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    scope: Mapped[str] = mapped_column(String(24), index=True)  # turn | summary | knowledge
    ref_id: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    vector: Mapped[list] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DecisionRow(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(40), index=True)
    episode_id: Mapped[str] = mapped_column(String(40), index=True)
    session_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rule_id: Mapped[str] = mapped_column(String(64))
    expression: Mapped[str] = mapped_column(Text)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    fired: Mapped[bool] = mapped_column(Boolean, default=False)
    effects: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EgressRow(Base):
    """The Article 30 processing register, written on every outbound call."""

    __tablename__ = "egress_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_pseudonym: Mapped[str] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(64))
    characters: Mapped[int] = mapped_column(Integer, default=0)
    masked: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
