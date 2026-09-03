"""Assembly. One place where the layers are wired together.

Everything below is constructor injection on purpose: the API layer, the CLI,
the demo scripts and the test suite all build a `TreatmentEngine` the same way,
and any of them can substitute a component without the others noticing. That is
also what makes the provider-swap demo a four-line script rather than a fork.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cadence.config import Settings, load_settings
from cadence.content.loader import ContentBundle
from cadence.content.registry import ContentRegistry
from cadence.db.crypto import IdentityCipher
from cadence.db.engine import Database, make_database
from cadence.llm.router import ModelRouter
from cadence.memory.assembler import ContextAssembler
from cadence.memory.semantic import SemanticMemory, SqlVectorStore
from cadence.memory.store import MemoryStore
from cadence.orchestration.session_runner import SessionRunner, TurnResult
from cadence.rag.retriever import KnowledgeRetriever


@dataclass
class Enrolment:
    client_id: str
    episode_id: str
    program_version: str


class TreatmentEngine:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        database: Database | None = None,
        registry: ContentRegistry | None = None,
        router: ModelRouter | None = None,
        context_budget: int = 2400,
    ):
        self.settings = settings or load_settings()
        self.registry = registry or ContentRegistry(
            self.settings.content_path, self.settings.content_version
        )
        self.db = database or make_database(self.settings.database_url)
        self.store = MemoryStore(self.db, IdentityCipher(self.settings.encryption_key))
        self.router = router or ModelRouter(self.settings)
        self.vectors = SqlVectorStore(self.db)
        self.semantic = SemanticMemory(self.vectors, self.router)
        self.context_budget = context_budget
        self._indexed: set[str] = set()

    # -- content -------------------------------------------------------------

    def bundle(self, version: str | None = None) -> ContentBundle:
        return self.registry.get(version)

    def runner_for(self, version: str | None = None) -> SessionRunner:
        bundle = self.bundle(version)
        retriever = KnowledgeRetriever(bundle, self.semantic)
        if bundle.content_hash not in self._indexed:
            retriever.index()
            self._indexed.add(bundle.content_hash)
        return SessionRunner(
            bundle=bundle,
            store=self.store,
            router=self.router,
            assembler=ContextAssembler(
                retriever=retriever, semantic=self.semantic, budget_tokens=self.context_budget
            ),
            semantic=self.semantic,
        )

    # -- lifecycle -----------------------------------------------------------

    def enrol(
        self, *, identity: dict[str, Any] | None = None, locale: str = "en", version: str | None = None
    ) -> Enrolment:
        bundle = self.bundle(version)
        client_id = self.store.create_client(identity=identity, locale=locale)
        episode_id = self.store.create_episode(
            client_id,
            program_id=bundle.program.id,
            program_version=bundle.version_id,
            content_hash=bundle.content_hash,
        )
        return Enrolment(client_id, episode_id, bundle.version_id)

    def next_session(self, episode_id: str) -> str | None:
        """Which session this client should do next.

        Inserted sessions come first: a lapse recovery session queued by a rule
        is not something to get to eventually. After that, the ordinary
        sequence, skipping anything already done and anything whose gate has
        not opened. Sessions with a sequence of 900 or more are inserted-only
        and never scheduled by position.
        """
        episode = self.store.episode(episode_id)
        bundle = self.bundle(episode.program_version)
        runner = self.runner_for(episode.program_version)
        projection = self.store.projection(client_id=episode.client_id, episode_id=episode_id)
        completed = set(episode.sessions_completed or [])

        for session_id in episode.queued_sessions or []:
            session = bundle.sessions.get(session_id)
            if session is None:
                continue
            if session.opens_when and not runner._safe_truthy(
                session.opens_when, projection, session_id
            ):
                continue
            return session_id

        for session in bundle.ordered_sessions():
            if session.id in completed or session.sequence >= 900:
                continue
            if session.opens_when and not runner._safe_truthy(
                session.opens_when, projection, session.id
            ):
                return None  # the sequence is ordered; a closed gate means "not yet"
            return session.id
        return None

    def start(self, episode_id: str, session_id: str | None = None) -> TurnResult:
        episode = self.store.episode(episode_id)
        target = session_id or self.next_session(episode_id)
        if target is None:
            raise LookupError("no session is currently available for this episode")
        runner = self.runner_for(episode.program_version)
        return runner.begin(episode_id=episode_id, session_id=target)

    def respond(self, run_id: str, text: str) -> TurnResult:
        run = self.store.run(run_id)
        episode = self.store.episode(run.episode_id)
        runner = self.runner_for(episode.program_version)
        return runner.respond(run_id, text)

    # -- reporting -----------------------------------------------------------

    def progress(self, episode_id: str) -> dict[str, Any]:
        episode = self.store.episode(episode_id)
        bundle = self.bundle(episode.program_version)
        projection = self.store.projection(client_id=episode.client_id, episode_id=episode_id)
        observations = projection["observations"]
        scheduled = [s.id for s in bundle.ordered_sessions() if s.sequence < 900]
        done = list(episode.sessions_completed or [])
        return {
            "episode_id": episode_id,
            "client_id": episode.client_id,
            "program_version": episode.program_version,
            "pathway": projection["client"]["pathway"],
            "flags": projection["flags"],
            "sessions_completed": done,
            "sessions_remaining": [s for s in scheduled if s not in done],
            "queued": list(episode.queued_sessions or []),
            "quit_date": episode.quit_date,
            "confidence": observations.get("confidence"),
            "craving_intensity": observations.get("craving_intensity"),
            "triggers_recorded": len(projection["history"].get("trigger_situation", [])),
            "lapses_recorded": len(projection["history"].get("lapse", [])),
            "completion_pct": round(100 * len(done) / max(1, len(scheduled))),
        }
