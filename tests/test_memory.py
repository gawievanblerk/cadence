from __future__ import annotations

from cadence.db.crypto import IdentityCipher
from cadence.domain.enums import ObservationKind
from cadence.memory.assembler import ContextAssembler, Section
from cadence.memory.semantic import SqlVectorStore, cosine
from cadence.memory.store import MemoryStore
from cadence.rag.retriever import KnowledgeRetriever


def make_store(database) -> MemoryStore:
    return MemoryStore(database)


class TestStructuredMemory:
    def test_scalar_kinds_answer_with_the_latest_value(self, database) -> None:
        store = make_store(database)
        client = store.create_client()
        episode = store.create_episode(client, program_id="p", program_version="1.1", content_hash="x")
        for value in (3, 6, 5):
            store.add_observation(
                client_id=client, episode_id=episode, kind=ObservationKind.CONFIDENCE, numeric=value
            )
        projection = store.projection(client_id=client, episode_id=episode)
        assert projection["observations"]["confidence"] == 5
        assert projection["history"]["confidence"] == [3, 6, 5]

    def test_list_kinds_accumulate(self, database) -> None:
        store = make_store(database)
        client = store.create_client()
        episode = store.create_episode(client, program_id="p", program_version="1.1", content_hash="x")
        for value in ("the car", "after lunch"):
            store.add_observation(
                client_id=client,
                episode_id=episode,
                kind=ObservationKind.TRIGGER_SITUATION,
                value=value,
            )
        projection = store.projection(client_id=client, episode_id=episode)
        assert projection["observations"]["trigger_situation"] == ["the car", "after lunch"]

    def test_low_confidence_observations_never_reach_the_rules(self, database) -> None:
        """A half-heard answer must not be able to reroute a pathway."""
        store = make_store(database)
        client = store.create_client()
        episode = store.create_episode(client, program_id="p", program_version="1.1", content_hash="x")
        store.add_observation(
            client_id=client,
            episode_id=episode,
            kind=ObservationKind.LAPSE,
            value="maybe",
            confidence=0.2,
        )
        projection = store.projection(client_id=client, episode_id=episode)
        assert projection["history"]["lapse"] == []
        assert len(store.observations(client)) == 1  # recorded, just not used


class TestPrivacy:
    def test_identity_round_trips_and_is_not_stored_in_the_clear(self, database) -> None:
        cipher = IdentityCipher(None)
        store = MemoryStore(database, cipher)
        client = store.create_client(identity={"name": "Sam", "email": "sam@example.com"})
        assert store.identity(client)["name"] == "Sam"

    def test_encrypted_identity_is_opaque(self, database) -> None:
        import base64
        import os

        key = base64.b64encode(os.urandom(32)).decode()
        store = MemoryStore(database, IdentityCipher(key))
        client = store.create_client(identity={"name": "Sam"})
        blob = store.client(client).identity_encrypted
        assert b"Sam" not in blob
        assert store.identity(client) == {"name": "Sam"}

    def test_export_contains_everything_including_the_decision_log(self, database) -> None:
        store = make_store(database)
        client = store.create_client(identity={"name": "Sam"})
        episode = store.create_episode(client, program_id="p", program_version="1.1", content_hash="x")
        store.add_observation(
            client_id=client, episode_id=episode, kind=ObservationKind.GOAL, value="my daughter"
        )
        export = store.export(client)
        assert export["client"]["identity"]["name"] == "Sam"
        assert len(export["observations"]) == 1
        assert "decisions" in export

    def test_erasure_removes_conversation_and_identifiers(self, database) -> None:
        store = make_store(database)
        client = store.create_client(identity={"name": "Sam"})
        episode = store.create_episode(client, program_id="p", program_version="1.1", content_hash="x")
        run = store.create_run(episode_id=episode, client_id=client, session_id="s01-intake", session_version=1)
        store.add_turn(session_run_id=run.id, client_id=client, step_id="x", role="client", text="my name is Sam")
        store.add_observation(
            client_id=client, episode_id=episode, kind=ObservationKind.CONFIDENCE, numeric=4
        )

        store.erase(client)

        assert store.turns(run.id) == []
        assert store.client(client).identity_encrypted is None
        # De-identified outcome data is retained by default; free text is not.
        remaining = store.observations(client)
        assert len(remaining) == 1
        assert remaining[0].text is None
        assert remaining[0].numeric == 4


class TestSemanticStore:
    def test_cosine_of_identical_vectors_is_one(self) -> None:
        assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
        assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_search_ranks_by_similarity_and_scopes(self, database) -> None:
        store = SqlVectorStore(database)
        store.add(client_id="c1", scope="turn", ref_id="a", text="a", vector=[1.0, 0.0], model="m")
        store.add(client_id="c1", scope="turn", ref_id="b", text="b", vector=[0.0, 1.0], model="m")
        store.add(client_id="c2", scope="turn", ref_id="c", text="c", vector=[1.0, 0.0], model="m")
        hits = store.search([1.0, 0.0], client_id="c1", scopes=["turn"], limit=2)
        assert [h.ref_id for h in hits] == ["a", "b"]
        assert all(h.ref_id != "c" for h in hits)


class TestContextAssembler:
    def test_budget_drops_low_priority_sections_whole(self, bundle) -> None:
        assembler = ContextAssembler(retriever=KnowledgeRetriever(bundle), budget_tokens=20)
        sections = [
            Section("frame", "x" * 40, 1, droppable=False),
            Section("summaries", "y" * 400, 3),
            Section("semantic", "z" * 400, 5),
        ]
        context = assembler._fit(sections)
        assert "frame" in [s.name for s in context.sections]
        assert "semantic" in context.dropped

    def test_the_programme_frame_is_never_dropped(self, bundle) -> None:
        assembler = ContextAssembler(retriever=KnowledgeRetriever(bundle), budget_tokens=1)
        context = assembler._fit([Section("frame", "x" * 4000, 1, droppable=False)])
        assert [s.name for s in context.sections] == ["frame"]

    def test_authored_knowledge_is_returned_verbatim(self, bundle) -> None:
        retriever = KnowledgeRetriever(bundle)
        passage = retriever.authored("kb-urge-surfing")
        assert passage is not None
        assert passage.reason == "authored"
        assert "wave" in passage.text
