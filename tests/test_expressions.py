from __future__ import annotations

import pytest

from cadence.rules.expressions import ExpressionError, OpenMapping, evaluate, static_check, truthy

CONTEXT = {
    "observations": {"confidence": 4, "nicotine_use": True, "trigger_situation": ["car", "coffee"]},
    "history": {"lapse": ["friday"], "goal": ["daughter", "money"]},
    "thresholds": {"low_confidence": 4},
    "episode": {"sessions_completed": ["s01-intake"], "quit_date": "2026-01-01"},
    "client": {"pathway": "standard"},
    "capture": OpenMapping({"answered": 2}),
    "flags": OpenMapping({}),
    "marks": OpenMapping({"s01-intake": "2026-01-01"}),
}


class TestSafety:
    """The language must be incapable of doing anything except returning a value."""

    @pytest.mark.parametrize(
        "expression",
        [
            "__import__('os').system('ls')",
            "(lambda: 1)()",
            "[x for x in range(3)]",
            "open('/etc/passwd').read()",
            "observations.__class__",
        ],
    )
    def test_hostile_expressions_are_rejected(self, expression: str) -> None:
        with pytest.raises(ExpressionError):
            evaluate(expression, CONTEXT)

    def test_attribute_access_on_real_objects_is_impossible(self) -> None:
        with pytest.raises(ExpressionError):
            evaluate("client.pathway.upper", CONTEXT)

    def test_unknown_function_is_rejected(self) -> None:
        with pytest.raises(ExpressionError):
            evaluate("exec('1')", CONTEXT)


class TestSemantics:
    def test_threshold_comparison(self) -> None:
        assert evaluate("observations.confidence <= thresholds.low_confidence", CONTEXT) is True

    def test_membership(self) -> None:
        assert evaluate("'s01-intake' in episode.sessions_completed", CONTEXT) is True
        assert evaluate("'s02-triggers' in episode.sessions_completed", CONTEXT) is False

    def test_counting(self) -> None:
        assert evaluate("count(observations.trigger_situation) >= 2", CONTEXT) is True
        assert evaluate("count(history.lapse) >= 3", CONTEXT) is False

    def test_defined(self) -> None:
        assert evaluate("defined(observations.confidence)", CONTEXT) is True
        assert evaluate("defined(capture.nothing_here)", CONTEXT) is False

    def test_missing_value_never_satisfies_a_comparison(self) -> None:
        """A rule must not fire because an observation is absent."""
        context = {**CONTEXT, "observations": {**CONTEXT["observations"], "confidence": None}}
        assert evaluate("observations.confidence <= thresholds.low_confidence", context) is False

    def test_open_namespaces_read_missing_keys_as_null(self) -> None:
        assert evaluate("capture.not_collected_yet", CONTEXT) is None

    def test_closed_namespaces_raise_on_a_typo(self) -> None:
        with pytest.raises(ExpressionError):
            evaluate("observations.confidenc", CONTEXT)

    def test_truthy_default(self) -> None:
        assert truthy(None, CONTEXT) is True
        assert truthy("observations.nicotine_use == True", CONTEXT) is True


class TestStaticCheck:
    def test_accepts_valid_expression(self) -> None:
        assert static_check("observations.confidence <= thresholds.low_confidence", {"observations", "thresholds"}) == []

    def test_reports_unknown_root(self) -> None:
        problems = static_check("patient.confidence > 3", {"observations"})
        assert any("unknown name 'patient'" in p for p in problems)

    def test_reports_unknown_function(self) -> None:
        problems = static_check("frobnicate(observations.confidence)", {"observations"})
        assert any("unknown function" in p for p in problems)

    def test_reports_syntax_error(self) -> None:
        assert static_check("observations.confidence <=", {"observations"})
