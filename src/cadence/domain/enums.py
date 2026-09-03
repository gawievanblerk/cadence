from __future__ import annotations

from enum import Enum


class StepKind(str, Enum):
    """The kinds of step a clinician can put in a session.

    Adding a kind is a code change; using one is not. The set is kept small on
    purpose - every kind is a distinct piece of orchestration behaviour, not a
    presentational variant.
    """

    SAY = "say"
    """Deliver content. The model rephrases; it does not invent."""

    ASK = "ask"
    """Ask an open question and extract structured observations from the reply."""

    SCALE = "scale"
    """Ask for a rating on a fixed numeric range. Validated deterministically."""

    CHOICE = "choice"
    """Ask the client to pick from a fixed set of options."""

    TEACH = "teach"
    """Explain a technique, grounded in a retrieved knowledge document."""

    REFLECT = "reflect"
    """Play back what earlier sessions recorded, to be confirmed or corrected."""

    ASSIGN = "assign"
    """Assign an exercise or supporting document as between-session work."""

    BRANCH = "branch"
    """Evaluate rules and jump. No model call at all."""


class SessionState(str, Enum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class ObservationKind(str, Enum):
    """Clinically meaningful facts extracted from conversation.

    These are the units of structured memory. They are typed, timestamped, and
    attributable to the session and step that produced them, which is what
    makes later recall auditable rather than vibes-based.
    """

    TRIGGER_SITUATION = "trigger_situation"
    CRAVING_INTENSITY = "craving_intensity"
    CONFIDENCE = "confidence"
    MOTIVATION = "motivation"
    NICOTINE_USE = "nicotine_use"
    LAPSE = "lapse"
    TECHNIQUE_USED = "technique_used"
    TECHNIQUE_EFFECTIVENESS = "technique_effectiveness"
    GOAL = "goal"
    COMMITMENT = "commitment"
    BARRIER = "barrier"
    LIFE_CHANGE = "life_change"
    SUPPORT_PERSON = "support_person"
    QUIT_DATE = "quit_date"
    SAFETY_CONCERN = "safety_concern"
    FREE_NOTE = "free_note"


class Pathway(str, Enum):
    STANDARD = "standard"
    INTENSIVE = "intensive"
    LAPSE_RECOVERY = "lapse_recovery"
    MAINTENANCE = "maintenance"


class RiskLevel(str, Enum):
    NONE = "none"
    MONITOR = "monitor"
    ESCALATE = "escalate"
