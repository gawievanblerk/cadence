"""Building the working context for a single turn.

This is the module the brief was really asking about. The naive design sends
the whole conversation history and hopes; it degrades in month two, costs more
every session, and quietly loses the one fact that mattered somewhere in the
middle of the window.

What goes into a turn is instead assembled from four sources with different
guarantees, in a fixed priority order, under a budget:

  1. Programme frame - persona, session objectives, mandatory prohibitions.
     Never truncated. If this does not fit, nothing should be sent at all.
  2. Structured recall - the observation kinds the session's `recall:` block
     asked for, rendered as a compact fact sheet. Deterministic, cheap, and the
     reason a session in month three knows the client's quit date exactly
     rather than approximately.
  3. Episodic summaries - the last few session summaries, most recent first.
     Compressed once, at the end of each session, and reused thereafter.
  4. Semantic recall - top-k passages from this client's own past words, keyed
     on what the step is about. Last in, first cut.

Verbatim turns from the *current* session are always included: within a session
the counsellor must be able to hear what was just said. Verbatim turns from
past sessions are never included. That single rule is what keeps a
nine-month programme's context window flat instead of linear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cadence.content.models import Session, Step
from cadence.domain.enums import ObservationKind
from cadence.rag.retriever import KnowledgeRetriever, Passage

# Rough characters-per-token. Deliberately pessimistic: the budget is a safety
# rail, and over-estimating cost is the safe direction to be wrong in.
CHARS_PER_TOKEN = 3.6


@dataclass
class Section:
    name: str
    body: str
    priority: int
    droppable: bool = True

    @property
    def tokens(self) -> int:
        return int(len(self.body) / CHARS_PER_TOKEN) + 1


@dataclass
class WorkingContext:
    sections: list[Section] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    budget_tokens: int = 0

    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.sections)

    def render(self) -> str:
        return "\n\n".join(f"## {s.name}\n{s.body}".rstrip() for s in self.sections if s.body.strip())

    def section(self, name: str) -> str | None:
        for s in self.sections:
            if s.name == name:
                return s.body
        return None


# Human-readable labels for the fact sheet. Clinical shorthand in a prompt
# produces clinical shorthand in the reply.
_LABELS = {
    ObservationKind.NICOTINE_USE: "Nicotine use",
    ObservationKind.TRIGGER_SITUATION: "Trigger situations recorded",
    ObservationKind.CRAVING_INTENSITY: "Strongest craving rated",
    ObservationKind.CONFIDENCE: "Most recent confidence rating (0-10)",
    ObservationKind.MOTIVATION: "Motivation",
    ObservationKind.GOAL: "Their reasons for stopping, in their words",
    ObservationKind.COMMITMENT: "Commitments made",
    ObservationKind.TECHNIQUE_USED: "Techniques used so far",
    ObservationKind.TECHNIQUE_EFFECTIVENESS: "How useful they found them (0-10)",
    ObservationKind.BARRIER: "Difficulties they have named",
    ObservationKind.LAPSE: "Lapses recorded",
    ObservationKind.QUIT_DATE: "Quit date",
    ObservationKind.SUPPORT_PERSON: "Support person",
    ObservationKind.LIFE_CHANGE: "Changes in their situation",
    ObservationKind.SAFETY_CONCERN: "Safety concerns raised",
    ObservationKind.FREE_NOTE: "Other notes",
}


class ContextAssembler:
    def __init__(
        self,
        *,
        retriever: KnowledgeRetriever,
        semantic: Any | None = None,
        budget_tokens: int = 2400,
    ):
        self.retriever = retriever
        self.semantic = semantic
        self.budget_tokens = budget_tokens

    def build(
        self,
        *,
        session: Session,
        step: Step,
        projection: dict[str, Any],
        summaries: list[Any],
        current_turns: list[Any],
        client_id: str,
        locale: str = "en",
    ) -> WorkingContext:
        bundle = self.retriever.bundle
        sections: list[Section] = []

        # 1. Programme frame. Not droppable.
        frame = [
            bundle.program.persona.strip(),
            "",
            f"You are conducting session: {session.title}.",
            "The objectives of this session are:",
            *[f"  - {o}" for o in session.objectives],
        ]
        prohibitions = [*bundle.program.global_prohibitions]
        prohibitions += [m.prohibition.strip() for m in session.mandatory if m.prohibition]
        if prohibitions:
            frame += ["", "You must not, under any circumstances:"]
            frame += [f"  - {p}" for p in prohibitions]
        sections.append(Section("Your role in this session", "\n".join(frame), 1, droppable=False))

        # 2. Structured recall, restricted to what the session asked for.
        facts = self._fact_sheet(session, projection)
        if facts:
            sections.append(Section("What is already recorded about this client", facts, 2, droppable=False))

        # 3. Episodic summaries.
        if summaries:
            body = "\n\n".join(
                f"{s.session_id} - {s.headline}\n{s.narrative}".strip() for s in summaries[:3]
            )
            sections.append(Section("Previous sessions, in brief", body, 3))

        # 4. Authored knowledge for this step, then supporting passages.
        passages = self._passages(step, client_id)
        if passages:
            body = "\n\n".join(f"[{p.title}]\n{p.text}" for p in passages)
            body += (
                "\n\nDeliver the substance of the material above. Do not add "
                "techniques, claims or reassurance that it does not contain."
            )
            sections.append(Section("Source material for this step", body, 2, droppable=False))

        # 5. Semantic recall from the client's own past words.
        recalled = self._semantic(step, client_id)
        if recalled:
            body = "\n".join(f'- "{r.text.strip()}"' for r in recalled)
            sections.append(
                Section("Things this client said earlier that may be relevant", body, 5)
            )

        # 6. This session's conversation so far, verbatim.
        if current_turns:
            body = "\n".join(
                f"{'Counsellor' if t.role == 'counsellor' else 'Client'}: {t.text.strip()}"
                for t in current_turns[-10:]
            )
            sections.append(Section("This session so far", body, 2, droppable=False))

        return self._fit(sections)

    # -- pieces --------------------------------------------------------------

    def _fact_sheet(self, session: Session, projection: dict[str, Any]) -> str:
        wanted = session.recall or []
        if not wanted:
            return ""
        lines = []
        observations = projection.get("observations", {})
        history = projection.get("history", {})
        for name in wanted:
            try:
                kind = ObservationKind(name)
            except ValueError:
                continue
            label = _LABELS.get(kind, name.replace("_", " ").capitalize())
            values = history.get(name) or []
            latest = observations.get(name)
            if isinstance(latest, list) or (values and len(values) > 1 and latest in values):
                items = values if isinstance(latest, list) else values
                if not items:
                    continue
                rendered = "; ".join(str(v) for v in items[-6:])
                lines.append(f"- {label}: {rendered}")
            elif latest not in (None, "", []):
                lines.append(f"- {label}: {latest}")

        episode = projection.get("episode", {})
        if episode.get("quit_date"):
            lines.append(f"- Quit date on record: {episode['quit_date']}")
        if episode.get("sessions_completed"):
            lines.append(f"- Sessions completed: {', '.join(episode['sessions_completed'])}")
        return "\n".join(lines)

    def _passages(self, step: Step, client_id: str) -> list[Passage]:
        passages: list[Passage] = []
        if step.knowledge_ref:
            authored = self.retriever.authored(step.knowledge_ref)
            if authored:
                passages.append(authored)
        if passages and step.text:
            passages += self.retriever.supporting(
                step.text, client_id=client_id, exclude={p.doc_id for p in passages}, limit=1
            )
        return passages

    def _semantic(self, step: Step, client_id: str) -> list[Any]:
        if self.semantic is None or step.kind.value in ("say", "branch"):
            return []
        query = step.text or ""
        if not query.strip():
            return []
        return self.semantic.recall(query, client_id=client_id, scopes=["summary", "turn"], limit=3)

    def _fit(self, sections: list[Section]) -> WorkingContext:
        """Drop droppable sections, lowest priority first, until it fits.

        Truncating mid-section is worse than dropping one: a half-quoted
        clinical document is a clinical risk, and a half-rendered fact sheet
        silently loses a fact. So sections are whole or absent.
        """
        context = WorkingContext(budget_tokens=self.budget_tokens)
        ordered = sorted(sections, key=lambda s: s.priority)
        total = sum(s.tokens for s in ordered)

        while total > self.budget_tokens:
            candidates = [s for s in ordered if s.droppable]
            if not candidates:
                break
            victim = max(candidates, key=lambda s: (s.priority, s.tokens))
            ordered.remove(victim)
            context.dropped.append(victim.name)
            total -= victim.tokens

        context.sections = ordered
        return context
