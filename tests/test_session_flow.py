"""End-to-end behaviour of the treatment engine.

Every test here runs against the offline provider, so the assertions are about
the engine's decisions rather than about a model's wording. That is the point:
the clinically consequential behaviour is deterministic and testable, and it
would assert identically against any vendor.
"""

from __future__ import annotations

from conftest import INTAKE_ANSWERS, walk


def enrol_and_intake(engine, answers=None):
    enrolment = engine.enrol(identity={"name": "Test Client"})
    result = walk(engine, enrolment.episode_id, answers or INTAKE_ANSWERS)
    return enrolment, result


class TestSessionMechanics:
    def test_a_session_runs_to_completion_and_records_a_summary(self, engine) -> None:
        enrolment, result = enrol_and_intake(engine)
        assert result.finished
        assert not result.awaiting_client
        summaries = engine.store.summaries(enrolment.client_id)
        assert len(summaries) == 1
        assert summaries[0].session_id == "s01-intake"

    def test_conditional_steps_are_skipped_when_their_condition_is_false(self, engine) -> None:
        """probe-low-confidence only runs when confidence is at or below threshold."""
        enrolment, _ = enrol_and_intake(engine)  # answers rate confidence at 7
        run = engine.store.runs_for_episode(enrolment.episode_id)[0]
        steps = {t.step_id for t in engine.store.turns(run.id)}
        assert "rate-confidence" in steps
        assert "probe-low-confidence" not in steps

    def test_conditional_steps_run_when_their_condition_holds(self, engine) -> None:
        answers = {**INTAKE_ANSWERS, "rate-confidence": "About a 2."}
        enrolment, _ = enrol_and_intake(engine, answers)
        run = engine.store.runs_for_episode(enrolment.episode_id)[0]
        steps = {t.step_id for t in engine.store.turns(run.id)}
        assert "probe-low-confidence" in steps

    def test_a_session_gate_refuses_a_session_that_is_not_open_yet(self, engine) -> None:
        import pytest

        enrolment = engine.enrol()
        with pytest.raises(PermissionError):
            engine.start(enrolment.episode_id, "s02-triggers")

    def test_next_session_follows_the_authored_sequence(self, engine) -> None:
        enrolment, _ = enrol_and_intake(engine)
        # The gate on session 2 requires a day to have passed, so nothing is
        # available immediately. That is the content's decision, not the code's.
        assert engine.next_session(enrolment.episode_id) is None


class TestMandatoryRules:
    def test_a_session_cannot_close_while_a_required_observation_is_missing(self, engine) -> None:
        """m01-own-reasons requires two goals; a one-word answer should re-open the step."""
        answers = {**INTAKE_ANSWERS, "q-reasons": ["Money.", "And my daughter asked me to."]}
        enrolment = engine.enrol()
        result = walk(engine, enrolment.episode_id, answers)
        run = engine.store.runs_for_episode(enrolment.episode_id)[0]
        asked = [t.step_id for t in engine.store.turns(run.id) if t.role == "counsellor"]
        assert asked.count("q-reasons") >= 2
        assert result.finished

    def test_the_follow_up_budget_is_a_hard_limit(self, engine) -> None:
        """q-current-use follows up at most twice, whatever the client says."""
        answers = {**INTAKE_ANSWERS, "q-current-use": "I would rather not say."}
        enrolment = engine.enrol()
        walk(engine, enrolment.episode_id, answers)
        run = engine.store.runs_for_episode(enrolment.episode_id)[0]
        asked = [t.step_id for t in engine.store.turns(run.id) if t.role == "counsellor"]
        assert asked.count("q-current-use") <= 3  # the question, plus at most two follow-ups

    def test_a_follow_up_shows_the_client_a_question_and_never_the_model_instruction(
        self, engine
    ) -> None:
        """`followup.instruction` is written for the model. The client sees `followup.text`."""
        answers = {**INTAKE_ANSWERS, "q-reasons": "I have tried before but nothing worked."}
        enrolment = engine.enrol()
        walk(engine, enrolment.episode_id, answers)
        step = next(s for s in engine.bundle().session("s01-intake").steps if s.id == "q-reasons")
        run = engine.store.runs_for_episode(enrolment.episode_id)[0]
        followups = [
            t
            for t in engine.store.turns(run.id)
            if t.role == "counsellor" and (t.meta or {}).get("followup")
        ]
        assert followups, "a single reason should have triggered a follow-up"
        for turn in followups:
            assert turn.text.strip() != step.followup.instruction.strip()
            assert turn.text.strip() == step.followup.text.strip()


class TestPathwayRules:
    def test_no_quit_date_does_not_move_a_client_onto_maintenance(self, engine) -> None:
        """r40 counts days since the quit date. A missing date is not a date long ago."""
        answers = {**INTAKE_ANSWERS, "q-quit-date": "I am not sure yet."}
        enrolment = engine.enrol()
        result = walk(engine, enrolment.episode_id, answers)
        assert result.finished
        assert engine.store.episode(enrolment.episode_id).quit_date is None
        fired = {d.rule_id for d in engine.store.decisions(enrolment.client_id) if d.fired}
        assert "r40-maintenance" not in fired
        assert engine.store.client(enrolment.client_id).pathway == "standard"


class TestSafety:
    def test_a_disclosure_stops_the_programme_and_escalates(self, engine) -> None:
        enrolment = engine.enrol()
        result = engine.start(enrolment.episode_id)
        result = engine.respond(
            result.session_run_id, "Honestly some days I do not want to be here at all."
        )
        assert result.escalated
        assert result.finished
        assert "doctor" in result.text.lower() or "crisis" in result.text.lower()

        client = engine.store.client(enrolment.client_id)
        assert client.flags.get("program_paused") is True
        concerns = engine.store.observations(enrolment.client_id, kinds=["safety_concern"])
        assert concerns and concerns[0].value["v"] == "self_harm"

    def test_escalation_does_not_depend_on_a_model_call(self, engine) -> None:
        """The safety screen is deterministic, so a provider outage cannot disable it."""
        from cadence.orchestration import guardrails

        assert guardrails.screen_input("I keep thinking about ending it").triggered
        assert guardrails.screen_input("I had a cigarette on Friday").triggered is False


class TestPathwaysAndRules:
    def test_a_lapse_inserts_the_recovery_session_and_changes_the_pathway(self, engine) -> None:
        enrolment = engine.enrol()
        walk(engine, enrolment.episode_id, INTAKE_ANSWERS)
        _advance_clock(engine, enrolment.episode_id, 3)
        walk(engine, enrolment.episode_id, TRIGGERS, "s02-triggers")
        _advance_clock(engine, enrolment.episode_id, 3)
        walk(engine, enrolment.episode_id, PLAN, "s03-quit-plan")
        _advance_clock(engine, enrolment.episode_id, 3)
        walk(engine, enrolment.episode_id, QUIT_DAY, "s04-quit-day")
        _advance_clock(engine, enrolment.episode_id, 7)
        result = walk(engine, enrolment.episode_id, WEEK_ONE, "s05-week-one-review")

        assert "r10-first-lapse" in result.rules_fired
        progress = engine.progress(enrolment.episode_id)
        assert progress["pathway"] == "lapse_recovery"
        assert "s90-lapse-recovery" in progress["queued"]
        assert engine.next_session(enrolment.episode_id) == "s90-lapse-recovery"

    def test_every_rule_evaluation_is_recorded_whether_or_not_it_fired(self, engine) -> None:
        enrolment, _ = enrol_and_intake(engine)
        decisions = engine.store.decisions(enrolment.client_id)
        assert decisions
        assert any(d.fired is False for d in decisions), "non-firing rules must be recorded too"
        for decision in decisions:
            assert decision.expression

    def test_a_low_confidence_rating_queues_the_confidence_session(self, engine) -> None:
        answers = {**INTAKE_ANSWERS, "rate-confidence": "About a 3."}
        enrolment = engine.enrol()
        result = walk(engine, enrolment.episode_id, answers)
        assert "r20-low-confidence" in result.rules_fired
        assert "s91-confidence-build" in engine.progress(enrolment.episode_id)["queued"]


class TestMemoryAcrossSessions:
    def test_session_three_recalls_what_session_two_recorded(self, engine) -> None:
        enrolment = engine.enrol()
        walk(engine, enrolment.episode_id, INTAKE_ANSWERS)
        _advance_clock(engine, enrolment.episode_id, 3)
        walk(engine, enrolment.episode_id, TRIGGERS, "s02-triggers")
        _advance_clock(engine, enrolment.episode_id, 3)

        result = engine.start(enrolment.episode_id, "s03-quit-plan")
        opening = result.text.lower()
        assert "car" in opening or "coffee" in opening

    def test_no_past_transcript_is_carried_into_a_later_session(self, engine) -> None:
        """Context stays flat over months because verbatim history is not reused."""
        enrolment = engine.enrol()
        walk(engine, enrolment.episode_id, INTAKE_ANSWERS)
        _advance_clock(engine, enrolment.episode_id, 3)
        result = engine.start(enrolment.episode_id, "s02-triggers")

        run = engine.store.run(result.session_run_id)
        first_turn = engine.store.turns(run.id)[0]
        assert first_turn.meta.get("context_tokens", 0) < 1500

    def test_the_episode_stays_pinned_to_its_content_version(self, engine) -> None:
        enrolment = engine.enrol()
        episode = engine.store.episode(enrolment.episode_id)
        assert episode.program_version == "1.1"
        assert episode.content_hash == engine.bundle().content_hash


def _advance_clock(engine, episode_id: str, days: int) -> None:
    """Move completed sessions back in time so real-day gates can be exercised."""
    from datetime import timedelta

    from cadence.db.models import SessionRunRow

    with engine.db.session() as s:
        for row in s.query(SessionRunRow).filter(SessionRunRow.episode_id == episode_id):
            if row.completed_at:
                row.completed_at = row.completed_at - timedelta(days=days)
            if row.started_at:
                row.started_at = row.started_at - timedelta(days=days)


TRIGGERS = {
    "recall-last-time": "Yes, that is right.",
    "q-triggers": "Coffee on the back step; the car after the school run; after lunch outside with a colleague.",
    "rate-strongest": "The car one, an 8.",
    "q-attach-technique": "The car after the school run.",
}
PLAN = {
    "recall-triggers": "That is the list.",
    "q-plan-each": "Mints in the car; coffee at the front instead; walk the block after lunch.",
    "q-support": "My brother.",
    "q-environment": "The evening before",
    "q-confirm-date": "Yes, it stands.",
}
QUIT_DAY = {
    "rate-confidence": "A 6.",
    "rehearse": "The drive home. I will phone my brother first.",
}
WEEK_ONE = {
    "q-any-use": "Yes, I had one on the Friday.",
    "q-lapse-circumstances": "In the car park after a bad meeting. Someone offered me one and I took it.",
    "q-techniques-used": "The mints most days, and the walk twice. About a 6 for how useful.",
    "rate-confidence": "A 5.",
}
