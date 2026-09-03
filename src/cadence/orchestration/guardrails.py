"""Enforcement that does not depend on the model behaving.

A prohibition in a system prompt is a request. Most models honour it most of
the time, and "most of the time" is not a control you can put in front of a
regulator. So every clinical constraint in this system is enforced at least
once outside the model:

  * `requires` mandatories are gates in the state machine. The session cannot
    be marked complete while one is unmet, whatever the conversation looked
    like.
  * `prohibition` mandatories are injected as instruction *and* checked against
    the generated text before it reaches the client.
  * Adaptive follow-ups have a hard maximum enforced by a counter, not by an
    instruction saying "at most three".
  * Safety triage runs on every client utterance, independent of the session
    flow, and can stop the programme mid-step.

The post-generation check below is deliberately conservative. It looks for the
specific categories the clinical team wrote prohibitions about, and when it
fires the turn is regenerated once with the violation named. If the second
attempt also fails, the authored text is delivered verbatim instead - a plain
session is an acceptable outcome; an unsafe one is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cadence.content.models import Program, Session

# Category patterns, kept here rather than in content because they encode what
# the *checker* can detect, not what the clinical team wants prohibited. The
# clinical prohibitions stay in YAML; these are the detectors that back a few
# of them up.
_DETECTORS: list[tuple[str, re.Pattern[str]]] = [
    (
        "dosage advice",
        re.compile(
            r"\b(increase|decrease|double|halve|reduce|up)\s+(your|the)\s+"
            r"(dose|dosage|patch|gum|milligram|mg)\b",
            re.I,
        ),
    ),
    (
        "medication recommendation",
        re.compile(
            r"\b(you should (start|take|try)|i (recommend|suggest) (you )?(take|start))\b.{0,40}"
            r"\b(varenicline|champix|bupropion|zyban|nicotine patch|patches|lozenge|nrt)\b",
            re.I,
        ),
    ),
    (
        "diagnosis",
        re.compile(r"\byou (have|are suffering from|are showing signs of)\s+(a\s+)?(depression|anxiety disorder|copd|emphysema)\b", re.I),
    ),
    (
        "medical reassurance",
        re.compile(r"\b(that('s| is) nothing to worry about|you('re| are) fine|it('s| is) harmless)\b", re.I),
    ),
]

# Backs the "no praise, no disappointment" prohibition in the week-one review.
_PRAISE = re.compile(
    r"\b(well done|congratulations|i'?m (so )?(proud|pleased|impressed)|"
    r"that'?s (amazing|fantastic|brilliant)|good (job|work)|"
    r"i'?m disappointed|you should be proud)\b",
    re.I,
)

_SELF_HARM = re.compile(
    r"\b(kill(ing)? myself|end(ing)? (it|my life)|suicid|"
    r"(do not|don'?t|didn'?t) want to be here|not want(ing)? to be here|"
    r"harm(ing)? myself|hurt(ing)? myself|no point (in )?going on|"
    r"better off without me)\b",
    re.I,
)
_ACUTE_MEDICAL = re.compile(
    r"\b(chest pain|can'?t breathe|cannot breathe|breathless (at rest|sitting)|"
    r"fainted|passed out|coughing (up )?blood)\b",
    re.I,
)


@dataclass
class GuardrailReport:
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def check_output(text: str, *, program: Program, session: Session) -> GuardrailReport:
    """Screen a generated counsellor turn before it reaches the client."""
    report = GuardrailReport()
    for label, pattern in _DETECTORS:
        if pattern.search(text):
            report.violations.append(f"{label}: the turn appears to give {label}")

    prohibitions = " ".join(m.prohibition or "" for m in session.mandatory).lower()
    if ("praise" in prohibitions or "congratulat" in prohibitions) and _PRAISE.search(text):
        report.violations.append(
            "praise or disappointment: this session prohibits both, and the turn contains one"
        )
    return report


@dataclass
class SafetySignal:
    concern: str | None = None
    quote: str | None = None
    certain: bool = False

    @property
    def triggered(self) -> bool:
        return self.concern is not None


def screen_input(text: str) -> SafetySignal:
    """A deterministic first pass over what the client just said.

    Runs before, and independently of, the model-based triage. Two reasons: it
    costs nothing and cannot fail open on a provider outage, and it means the
    highest-consequence path in the system does not depend on an inference call
    succeeding.
    """
    if _SELF_HARM.search(text):
        return SafetySignal("self_harm", _first_match(_SELF_HARM, text), certain=True)
    if _ACUTE_MEDICAL.search(text):
        return SafetySignal("acute_medical", _first_match(_ACUTE_MEDICAL, text), certain=True)
    return SafetySignal()


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    start = max(0, match.start() - 40)
    end = min(len(text), match.end() + 40)
    return text[start:end].strip()


class FollowupBudget:
    """A counter, not a prompt instruction.

    The distinction is the point: a model asked to probe "at most twice" will
    sometimes probe five times. A model whose third probe is never requested
    cannot.
    """

    def __init__(self, maximum: int):
        self.maximum = max(0, maximum)
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.maximum

    def spend(self) -> bool:
        if self.exhausted:
            return False
        self.used += 1
        return True
