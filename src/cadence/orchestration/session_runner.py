"""The session state machine.

The division of labour here is the whole design, so it is worth stating plainly:

    the state machine decides what happens next;
    the model decides how it is said.

Which step comes next, whether a follow-up is permitted, whether a session may
close, which pathway a lapse routes into - none of that is a model decision.
The model is asked for one thing per turn: prose that delivers the clinician's
material, given a context this module assembled. Everything else is Python and
YAML, which is why swapping the model changes the wording and nothing else.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cadence.content.loader import ContentBundle
from cadence.content.models import Mandatory, Session, Step
from cadence.domain.enums import ObservationKind, RiskLevel, SessionState, StepKind
from cadence.llm.router import ModelRouter
from cadence.llm.types import CompletionRequest, Message, ProviderError, StructuredRequest
from cadence.memory.assembler import ContextAssembler
from cadence.memory.semantic import SemanticMemory
from cadence.memory.store import MemoryStore
from cadence.orchestration import guardrails
from cadence.orchestration.prompts import render
from cadence.orchestration.schemas import SUMMARY_SCHEMA, step_schema
from cadence.rules.engine import RuleEngine
from cadence.rules.expressions import ExpressionError, truthy

log = logging.getLogger("cadence.orchestration")

# What a client is asked when a follow-up has no authored `text`. Neutral on
# purpose: it must be safe for every step in every programme.
GENERIC_FOLLOWUP = "Is there anything else you would like to add to that?"

MAX_REOPEN_PASSES = 2


@dataclass
class TurnResult:
    session_run_id: str
    messages: list[str] = field(default_factory=list)
    step_id: str | None = None
    awaiting_client: bool = False
    finished: bool = False
    escalated: bool = False
    observations: list[str] = field(default_factory=list)
    rules_fired: list[str] = field(default_factory=list)
    dropped_context: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(m.strip() for m in self.messages if m.strip())


class SessionRunner:
    def __init__(
        self,
        *,
        bundle: ContentBundle,
        store: MemoryStore,
        router: ModelRouter,
        assembler: ContextAssembler,
        semantic: SemanticMemory | None = None,
    ):
        self.bundle = bundle
        self.store = store
        self.router = router
        self.assembler = assembler
        self.semantic = semantic
        self.rules = RuleEngine(bundle.ruleset)

    # -- entry points --------------------------------------------------------

    def begin(self, *, episode_id: str, session_id: str) -> TurnResult:
        episode = self.store.episode(episode_id)
        session = self.bundle.session(session_id)

        projection = self.store.projection(client_id=episode.client_id, episode_id=episode_id)
        if session.opens_when and not self._safe_truthy(session.opens_when, projection, session_id):
            raise PermissionError(
                f"session {session_id!r} is not open for this client yet "
                f"(gate: {session.opens_when})"
            )

        run = self.store.create_run(
            episode_id=episode_id,
            client_id=episode.client_id,
            session_id=session.id,
            session_version=session.version,
        )
        self.store.update_run(run.id, scratch={"captures": {}, "followups": {}, "reopened": []})
        return self._drive(run.id)

    def respond(self, run_id: str, utterance: str) -> TurnResult:
        run = self.store.run(run_id)
        if run.state != SessionState.IN_PROGRESS.value:
            raise ValueError(f"session run {run_id} is {run.state}, not in progress")

        session = self.bundle.session(run.session_id)
        step = self._step(session, run.step_cursor)
        result = TurnResult(session_run_id=run_id, step_id=step.id if step else None)

        self.store.add_turn(
            session_run_id=run_id,
            client_id=run.client_id,
            step_id=step.id if step else "-",
            role="client",
            text=utterance,
        )

        # Safety runs first, on the raw utterance, before anything clinical.
        signal = guardrails.screen_input(utterance)
        if signal.triggered:
            return self._escalate(run_id, signal, result)

        # A plain no to a follow-up is an answer. Spend the rest of the budget
        # and move on, rather than asking the same thing again.
        followups_used = int((run.scratch or {}).get("followups", {}).get(step.id, 0)) if step else 0
        if step is not None and step.followup and followups_used and guardrails.is_decline(utterance):
            scratch = dict(run.scratch or {})
            scratch["followups"] = {**scratch.get("followups", {}), step.id: step.followup.max}
            self.store.update_run(run_id, scratch=scratch, step_cursor=run.step_cursor + 1, followup_count=0)
            result.notes.append("client declined the follow-up; moving on")
            return self._drive(run_id, carry=result)

        if step is not None and step.capture:
            captured = self._extract(session, step, utterance, run)
            result.observations = captured["observation_ids"]
            if captured["low_confidence"]:
                result.notes.append(
                    "extraction confidence below the rule floor; recorded but not used for routing"
                )

        if self.semantic is not None and len(utterance.strip()) >= 25:
            self.semantic.index_text(
                client_id=run.client_id, scope="turn", ref_id=run_id, text=utterance
            )

        # A follow-up repeats the same step rather than advancing, and is
        # bounded by a counter the model cannot see or influence.
        if step is not None and self._should_follow_up(session, step, run):
            return self._ask_followup(run_id, session, step, result, utterance)

        self.store.update_run(run_id, step_cursor=run.step_cursor + 1, followup_count=0)
        return self._drive(run_id, carry=result)

    # -- the machine ---------------------------------------------------------

    def _drive(self, run_id: str, carry: TurnResult | None = None) -> TurnResult:
        result = carry or TurnResult(session_run_id=run_id)
        session = self.bundle.session(self.store.run(run_id).session_id)

        for _ in range(len(session.steps) * (MAX_REOPEN_PASSES + 2) + 8):
            run = self.store.run(run_id)
            step = self._step(session, run.step_cursor)

            if step is None:
                reopened = self._reopen_for_mandatories(run_id, session)
                if reopened is not None:
                    result.notes.append(
                        f"mandatory {reopened} is unmet; re-opening the step that satisfies it"
                    )
                    continue
                return self._finish(run_id, session, result)

            projection = self._projection(run)
            if step.when and not self._safe_truthy(step.when, projection, f"{session.id}/{step.id}"):
                self.store.update_run(run_id, step_cursor=run.step_cursor + 1)
                continue

            if step.kind is StepKind.BRANCH:
                self._take_branch(run_id, session, step, projection, result)
                continue

            message = self._speak(run_id, session, step, projection, result)
            result.messages.append(message)
            result.step_id = step.id

            if step.kind in (StepKind.ASK, StepKind.SCALE, StepKind.CHOICE):
                self.store.update_run(run_id, current_step=step.id)
                result.awaiting_client = True
                return result

            self.store.update_run(run_id, step_cursor=run.step_cursor + 1, current_step=step.id)

        result.notes.append("step budget exhausted; session halted for review")
        return result

    @staticmethod
    def _step(session: Session, cursor: int) -> Step | None:
        if 0 <= cursor < len(session.steps):
            return session.steps[cursor]
        return None

    def _projection(self, run: Any) -> dict[str, Any]:
        return self.store.projection(
            client_id=run.client_id,
            episode_id=run.episode_id,
            captures=(run.scratch or {}).get("captures", {}),
            session={"id": run.session_id, "version": run.session_version},
        )

    def _safe_truthy(self, expression: str, projection: dict[str, Any], where: str) -> bool:
        """A broken expression is a content bug, and it fails closed.

        Content is validated at publish time, so reaching here means something
        got past that. Treating the gate as false keeps a malformed condition
        from opening a session it should not.
        """
        try:
            return truthy(expression, {**projection, "thresholds": self.bundle.ruleset.thresholds})
        except ExpressionError as exc:
            log.error("expression failed at %s: %s", where, exc)
            return False

    # -- speaking ------------------------------------------------------------

    def _speak(
        self, run_id: str, session: Session, step: Step, projection: dict[str, Any], result: TurnResult
    ) -> str:
        run = self.store.run(run_id)
        client = self.store.client(run.client_id)
        authored = self.bundle.localise(step.text or "", client.locale)

        context = self.assembler.build(
            session=session,
            step=step,
            projection=projection,
            summaries=self.store.summaries(run.client_id, limit=3),
            current_turns=self.store.turns(run_id),
            client_id=run.client_id,
            locale=client.locale,
        )
        result.dropped_context = context.dropped

        task = self._task(step, authored)
        prompt = render(
            "counsellor.j2",
            context=context.render(),
            task=task,
            style_hint=step.style_hint,
        )

        text = self._generate(
            run=run,
            session=session,
            prompt=prompt,
            authored=self._fallback_text(step, authored),
            step=step,
            context_recall=context.section("What is already recorded about this client") or "",
        )

        self.store.add_turn(
            session_run_id=run_id,
            client_id=run.client_id,
            step_id=step.id,
            role="counsellor",
            text=text,
            meta={"kind": step.kind.value, "context_tokens": context.tokens, "dropped": context.dropped},
        )
        return text

    def _fallback_text(self, step: Step, authored: str) -> str:
        """What the client gets if generation fails or breaches a prohibition.

        Every step kind must have one. A `teach` step with no fallback would
        degrade to silence, which for a step whose whole job is delivering a
        clinical technique is worse than degrading to the document itself.
        """
        if step.kind is StepKind.TEACH:
            doc = self.bundle.knowledge.get(step.knowledge_ref or "")
            return doc.body.strip() if doc else authored
        if step.kind is StepKind.ASSIGN:
            exercise = self.bundle.exercises.get(step.exercise_ref or "")
            if exercise:
                return f"{exercise.title}\n\n{exercise.instructions.strip()}"
            return authored
        if step.kind is StepKind.CHOICE and step.options:
            options = "\n".join(f"  - {o}" for o in step.options)
            return f"{authored}\n\n{options}"
        if step.kind is StepKind.REFLECT:
            return authored or ""
        return authored

    def _task(self, step: Step, authored: str) -> str:
        if step.kind is StepKind.SAY:
            return (
                "Say the following to the client. You may adjust the phrasing to sound "
                "natural in conversation, but do not change its meaning, add advice, or "
                "add anything the clinical team did not write:\n\n"
                f"{authored}"
            )
        if step.kind is StepKind.ASK:
            return (
                "Ask the client this question. Ask it once, in full, and then stop:\n\n"
                f"{authored}"
            )
        if step.kind is StepKind.SCALE:
            return (
                "Ask the client for a rating. Make the scale explicit and do not offer "
                "examples of what a given number would mean:\n\n"
                f"{authored}"
            )
        if step.kind is StepKind.CHOICE:
            options = "\n".join(f"  - {o}" for o in (step.options or []))
            return (
                "Ask the client this question and offer exactly these options, without "
                f"adding any of your own:\n\n{authored}\n\nOptions:\n{options}"
            )
        if step.kind is StepKind.TEACH:
            return (
                "Explain the technique in the source material above to the client, in "
                "your own words, at their level. Stay within the material."
            )
        if step.kind is StepKind.REFLECT:
            return (
                "Open by reflecting back what is already recorded about this client, "
                "using their own words where you have them. Then follow this "
                f"instruction:\n\n{authored}"
            )
        if step.kind is StepKind.ASSIGN:
            exercise = self.bundle.exercises.get(step.exercise_ref or "")
            body = exercise.instructions.strip() if exercise else ""
            title = exercise.title if exercise else "the exercise"
            return (
                f"Set the client this piece of between-session work, called '{title}'. "
                "Give them the instructions as written, briefly framed:\n\n"
                f"{body}"
            )
        return authored

    def _generate(
        self,
        *,
        run: Any,
        session: Session,
        prompt: str,
        authored: str,
        step: Step,
        context_recall: str,
    ) -> str:
        """One turn of prose, screened before it is allowed out.

        The fallback is the point: if generation fails or keeps breaching a
        clinical prohibition, the client gets the clinician's authored text
        verbatim. Degraded, but never unsafe and never silent.
        """
        request = CompletionRequest(
            messages=[Message("user", prompt)],
            system=self.bundle.program.persona.strip(),
            temperature=0.5 if step.kind in (StepKind.SAY, StepKind.TEACH, StepKind.REFLECT) else 0.3,
            max_tokens=500,
            metadata={
                "purpose": f"counsellor:{session.id}/{step.id}",
                "authored_text": authored,
                "step_kind": step.kind.value,
                "recall_summary": context_recall,
            },
        )

        for attempt in (1, 2):
            try:
                completion = self.router.complete(
                    "counsellor", request, pseudonym=run.client_id
                )
            except ProviderError as exc:
                log.error("counsellor generation failed: %s", exc)
                return authored or ""

            report = guardrails.check_output(
                completion.text, program=self.bundle.program, session=session
            )
            if report.ok:
                return completion.text.strip()

            log.warning("guardrail violation on attempt %s: %s", attempt, report.violations)
            if attempt == 2:
                return authored or ""
            request = CompletionRequest(
                messages=[
                    Message("user", prompt),
                    Message("assistant", completion.text),
                    Message(
                        "user",
                        "That response breached a clinical rule for this session: "
                        + "; ".join(report.violations)
                        + ". Rewrite it without that. Do not explain the correction.",
                    ),
                ],
                system=request.system,
                temperature=0.2,
                max_tokens=request.max_tokens,
                metadata=request.metadata,
            )
        return authored or ""

    # -- listening -----------------------------------------------------------

    @staticmethod
    def _read_scale(step: Step, utterance: str) -> dict[str, Any] | None:
        """A plain number on a scale step is read by the engine, not a model.

        "3", "a 3", "3 out of 10" and "About a 3, if I am honest" are all one
        number inside the step's range, and there is nothing for a model to
        interpret. Reading them here means the rules that watch the number can
        trust it fully. Anything less plain, "4 or 5", goes to the reader.
        """
        if step.kind != StepKind.SCALE or len(step.capture) != 1:
            return None
        spec = step.capture[0]
        if spec.kind not in ("integer", "number") or not spec.range:
            return None
        text = utterance.strip()
        if not text or len(text) > 40:
            return None
        lo, hi = float(spec.range[0]), float(spec.range[-1])
        bare = re.sub(rf"\s*(?:/|out of)\s*{int(hi)}\b", "", text, flags=re.I)
        found = re.findall(r"-?\d+(?:\.\d+)?", bare)
        if len(found) != 1:
            return None
        value = float(found[0])
        if not lo <= value <= hi:
            return None
        number: int | float = int(value) if spec.kind == "integer" and value.is_integer() else value
        return {spec.name: number, "_confidence": 1.0, "_verbatim": text}

    def _extract(self, session: Session, step: Step, utterance: str, run: Any) -> dict[str, Any]:
        schema = step_schema(step)
        extracted = self._read_scale(step, utterance)
        if extracted is None:
            prompt = render(
                "extract.j2",
                question=self.bundle.localise(step.text or "", "en"),
                utterance=utterance,
            )
            try:
                extracted = self.router.structured(
                    "extractor",
                    StructuredRequest(
                        messages=[Message("user", prompt)],
                        schema=schema,
                        schema_name=f"{session.id}_{step.id}",
                        metadata={
                            "purpose": f"extract:{session.id}/{step.id}",
                            "client_utterance": utterance,
                        },
                    ),
                    pseudonym=run.client_id,
                ).data
            except ProviderError as exc:
                log.error("extraction failed for %s/%s: %s", session.id, step.id, exc)
                return {"observation_ids": [], "low_confidence": False}

        confidence = float(extracted.get("_confidence", 1.0) or 0.0)
        verbatim = extracted.get("_verbatim")
        observation_ids: list[str] = []
        captures = dict((run.scratch or {}).get("captures", {}))

        for field_spec in step.capture:
            value = extracted.get(field_spec.name)
            if value in (None, "", []):
                continue
            captures[field_spec.name] = value
            kind = self._kind_for(field_spec.maps_to or field_spec.name)
            if kind is None:
                continue
            for single in value if isinstance(value, list) else [value]:
                observation_ids.append(
                    self.store.add_observation(
                        client_id=run.client_id,
                        episode_id=run.episode_id,
                        session_run_id=run.id,
                        step_id=step.id,
                        kind=kind,
                        value=single if not isinstance(single, (int, float)) or isinstance(single, bool) else None,
                        numeric=float(single) if isinstance(single, (int, float)) and not isinstance(single, bool) else None,
                        text=verbatim if isinstance(single, str) else None,
                        confidence=confidence,
                    )
                )

        scratch = dict(run.scratch or {})
        scratch["captures"] = captures
        self.store.update_run(run.id, scratch=scratch)

        if kind_quit := captures.get("quit_date"):
            self.store.update_episode(run.episode_id, quit_date=str(kind_quit))

        return {
            "observation_ids": observation_ids,
            "low_confidence": confidence < 0.6,
        }

    @staticmethod
    def _kind_for(name: str) -> ObservationKind | None:
        try:
            return ObservationKind(name)
        except ValueError:
            return ObservationKind.FREE_NOTE

    def _should_follow_up(self, session: Session, step: Step, run: Any) -> bool:
        if not step.followup:
            return False
        budget = guardrails.FollowupBudget(step.followup.max)
        budget.used = int((run.scratch or {}).get("followups", {}).get(step.id, 0))
        if budget.exhausted:
            return False
        projection = self._projection(self.store.run(run.id))
        return self._safe_truthy(step.followup.when, projection, f"{session.id}/{step.id}.followup")

    def _ask_followup(
        self, run_id: str, session: Session, step: Step, result: TurnResult, utterance: str = ""
    ) -> TurnResult:
        run = self.store.run(run_id)
        scratch = dict(run.scratch or {})
        followups = dict(scratch.get("followups", {}))
        followups[step.id] = followups.get(step.id, 0) + 1
        scratch["followups"] = followups
        self.store.update_run(run_id, scratch=scratch)

        projection = self._projection(self.store.run(run_id))
        context = self.assembler.build(
            session=session,
            step=step,
            projection=projection,
            summaries=[],
            current_turns=self.store.turns(run_id),
            client_id=run.client_id,
        )
        instruction = step.followup.instruction if step.followup else ""
        # The instruction is written for the model. What the client may see, and
        # what they get if generation fails, is the authored question.
        authored = (step.followup.text if step.followup else None) or GENERIC_FOLLOWUP
        task = (
            "The client has answered, but not fully. Their last message was:\n\n"
            f"\"{utterance.strip()}\"\n\n"
            "If that message asked you something, or was not an answer at all, respond to "
            "it in one plain sentence first: answer it if it is simple and within your "
            "remit, otherwise say you cannot help with that here. Then follow up once, "
            f"with a single question, according to this instruction:\n\n{instruction}"
        )
        if step.followup and step.followup.text:
            task += (
                "\n\nThe clinical team's own wording for this follow-up is below. Keep its "
                f"meaning. You may reword it, and add nothing clinical they did not write:"
                f"\n\n{step.followup.text}"
            )
        if followups[step.id] > 1:
            task += (
                "\n\nYou have already asked this once and the client did not answer it. "
                "Do not repeat the same sentence; put it a different way, and make it "
                "easy to answer briefly."
            )
        prompt = render(
            "counsellor.j2",
            context=context.render(),
            task=task,
            style_hint=step.style_hint,
        )
        text = self._generate(
            run=run,
            session=session,
            prompt=prompt,
            authored=authored,
            step=step,
            context_recall="",
        )
        self.store.add_turn(
            session_run_id=run_id,
            client_id=run.client_id,
            step_id=step.id,
            role="counsellor",
            text=text,
            meta={"followup": followups[step.id]},
        )
        result.messages.append(text)
        result.step_id = step.id
        result.awaiting_client = True
        result.notes.append(f"follow-up {followups[step.id]} of {step.followup.max}")
        return result

    # -- branching, gates and closing ---------------------------------------

    def _take_branch(
        self, run_id: str, session: Session, step: Step, projection: dict[str, Any], result: TurnResult
    ) -> None:
        for branch in step.branches:
            if self._safe_truthy(branch.when, projection, f"{session.id}/{step.id}"):
                if branch.end_session:
                    self.store.update_run(run_id, step_cursor=len(session.steps))
                    result.notes.append(f"branch at {step.id} ended the session early")
                    return
                target = next(
                    (i for i, s in enumerate(session.steps) if s.id == branch.goto), None
                )
                if target is not None:
                    self.store.update_run(run_id, step_cursor=target)
                    result.notes.append(f"branch at {step.id} -> {branch.goto}")
                    return
        run = self.store.run(run_id)
        self.store.update_run(run_id, step_cursor=run.step_cursor + 1)

    def _reopen_for_mandatories(self, run_id: str, session: Session) -> str | None:
        """Enforce `requires` mandatories as a gate on completion.

        Returns the id of a mandatory that is unmet and has been re-opened, or
        None when the session may close. A mandatory is re-opened at most
        MAX_REOPEN_PASSES times; past that it is recorded as unmet on the run
        and surfaced to the clinical team rather than trapping the client in a
        loop they cannot get out of.
        """
        run = self.store.run(run_id)
        scratch = dict(run.scratch or {})
        reopened: list[str] = list(scratch.get("reopened", []))
        projection = self._projection(run)

        unmet: list[Mandatory] = []
        for mandatory in session.mandatory:
            if not mandatory.requires:
                continue
            if not self._safe_truthy(
                mandatory.requires, projection, f"{session.id}/{mandatory.id}"
            ):
                unmet.append(mandatory)

        self.store.update_run(run_id, unmet_mandatories=[m.id for m in unmet])

        for mandatory in unmet:
            if not mandatory.satisfied_by:
                continue
            if reopened.count(mandatory.id) >= MAX_REOPEN_PASSES:
                continue
            target = next(
                (i for i, s in enumerate(session.steps) if s.id == mandatory.satisfied_by[0]), None
            )
            if target is None:
                continue
            reopened.append(mandatory.id)
            scratch["reopened"] = reopened
            self.store.update_run(run_id, step_cursor=target, scratch=scratch)
            return mandatory.id
        return None

    def _finish(self, run_id: str, session: Session, result: TurnResult) -> TurnResult:
        run = self.store.run(run_id)
        now = datetime.now(timezone.utc)
        self.store.update_run(
            run_id, state=SessionState.COMPLETED.value, completed_at=now, current_step=None
        )

        self._summarise(run_id, session)

        episode = self.store.episode(run.episode_id)
        completed = list(episode.sessions_completed or [])
        if session.id not in completed:
            completed.append(session.id)
        queued = [s for s in (episode.queued_sessions or []) if s != session.id]
        self.store.update_episode(run.episode_id, sessions_completed=completed, queued_sessions=queued)

        for key, value in (session.on_complete.get("set_flag") or {}).items():
            self.store.set_flags(run.client_id, {key: value})

        result.rules_fired = self._apply_rules(run_id)
        result.finished = True
        result.awaiting_client = False

        if self.router.egress_log:
            self.store.record_egress(self.router.egress_log)
            self.router.egress_log.clear()
        return result

    def _summarise(self, run_id: str, session: Session) -> None:
        run = self.store.run(run_id)
        turns = self.store.turns(run_id)
        transcript = "\n".join(
            f"{'Counsellor' if t.role == 'counsellor' else 'Client'}: {t.text.strip()}" for t in turns
        )
        observations = self.store.observations(
            run.client_id, episode_id=run.episode_id, limit=None
        )
        facts = "\n".join(
            f"- {o.kind}: {o.numeric if o.numeric is not None else (o.value or {}).get('v', o.text)}"
            for o in observations
            if o.session_run_id == run_id
        )

        prompt = render(
            "summarise.j2",
            session_title=session.title,
            objectives=session.objectives,
            facts=facts or "(nothing was recorded)",
            transcript=transcript or "(no conversation)",
        )
        headline = f"{session.title} completed"
        narrative = facts or "Session completed with no recorded observations."
        key_points = [line.lstrip("- ") for line in facts.splitlines()][:5]

        try:
            data = self.router.structured(
                "summariser",
                StructuredRequest(
                    messages=[Message("user", prompt)],
                    schema=SUMMARY_SCHEMA,
                    schema_name="session_summary",
                    metadata={"purpose": f"summarise:{session.id}", "client_utterance": facts},
                ),
                pseudonym=run.client_id,
            ).data
            headline = data.get("headline") or headline
            narrative = data.get("narrative") or narrative
            key_points = data.get("key_points") or key_points
        except (ProviderError, Exception) as exc:  # noqa: BLE001 - summary must never block closing
            log.warning("summary generation fell back to the recorded facts: %s", exc)

        self.store.add_summary(
            client_id=run.client_id,
            episode_id=run.episode_id,
            session_run_id=run_id,
            session_id=session.id,
            headline=headline,
            narrative=narrative,
            key_points=key_points,
        )
        if self.semantic is not None:
            self.semantic.index_text(
                client_id=run.client_id,
                scope="summary",
                ref_id=run_id,
                text=f"{session.title}: {headline}. {narrative}",
            )

    # -- rules ---------------------------------------------------------------

    def _apply_rules(self, run_id: str) -> list[str]:
        run = self.store.run(run_id)
        episode = self.store.episode(run.episode_id)
        projection = self._projection(run)

        outcome = self.rules.evaluate(
            projection,
            client_id=run.client_id,
            episode_id=run.episode_id,
            session_run_id=run_id,
            already_fired=set(episode.fired_rules or []),
        )
        self.store.record_decisions(outcome.decisions)
        for error in outcome.errors:
            log.error("rule evaluation error: %s", error)

        if not outcome.effects:
            return []

        merged = outcome.merged()
        queued = list(episode.queued_sessions or [])
        if merged.insert_session and merged.insert_session not in queued:
            queued.insert(0, merged.insert_session)
        if merged.remove_session and merged.remove_session in queued:
            queued.remove(merged.remove_session)

        fired = list(episode.fired_rules or []) + outcome.fired_ids
        self.store.update_episode(run.episode_id, queued_sessions=queued, fired_rules=fired)

        if merged.set_flag:
            self.store.set_flags(run.client_id, merged.set_flag)
        if merged.set_pathway:
            self.store.set_pathway(run.client_id, merged.set_pathway)
        if merged.escalate:
            self.store.update_run(run_id, risk=RiskLevel.ESCALATE.value)

        return outcome.fired_ids

    # -- safety --------------------------------------------------------------

    def _escalate(self, run_id: str, signal: guardrails.SafetySignal, result: TurnResult) -> TurnResult:
        run = self.store.run(run_id)
        self.store.add_observation(
            client_id=run.client_id,
            episode_id=run.episode_id,
            session_run_id=run_id,
            kind=ObservationKind.SAFETY_CONCERN,
            value=signal.concern,
            text=signal.quote,
            confidence=1.0,
            source="system",
        )
        self.store.update_run(
            run_id, risk=RiskLevel.ESCALATE.value, state=SessionState.ABANDONED.value
        )
        self.store.set_flags(run.client_id, {"program_paused": True})

        safety = self.bundle.program.safety or {}
        message = str(safety.get("escalation_message", "")).strip()
        contact = str(safety.get("contact_line", "")).strip()

        result.messages = [m for m in (message, contact) if m]
        result.escalated = True
        result.finished = True
        result.awaiting_client = False
        result.notes.append(f"safety escalation: {signal.concern}")
        result.rules_fired = self._apply_rules(run_id)
        return result
