"""Retrieval over the clinical knowledge base.

Two things distinguish this from a generic RAG bolt-on, and both matter
clinically.

First, retrieval is *scoped by the content author*, not by similarity alone. A
`teach` step names the document it teaches. The retriever will fetch that
document, not a document that happens to score well. Similarity search is used
only to add supporting passages, and only from documents the session's phase
permits. A cessation counsellor that answers a question about withdrawal by
retrieving a passage about relapse from four sessions ahead is not being
helpful, it is being early.

Second, retrieved text is quoted into the prompt as source material with an
instruction not to exceed it. The generation step's job is to deliver the
clinical team's material in the client's language and at the client's level; it
is not to add to it.
"""

from __future__ import annotations

from dataclasses import dataclass

from cadence.content.loader import ContentBundle
from cadence.memory.semantic import SemanticMemory


@dataclass
class Passage:
    doc_id: str
    title: str
    text: str
    reason: str  # "authored" | "similar"


class KnowledgeRetriever:
    def __init__(self, bundle: ContentBundle, semantic: SemanticMemory | None = None):
        self.bundle = bundle
        self.semantic = semantic

    def index(self) -> int:
        """Embed the knowledge corpus. Idempotent per content version in practice.

        Documents are indexed whole rather than chunked. Clinical knowledge
        documents in this program are a few hundred words and internally
        coherent; chunking them would split a technique from its caveat, which
        is exactly the sentence a counsellor must not drop.
        """
        if self.semantic is None:
            return 0
        count = 0
        for doc in self.bundle.knowledge.values():
            self.semantic.index_text(
                client_id=None,
                scope="knowledge",
                ref_id=doc.id,
                text=f"{doc.title}\n\n{doc.body}",
            )
            count += 1
        return count

    def authored(self, doc_id: str) -> Passage | None:
        doc = self.bundle.knowledge.get(doc_id)
        if doc is None:
            return None
        return Passage(doc.id, doc.title, doc.body.strip(), "authored")

    def supporting(
        self, query: str, *, client_id: str, exclude: set[str] | None = None, limit: int = 2
    ) -> list[Passage]:
        if self.semantic is None or not query.strip():
            return []
        exclude = exclude or set()
        hits = self.semantic.recall(query, client_id=client_id, scopes=["knowledge"], limit=limit + len(exclude))
        passages = []
        for hit in hits:
            if hit.ref_id in exclude:
                continue
            doc = self.bundle.knowledge.get(hit.ref_id)
            if doc is None or doc.audience != "client":
                continue
            passages.append(Passage(doc.id, doc.title, doc.body.strip(), "similar"))
            if len(passages) >= limit:
                break
        return passages
