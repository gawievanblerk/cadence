"""Deterministic evaluation of clinical rules.

No model is consulted here, ever. Whether a client is routed into lapse
recovery is decided by arithmetic over recorded observations, so the decision
is reproducible, explainable to a regulator, and identical whichever provider
happens to be generating the prose that day.

The engine's output is a list of effects plus a decision record for every rule
it considered - including the ones that did not fire, because "why did it not
escalate?" is the question that actually gets asked in review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cadence.content.models import Rule, RuleEffect, RuleSet
from cadence.domain.models import DecisionRecord
from cadence.rules.expressions import ExpressionError, evaluate


@dataclass
class RuleOutcome:
    effects: list[tuple[str, RuleEffect]] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def fired_ids(self) -> list[str]:
        return [rule_id for rule_id, _ in self.effects]

    def merged(self) -> RuleEffect:
        """Collapse effects into one, highest-priority-first.

        Later (lower priority) rules cannot undo an escalation raised by an
        earlier one. Escalation is a ratchet on purpose.
        """
        merged = RuleEffect()
        for _, eff in self.effects:
            merged.insert_session = merged.insert_session or eff.insert_session
            merged.remove_session = merged.remove_session or eff.remove_session
            merged.set_pathway = merged.set_pathway or eff.set_pathway
            merged.message_ref = merged.message_ref or eff.message_ref
            merged.set_flag = {**eff.set_flag, **merged.set_flag}
            merged.notify = list(dict.fromkeys([*merged.notify, *eff.notify]))
            merged.escalate = merged.escalate or eff.escalate
        return merged


class RuleEngine:
    """Evaluates a RuleSet against a projected context."""

    def __init__(self, ruleset: RuleSet):
        self.ruleset = ruleset

    def evaluate(
        self,
        context: dict[str, Any],
        *,
        client_id: str,
        episode_id: str,
        session_run_id: str | None = None,
        already_fired: set[str] | None = None,
    ) -> RuleOutcome:
        already_fired = already_fired or set()
        ctx = {**context, "thresholds": dict(self.ruleset.thresholds)}
        outcome = RuleOutcome()

        for rule in sorted(self.ruleset.rules, key=lambda r: r.priority):
            if rule.once and rule.id in already_fired:
                continue
            fired, error = self._test(rule, ctx)
            if error:
                outcome.errors.append(f"{rule.id}: {error}")
            outcome.decisions.append(
                DecisionRecord(
                    client_id=client_id,
                    episode_id=episode_id,
                    session_run_id=session_run_id,
                    rule_id=rule.id,
                    expression=rule.when,
                    inputs=_referenced(ctx, rule.when),
                    fired=fired,
                    effects=rule.then.model_dump(exclude_defaults=True) if fired else {},
                )
            )
            if fired:
                outcome.effects.append((rule.id, rule.then))
        return outcome

    def _test(self, rule: Rule, ctx: dict[str, Any]) -> tuple[bool, str | None]:
        try:
            return bool(evaluate(rule.when, ctx)), None
        except ExpressionError as exc:
            # A broken rule must never silently behave as if it were false in a
            # way nobody notices: it does not fire, and it is reported.
            return False, str(exc)


def _referenced(ctx: dict[str, Any], expression: str) -> dict[str, Any]:
    """Snapshot just the context values an expression mentions.

    Storing the whole context on every decision record would be both enormous
    and a privacy problem. Storing only what the rule read is enough to
    reproduce the decision.
    """
    import ast

    captured: dict[str, Any] = {}
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return captured

    def path(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = path(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)):
            dotted = path(node)
            if not dotted:
                continue
            cursor: Any = ctx
            for part in dotted.split("."):
                if isinstance(cursor, dict) and part in cursor:
                    cursor = cursor[part]
                else:
                    cursor = None
                    break
            if cursor is not None and not isinstance(cursor, dict):
                captured[dotted] = cursor
    return captured
