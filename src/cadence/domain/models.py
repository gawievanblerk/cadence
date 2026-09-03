from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from cadence.domain.enums import ObservationKind, Pathway, RiskLevel, SessionState


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Client(BaseModel):
    """A person in treatment.

    Direct identifiers live in `identity` and are the only fields encrypted at
    rest and never sent to a model provider. Everything the counsellor needs to
    do its job lives outside `identity`, which is what makes pseudonymous
    inference possible.
    """

    id: str = Field(default_factory=lambda: _uid("cli"))
    identity: dict[str, Any] = Field(default_factory=dict)
    locale: str = "en"
    timezone: str = "UTC"
    pathway: Pathway = Pathway.STANDARD
    flags: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)

    @property
    def pseudonym(self) -> str:
        """The handle used in every prompt. Never the client's real name."""
        return self.id


class Episode(BaseModel):
    """One course of treatment for one client under one program version.

    Pinning the program version to the episode is what stops a mid-treatment
    content edit from changing the program under a client who is halfway
    through it.
    """

    id: str = Field(default_factory=lambda: _uid("epi"))
    client_id: str
    program_id: str
    program_version: str
    started_at: datetime = Field(default_factory=_now)
    quit_date: date | None = None
    completed_at: datetime | None = None
    sessions_completed: list[str] = Field(default_factory=list)
    queued_sessions: list[str] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)


class Turn(BaseModel):
    """A single exchange. Verbatim text is retained under its own retention rule."""

    id: str = Field(default_factory=lambda: _uid("trn"))
    session_run_id: str
    step_id: str
    role: str  # "counsellor" | "client" | "system"
    text: str
    created_at: datetime = Field(default_factory=_now)
    meta: dict[str, Any] = Field(default_factory=dict)


class Observation(BaseModel):
    """A typed clinical fact, with provenance.

    `confidence` is the extractor's confidence, not the client's. Low-confidence
    observations are recorded but excluded from rule evaluation, so a model
    misreading never silently reroutes a treatment pathway.
    """

    id: str = Field(default_factory=lambda: _uid("obs"))
    client_id: str
    episode_id: str
    session_run_id: str | None = None
    step_id: str | None = None
    kind: ObservationKind
    value: Any = None
    text: str | None = None
    numeric: float | None = None
    confidence: float = 1.0
    observed_at: datetime = Field(default_factory=_now)
    source: str = "extraction"  # extraction | self_report | clinician | system


class SessionRun(BaseModel):
    """One attempt at one session of the program."""

    id: str = Field(default_factory=lambda: _uid("run"))
    episode_id: str
    client_id: str
    session_id: str
    session_version: int
    state: SessionState = SessionState.SCHEDULED
    current_step: str | None = None
    step_cursor: int = 0
    followup_count: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    risk: RiskLevel = RiskLevel.NONE
    unmet_mandatories: list[str] = Field(default_factory=list)
    scratch: dict[str, Any] = Field(default_factory=dict)


class EpisodicSummary(BaseModel):
    """The compressed record of a completed session.

    This, not the transcript, is what a session three months later reads.
    """

    id: str = Field(default_factory=lambda: _uid("sum"))
    client_id: str
    episode_id: str
    session_run_id: str
    session_id: str
    headline: str
    narrative: str
    key_points: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class DecisionRecord(BaseModel):
    """Why the system did what it did. One row per rule evaluation.

    Written for every branch whether it fired or not, because 'the rule did not
    fire' is the thing a clinical reviewer most often needs to see.
    """

    id: str = Field(default_factory=lambda: _uid("dec"))
    client_id: str
    episode_id: str
    session_run_id: str | None = None
    rule_id: str
    expression: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    fired: bool = False
    effects: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
