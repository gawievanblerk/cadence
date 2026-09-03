"""Semantic recall.

Structured memory answers questions the program knew to ask. Semantic memory
answers the ones it did not: the client mentioned, once, in session two, that
their brother-in-law smokes on the patio, and session nine is planning a family
gathering.

The store is behind an interface for a reason that is not academic. A pilot
runs happily on JSON vectors in SQLite with cosine similarity computed in
Python; at a few thousand clients that becomes the slowest thing in the system.
Swapping in pgvector - or Qdrant, or whatever survives the next two years - is
then a class, not a project, and the retrieval policy above it does not change.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from cadence.db.engine import Database
from cadence.db.models import EmbeddingRow


@dataclass
class Recalled:
    ref_id: str
    scope: str
    text: str
    score: float
    meta: dict[str, Any]


class VectorStore(ABC):
    @abstractmethod
    def add(
        self, *, client_id: str | None, scope: str, ref_id: str, text: str, vector: list[float], model: str
    ) -> None: ...

    @abstractmethod
    def search(
        self,
        vector: list[float],
        *,
        client_id: str | None = None,
        scopes: list[str] | None = None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[Recalled]: ...

    @abstractmethod
    def count(self, *, client_id: str | None = None, scope: str | None = None) -> int: ...


class SqlVectorStore(VectorStore):
    """Vectors as JSON, cosine in Python.

    Honest about what it is: correct, portable, and O(n) per query. Adequate to
    a few thousand vectors per client scope, which is well beyond a pilot, and
    replaced rather than tuned when it stops being adequate.
    """

    def __init__(self, db: Database):
        self.db = db

    def add(
        self, *, client_id: str | None, scope: str, ref_id: str, text: str, vector: list[float], model: str
    ) -> None:
        with self.db.session() as s:
            s.add(
                EmbeddingRow(
                    id=f"emb_{uuid.uuid4().hex[:16]}",
                    client_id=client_id,
                    scope=scope,
                    ref_id=ref_id,
                    text=text,
                    vector=list(vector),
                    model=model,
                )
            )

    def search(
        self,
        vector: list[float],
        *,
        client_id: str | None = None,
        scopes: list[str] | None = None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[Recalled]:
        query = select(EmbeddingRow)
        if client_id is not None:
            # Knowledge-base vectors have no client; client recall must see both
            # its own rows and the shared corpus when asked for it.
            query = query.where(
                (EmbeddingRow.client_id == client_id) | (EmbeddingRow.client_id.is_(None))
            )
        if scopes:
            query = query.where(EmbeddingRow.scope.in_(scopes))

        with self.db.session() as s:
            rows = list(s.scalars(query))

        scored = []
        for row in rows:
            score = cosine(vector, row.vector)
            if score >= min_score:
                scored.append(
                    Recalled(
                        ref_id=row.ref_id,
                        scope=row.scope,
                        text=row.text,
                        score=score,
                        meta={"model": row.model, "created_at": row.created_at.isoformat()},
                    )
                )
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:limit]

    def count(self, *, client_id: str | None = None, scope: str | None = None) -> int:
        query = select(EmbeddingRow)
        if client_id is not None:
            query = query.where(EmbeddingRow.client_id == client_id)
        if scope:
            query = query.where(EmbeddingRow.scope == scope)
        with self.db.session() as s:
            return len(list(s.scalars(query)))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class SemanticMemory:
    """Indexing policy: what gets embedded, and what does not.

    Not every turn is worth remembering. Embedding all of them makes recall
    noisier, not richer - the top result for "family" becomes whichever of forty
    near-identical greetings happened to score highest. So the indexer takes
    session summaries, the client's own words at steps that captured something,
    and nothing else.
    """

    INDEXABLE_ROLES = {"client"}

    def __init__(self, store: VectorStore, router: Any):
        self.store = store
        self.router = router

    def index_text(self, *, client_id: str | None, scope: str, ref_id: str, text: str) -> None:
        text = text.strip()
        if len(text) < 25:
            return
        result = self.router.embed([text])
        self.store.add(
            client_id=client_id,
            scope=scope,
            ref_id=ref_id,
            text=text,
            vector=result.vectors[0],
            model=result.model,
        )

    def recall(
        self, query: str, *, client_id: str, scopes: list[str] | None = None, limit: int = 4
    ) -> list[Recalled]:
        if not query.strip():
            return []
        vector = self.router.embed([query]).vectors[0]
        return self.store.search(
            vector, client_id=client_id, scopes=scopes or ["summary", "turn"], limit=limit
        )
