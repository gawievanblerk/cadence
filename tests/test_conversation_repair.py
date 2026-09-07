"""Three things a real transcript showed the counsellor doing badly.

A bare "3" on a nought to ten scale was handed to the reading model, which
reported low confidence, so the low-confidence rule never fired. A client who
asked "what day is today?" got the authored follow-up back, word for word,
twice. And a clear "not really" to "anything else?" was asked again. Each is
an engine decision, so each is asserted here against the offline provider.
"""

from __future__ import annotations

import pytest

from cadence.orchestration import session_runner
from conftest import INTAKE_ANSWERS


def _to_step(engine, episode_id: str, step_id: str):
    """Drive the intake up to `step_id` and return the turn that asks it."""
    result = engine.start(episode_id)
    for _ in range(20):
        if result.step_id == step_id or result.finished or not result.awaiting_client:
            return result
        result = engine.respond(result.session_run_id, INTAKE_ANSWERS[result.step_id])
    raise AssertionError(f"never reached {step_id}")


class TestScaleAnswers:
    @pytest.mark.parametrize("answer", ["3", "3.", "a 3", "About a 3, if I am honest.", "3 out of 10", "3/10"])
    def test_a_bare_number_is_read_by_the_engine_not_a_model(self, engine, monkeypatch, answer) -> None:
        enrolment = engine.enrol()
        result = _to_step(engine, enrolment.episode_id, "rate-confidence")
        assert result.step_id == "rate-confidence"

        def refuse(*args, **kwargs):
            raise AssertionError("the extractor was asked to read a plain number")

        with monkeypatch.context() as m:
            m.setattr(engine.router, "structured", refuse)
            result = engine.respond(result.session_run_id, answer)

        assert not any("below the rule floor" in n for n in result.notes)
        confidence = [o for o in engine.store.observations(enrolment.client_id, kinds=["confidence"])]
        assert confidence and confidence[-1].numeric == 3.0
        assert confidence[-1].confidence == 1.0

    def test_a_number_read_by_the_engine_drives_the_rules(self, engine) -> None:
        enrolment = engine.enrol()
        result = _to_step(engine, enrolment.episode_id, "rate-confidence")
        result = engine.respond(result.session_run_id, "3")
        while result.awaiting_client and not result.finished:
            result = engine.respond(result.session_run_id, INTAKE_ANSWERS.get(result.step_id, "Yes."))
        assert result.finished
        assert "r20-low-confidence" in result.rules_fired
        assert engine.store.client(enrolment.client_id).flags.get("low_confidence_support") is True

    @pytest.mark.parametrize("answer", ["4 or 5", "somewhere between 3 and 6", "not sure, maybe low"])
    def test_anything_less_plain_still_goes_to_the_reader(self, engine, monkeypatch, answer) -> None:
        enrolment = engine.enrol()
        result = _to_step(engine, enrolment.episode_id, "rate-confidence")
        called = []
        real = engine.router.structured

        def spy(*args, **kwargs):
            called.append(True)
            return real(*args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(engine.router, "structured", spy)
            engine.respond(result.session_run_id, answer)
        assert called


class TestFollowUpsListen:
    def test_a_follow_up_hears_what_the_client_just_said(self, engine, monkeypatch) -> None:
        seen: list[dict] = []
        real = session_runner.render

        def spy(name, **kwargs):
            if name == "counsellor.j2":
                seen.append(kwargs)
            return real(name, **kwargs)

        monkeypatch.setattr(session_runner, "render", spy)
        enrolment = engine.enrol()
        result = engine.start(enrolment.episode_id)
        assert result.step_id == "q-current-use"

        result = engine.respond(result.session_run_id, "what day is today?")
        assert result.step_id == "q-current-use"
        assert "follow-up 1 of 2" in result.notes
        task = seen[-1]["task"]
        assert "what day is today?" in task
        assert "one plain sentence" in task

        result = engine.respond(result.session_run_id, "a monday")
        assert "follow-up 2 of 2" in result.notes
        assert "different way" in seen[-1]["task"]


class TestADeclineIsAnAnswer:
    @pytest.mark.parametrize("no", ["no", "No.", "not really", "nothing else", "no thanks", "that's it", "nope"])
    def test_a_clear_no_to_a_follow_up_moves_the_session_on(self, engine, no) -> None:
        enrolment = engine.enrol()
        result = _to_step(engine, enrolment.episode_id, "q-reasons")
        result = engine.respond(result.session_run_id, "I am worried about my health.")
        assert result.step_id == "q-reasons" and "follow-up 1 of 2" in result.notes

        result = engine.respond(result.session_run_id, no)
        assert result.step_id == "q-previous-attempts"
        assert not any("follow-up 2 of 2" in n for n in result.notes)
        assert any("declined" in n for n in result.notes)
        goals = [o.value for o in engine.store.observations(enrolment.client_id, kinds=["goal"])]
        assert no.strip(" .").lower() not in [str(g).lower() for g in goals]

    def test_a_short_real_answer_is_not_mistaken_for_a_decline(self, engine) -> None:
        enrolment = engine.enrol()
        result = _to_step(engine, enrolment.episode_id, "q-reasons")
        result = engine.respond(result.session_run_id, "I am worried about my health.")
        result = engine.respond(result.session_run_id, "And the money.")
        goals = [str(o.value) for o in engine.store.observations(enrolment.client_id, kinds=["goal"])]
        assert any("money" in g.lower() for g in goals)
        assert not any("declined" in n for n in result.notes)
