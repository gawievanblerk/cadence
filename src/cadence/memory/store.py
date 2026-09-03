"""The structured memory layer.

This is the part of "long-term memory" that people usually skip, and skipping
it is why so many multi-session assistants feel amnesiac in month three. A
transcript is not memory. Memory is a small number of typed facts, each with a
timestamp and a provenance, that can be queried without a model in the loop.

Everything the treatment rules read comes from here. Semantic recall (the next
module) sits on top and answers a different question: not "what is the client's
confidence" but "what did they say, once, about their brother".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from cadence.db.crypto import IdentityCipher
from cadence.db.engine import Database
from cadence.db.models import (
    ClientRow,
    DecisionRow,
    EgressRow,
    EmbeddingRow,
    EpisodeRow,
    ObservationRow,
    SessionRunRow,
    SummaryRow,
    TurnRow,
)
from cadence.domain.enums import ObservationKind
from cadence.domain.models import DecisionRecord
from cadence.rules.expressions import OpenMapping

# Observations below this extractor confidence are recorded but never used to
# evaluate a rule. A model that half-heard "I had one" must not be able to
# reroute a treatment pathway on its own.
RULE_CONFIDENCE_FLOOR = 0.6

# Kinds whose history matters as a list. Everything else answers with its most
# recent value, because "confidence" means today's confidence.
LIST_KINDS = {
    ObservationKind.TRIGGER_SITUATION,
    ObservationKind.TECHNIQUE_USED,
    ObservationKind.GOAL,
    ObservationKind.BARRIER,
    ObservationKind.LAPSE,
    ObservationKind.LIFE_CHANGE,
    ObservationKind.COMMITMENT,
}


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class MemoryStore:
    def __init__(self, db: Database, cipher: IdentityCipher | None = None):
        self.db = db
        self.cipher = cipher or IdentityCipher(None)

    # -- clients -------------------------------------------------------------

    def create_client(
        self, *, identity: dict[str, Any] | None = None, locale: str = "en", timezone_name: str = "UTC"
    ) -> str:
        client_id = _uid("cli")
        with self.db.session() as s:
            s.add(
                ClientRow(
                    id=client_id,
                    identity_encrypted=self.cipher.seal(identity or {}),
                    locale=locale,
                    timezone=timezone_name,
                    flags={},
                )
            )
        return client_id

    def client(self, client_id: str) -> ClientRow:
        with self.db.session() as s:
            row = s.get(ClientRow, client_id)
            if row is None:
                raise KeyError(f"no client {client_id!r}")
            return row

    def identity(self, client_id: str) -> dict[str, Any]:
        """Read direct identifiers. Called by the care team's views, never by the counsellor."""
        return self.cipher.open(self.client(client_id).identity_encrypted)

    def set_flags(self, client_id: str, flags: dict[str, Any]) -> None:
        with self.db.session() as s:
            row = s.get(ClientRow, client_id)
            if row is None:
                raise KeyError(client_id)
            row.flags = {**(row.flags or {}), **flags}

    def set_pathway(self, client_id: str, pathway: str) -> None:
        with self.db.session() as s:
            row = s.get(ClientRow, client_id)
            if row is None:
                raise KeyError(client_id)
            row.pathway = pathway

    # -- episodes ------------------------------------------------------------

    def create_episode(
        self, client_id: str, *, program_id: str, program_version: str, content_hash: str
    ) -> str:
        episode_id = _uid("epi")
        with self.db.session() as s:
            s.add(
                EpisodeRow(
                    id=episode_id,
                    client_id=client_id,
                    program_id=program_id,
                    program_version=program_version,
                    content_hash=content_hash,
                    sessions_completed=[],
                    queued_sessions=[],
                    fired_rules=[],
                    state={},
                )
            )
        return episode_id

    def episode(self, episode_id: str) -> EpisodeRow:
        with self.db.session() as s:
            row = s.get(EpisodeRow, episode_id)
            if row is None:
                raise KeyError(f"no episode {episode_id!r}")
            return row

    def update_episode(self, episode_id: str, **fields: Any) -> None:
        with self.db.session() as s:
            row = s.get(EpisodeRow, episode_id)
            if row is None:
                raise KeyError(episode_id)
            for key, value in fields.items():
                setattr(row, key, value)

    # -- session runs --------------------------------------------------------

    def create_run(
        self, *, episode_id: str, client_id: str, session_id: str, session_version: int
    ) -> SessionRunRow:
        run_id = _uid("run")
        with self.db.session() as s:
            row = SessionRunRow(
                id=run_id,
                episode_id=episode_id,
                client_id=client_id,
                session_id=session_id,
                session_version=session_version,
                state="in_progress",
                started_at=datetime.now(timezone.utc),
                unmet_mandatories=[],
                scratch={},
            )
            s.add(row)
        return self.run(run_id)

    def run(self, run_id: str) -> SessionRunRow:
        with self.db.session() as s:
            row = s.get(SessionRunRow, run_id)
            if row is None:
                raise KeyError(f"no session run {run_id!r}")
            return row

    def update_run(self, run_id: str, **fields: Any) -> None:
        with self.db.session() as s:
            row = s.get(SessionRunRow, run_id)
            if row is None:
                raise KeyError(run_id)
            for key, value in fields.items():
                setattr(row, key, value)

    def runs_for_episode(self, episode_id: str) -> list[SessionRunRow]:
        with self.db.session() as s:
            return list(
                s.scalars(
                    select(SessionRunRow)
                    .where(SessionRunRow.episode_id == episode_id)
                    .order_by(SessionRunRow.started_at)
                )
            )

    # -- turns ---------------------------------------------------------------

    def add_turn(
        self, *, session_run_id: str, client_id: str, step_id: str, role: str, text: str, meta: dict | None = None
    ) -> str:
        turn_id = _uid("trn")
        with self.db.session() as s:
            s.add(
                TurnRow(
                    id=turn_id,
                    session_run_id=session_run_id,
                    client_id=client_id,
                    step_id=step_id,
                    role=role,
                    text=text,
                    meta=meta or {},
                )
            )
        return turn_id

    def turns(self, session_run_id: str) -> list[TurnRow]:
        with self.db.session() as s:
            return list(
                s.scalars(
                    select(TurnRow)
                    .where(TurnRow.session_run_id == session_run_id)
                    .order_by(TurnRow.created_at, TurnRow.id)
                )
            )

    # -- observations --------------------------------------------------------

    def add_observation(
        self,
        *,
        client_id: str,
        episode_id: str,
        kind: ObservationKind | str,
        value: Any = None,
        text: str | None = None,
        numeric: float | None = None,
        confidence: float = 1.0,
        session_run_id: str | None = None,
        step_id: str | None = None,
        source: str = "extraction",
    ) -> str:
        obs_id = _uid("obs")
        kind_value = kind.value if isinstance(kind, ObservationKind) else str(kind)
        with self.db.session() as s:
            s.add(
                ObservationRow(
                    id=obs_id,
                    client_id=client_id,
                    episode_id=episode_id,
                    session_run_id=session_run_id,
                    step_id=step_id,
                    kind=kind_value,
                    value={"v": value} if value is not None else None,
                    text=text,
                    numeric=numeric,
                    confidence=confidence,
                    source=source,
                )
            )
        return obs_id

    def observations(
        self,
        client_id: str,
        *,
        kinds: Iterable[str] | None = None,
        episode_id: str | None = None,
        min_confidence: float = 0.0,
        limit: int | None = None,
    ) -> list[ObservationRow]:
        query = select(ObservationRow).where(
            ObservationRow.client_id == client_id,
            ObservationRow.confidence >= min_confidence,
        )
        if kinds:
            query = query.where(ObservationRow.kind.in_(list(kinds)))
        if episode_id:
            query = query.where(ObservationRow.episode_id == episode_id)
        query = query.order_by(ObservationRow.observed_at, ObservationRow.id)
        if limit:
            query = query.limit(limit)
        with self.db.session() as s:
            return list(s.scalars(query))

    # -- summaries -----------------------------------------------------------

    def add_summary(
        self,
        *,
        client_id: str,
        episode_id: str,
        session_run_id: str,
        session_id: str,
        headline: str,
        narrative: str,
        key_points: Sequence[str],
    ) -> str:
        summary_id = _uid("sum")
        with self.db.session() as s:
            s.add(
                SummaryRow(
                    id=summary_id,
                    client_id=client_id,
                    episode_id=episode_id,
                    session_run_id=session_run_id,
                    session_id=session_id,
                    headline=headline,
                    narrative=narrative,
                    key_points=list(key_points),
                )
            )
        return summary_id

    def summaries(self, client_id: str, *, limit: int | None = None) -> list[SummaryRow]:
        query = (
            select(SummaryRow)
            .where(SummaryRow.client_id == client_id)
            .order_by(SummaryRow.created_at.desc())
        )
        if limit:
            query = query.limit(limit)
        with self.db.session() as s:
            return list(s.scalars(query))

    # -- audit ---------------------------------------------------------------

    def record_decisions(self, decisions: Iterable[DecisionRecord]) -> None:
        with self.db.session() as s:
            for d in decisions:
                s.add(
                    DecisionRow(
                        id=d.id,
                        client_id=d.client_id,
                        episode_id=d.episode_id,
                        session_run_id=d.session_run_id,
                        rule_id=d.rule_id,
                        expression=d.expression,
                        inputs=_jsonable(d.inputs),
                        fired=d.fired,
                        effects=_jsonable(d.effects),
                    )
                )

    def decisions(self, client_id: str) -> list[DecisionRow]:
        with self.db.session() as s:
            return list(
                s.scalars(
                    select(DecisionRow)
                    .where(DecisionRow.client_id == client_id)
                    .order_by(DecisionRow.created_at)
                )
            )

    def record_egress(self, records: Iterable[Any]) -> None:
        with self.db.session() as s:
            for r in records:
                s.add(
                    EgressRow(
                        client_pseudonym=r.client_pseudonym,
                        provider=r.provider,
                        model=r.model,
                        role=r.role,
                        purpose=r.purpose,
                        characters=r.characters,
                        masked=r.masked,
                    )
                )

    # -- GDPR ----------------------------------------------------------------

    def export(self, client_id: str) -> dict[str, Any]:
        """Article 20 portability: everything held about one person, as JSON.

        Includes the decision log, because a client is entitled to know not
        only what was recorded about them but what the system did with it.
        """
        client = self.client(client_id)
        with self.db.session() as s:
            episodes = list(s.scalars(select(EpisodeRow).where(EpisodeRow.client_id == client_id)))
            runs = list(s.scalars(select(SessionRunRow).where(SessionRunRow.client_id == client_id)))
            turns = list(s.scalars(select(TurnRow).where(TurnRow.client_id == client_id)))
            observations = list(
                s.scalars(select(ObservationRow).where(ObservationRow.client_id == client_id))
            )
            summaries = list(s.scalars(select(SummaryRow).where(SummaryRow.client_id == client_id)))
            decisions = list(s.scalars(select(DecisionRow).where(DecisionRow.client_id == client_id)))

        return {
            "client": {
                "id": client.id,
                "identity": self.cipher.open(client.identity_encrypted),
                "locale": client.locale,
                "pathway": client.pathway,
                "flags": client.flags,
                "created_at": client.created_at.isoformat(),
            },
            "episodes": [_row_dict(e) for e in episodes],
            "session_runs": [_row_dict(r) for r in runs],
            "turns": [_row_dict(t) for t in turns],
            "observations": [_row_dict(o) for o in observations],
            "summaries": [_row_dict(x) for x in summaries],
            "decisions": [_row_dict(d) for d in decisions],
        }

    def erase(self, client_id: str, *, keep_deidentified_outcomes: bool = True) -> dict[str, int]:
        """Article 17 erasure.

        Verbatim conversation, embeddings and direct identifiers go. Typed
        outcome observations can be kept in de-identified form where the
        operator has a lawful basis for retaining service statistics; that is a
        policy decision, so it is a parameter rather than a default.
        """
        removed: dict[str, int] = {}
        with self.db.session() as s:
            for table, name in ((TurnRow, "turns"), (EmbeddingRow, "embeddings"), (SummaryRow, "summaries")):
                column = table.client_id
                result = s.execute(delete(table).where(column == client_id))
                removed[name] = result.rowcount or 0

            if not keep_deidentified_outcomes:
                removed["observations"] = (
                    s.execute(delete(ObservationRow).where(ObservationRow.client_id == client_id)).rowcount or 0
                )
            else:
                for obs in s.scalars(
                    select(ObservationRow).where(ObservationRow.client_id == client_id)
                ):
                    obs.text = None
                    obs.value = None
                removed["observations_deidentified"] = 1

            client = s.get(ClientRow, client_id)
            if client is not None:
                client.identity_encrypted = None
                client.deleted_at = datetime.now(timezone.utc)
                removed["identity"] = 1
        return removed

    # -- projection for the rules engine -------------------------------------

    def projection(
        self,
        *,
        client_id: str,
        episode_id: str,
        captures: dict[str, Any] | None = None,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Flatten memory into the JSON-safe view expressions are evaluated against.

        The names produced here are exactly the roots the content validator
        allows, which is what keeps a clinician's expression from referring to
        something that does not exist at runtime.
        """
        client = self.client(client_id)
        episode = self.episode(episode_id)
        rows = self.observations(client_id, min_confidence=RULE_CONFIDENCE_FLOOR)

        history: dict[str, list[Any]] = {}
        for row in rows:
            history.setdefault(row.kind, []).append(_obs_value(row))

        observations: dict[str, Any] = {}
        for kind in ObservationKind:
            values = history.get(kind.value, [])
            if kind in LIST_KINDS:
                observations[kind.value] = values
            else:
                observations[kind.value] = values[-1] if values else None

        marks: dict[str, Any] = {}
        last_session_at: str | None = None
        for run in self.runs_for_episode(episode_id):
            if run.completed_at:
                stamp = run.completed_at.date().isoformat()
                marks[run.session_id] = stamp
                last_session_at = stamp

        full_history = {kind.value: history.get(kind.value, []) for kind in ObservationKind}

        return {
            "observations": observations,
            "history": full_history,
            "episode": {
                "id": episode.id,
                "program_version": episode.program_version,
                "sessions_completed": list(episode.sessions_completed or []),
                "queued_sessions": list(episode.queued_sessions or []),
                "quit_date": episode.quit_date,
                "started_at": episode.started_at.date().isoformat() if episode.started_at else None,
                "last_session_at": last_session_at,
                "session_count": len(episode.sessions_completed or []),
            },
            "client": {
                "id": client.id,
                "locale": client.locale,
                "pathway": client.pathway,
                "flags": OpenMapping(client.flags or {}),
            },
            "flags": OpenMapping(client.flags or {}),
            "marks": OpenMapping(marks),
            "session": OpenMapping(session or {}),
            "capture": OpenMapping(captures or {}),
        }


def _obs_value(row: ObservationRow) -> Any:
    if row.numeric is not None:
        return row.numeric
    if row.value is not None:
        return row.value.get("v")
    return row.text


def _row_dict(row: Any) -> dict[str, Any]:
    out = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        if isinstance(value, bytes):
            value = "<encrypted>"
        out[column.name] = value
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
