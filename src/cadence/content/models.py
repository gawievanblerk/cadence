from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cadence.domain.enums import StepKind

# ---------------------------------------------------------------------------
# These models are the contract between the clinical team and the engine.
#
# Everything a clinician can author is described here, and nothing else is.
# The admin editor is generated from these models, the YAML files are validated
# against them on every save, and the engine reads nothing that is not on them.
# ---------------------------------------------------------------------------


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptureField(Strict):
    """A piece of structured information a step is expected to collect."""

    name: str
    kind: str = Field(
        description="One of: string, text, integer, number, boolean, date, list[string]"
    )
    description: str
    required: bool = True
    range: list[float] | None = None
    options: list[str] | None = None
    min_items: int | None = None
    maps_to: str | None = Field(
        default=None,
        description="ObservationKind this field is stored as. Defaults to `name`.",
    )

    @model_validator(mode="after")
    def _check(self) -> CaptureField:
        allowed = {
            "string",
            "text",
            "integer",
            "number",
            "boolean",
            "date",
            "list[string]",
        }
        if self.kind not in allowed:
            raise ValueError(f"capture kind {self.kind!r} must be one of {sorted(allowed)}")
        if self.range is not None and len(self.range) != 2:
            raise ValueError("range must be [min, max]")
        return self


class Followup(Strict):
    """Adaptive probing, bounded.

    `max` is a hard stop enforced by the orchestrator, not a suggestion in a
    prompt. A model cannot talk its way past it.
    """

    max: int = 2
    when: str = Field(description="Expression; probe again only while this is true")
    instruction: str = Field(
        description="Written for the model: how to probe. The client never sees this."
    )
    text: str | None = Field(
        default=None,
        description=(
            "The follow-up question in the clinical team's own words. This is what the "
            "client gets if generation fails or is unavailable; the instruction is not."
        ),
    )


class Step(Strict):
    id: str
    kind: StepKind
    # Content the clinician writes. `say`/`ask` text is authoritative: the model
    # is instructed to deliver its meaning, not to improve on it.
    text: str | None = None
    options: list[str] | None = None
    capture: list[CaptureField] = Field(default_factory=list)
    followup: Followup | None = None
    knowledge_ref: str | None = None
    exercise_ref: str | None = None
    when: str | None = Field(default=None, description="Skip the step unless this is true")
    branches: list[Branch] = Field(default_factory=list)
    style_hint: str | None = Field(
        default=None, description="Tone guidance for this step only, e.g. 'brief and warm'"
    )

    @model_validator(mode="after")
    def _check(self) -> Step:
        if self.kind in (StepKind.SAY, StepKind.ASK, StepKind.SCALE, StepKind.CHOICE) and not self.text:
            raise ValueError(f"step {self.id!r} of kind {self.kind.value} requires `text`")
        if self.kind is StepKind.CHOICE and not self.options:
            raise ValueError(f"step {self.id!r} of kind choice requires `options`")
        if self.kind is StepKind.TEACH and not self.knowledge_ref:
            raise ValueError(f"step {self.id!r} of kind teach requires `knowledge_ref`")
        if self.kind is StepKind.ASSIGN and not self.exercise_ref:
            raise ValueError(f"step {self.id!r} of kind assign requires `exercise_ref`")
        if self.kind is StepKind.BRANCH and not self.branches:
            raise ValueError(f"step {self.id!r} of kind branch requires `branches`")
        if self.kind is StepKind.SCALE:
            scale_fields = [c for c in self.capture if c.range]
            if not scale_fields:
                raise ValueError(f"step {self.id!r} of kind scale requires a capture field with a range")
        return self


class Branch(Strict):
    when: str
    goto: str | None = None
    end_session: bool = False


class Mandatory(Strict):
    """A clinical rule the session may not violate.

    Two shapes, and the difference matters:

    `requires` is checked by the engine against recorded observations after the
    session's steps are exhausted. It is a gate, not a prompt: if it is unmet
    the session cannot be marked complete and the orchestrator re-opens the
    steps that would satisfy it.

    `prohibition` is a constraint injected into every prompt in the session and
    additionally checked by a post-generation guard. It is belt and braces
    because a prompt alone is not a control.
    """

    id: str
    description: str
    requires: str | None = None
    prohibition: str | None = None
    satisfied_by: list[str] = Field(
        default_factory=list, description="Step ids to re-open when `requires` is unmet"
    )

    @model_validator(mode="after")
    def _check(self) -> Mandatory:
        if not self.requires and not self.prohibition:
            raise ValueError(f"mandatory {self.id!r} needs either `requires` or `prohibition`")
        return self


class Session(Strict):
    id: str
    version: int = 1
    title: str
    phase: str
    sequence: int = Field(description="Position in the standard program order")
    estimated_minutes: int = 20
    objectives: list[str] = Field(default_factory=list)
    opens_when: str | None = Field(
        default=None, description="Expression gating availability of this session"
    )
    recall: list[str] = Field(
        default_factory=list,
        description="Observation kinds this session needs from earlier sessions",
    )
    mandatory: list[Mandatory] = Field(default_factory=list)
    steps: list[Step]
    on_complete: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> Session:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id {step.id!r} in session {self.id!r}")
            seen.add(step.id)
        for step in self.steps:
            for br in step.branches:
                if br.goto and br.goto not in seen:
                    raise ValueError(
                        f"session {self.id!r} step {step.id!r} branches to unknown step {br.goto!r}"
                    )
        for m in self.mandatory:
            for sid in m.satisfied_by:
                if sid not in seen:
                    raise ValueError(
                        f"session {self.id!r} mandatory {m.id!r} names unknown step {sid!r}"
                    )
        return self


class RuleEffect(Strict):
    insert_session: str | None = None
    remove_session: str | None = None
    set_pathway: str | None = None
    set_flag: dict[str, Any] = Field(default_factory=dict)
    notify: list[str] = Field(default_factory=list)
    escalate: bool = False
    message_ref: str | None = None


class Rule(Strict):
    id: str
    description: str
    priority: int = 100
    when: str
    then: RuleEffect
    once: bool = True


class RuleSet(Strict):
    thresholds: dict[str, float] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list)


class KnowledgeDoc(Strict):
    id: str
    title: str
    tags: list[str] = Field(default_factory=list)
    audience: Literal["client", "clinician"] = "client"
    body: str


class Exercise(Strict):
    id: str
    title: str
    instructions: str
    duration_minutes: int | None = None
    attachment: str | None = None


class ProgramVersion(Strict):
    version: str
    status: Literal["draft", "published", "retired"] = "draft"
    published_at: str | None = None
    author: str | None = None
    notes: str | None = None
    sessions: list[str]


class Program(Strict):
    """The manifest. This is the file that makes a set of YAML files a program."""

    id: str
    title: str
    locales: list[str] = Field(default_factory=lambda: ["en"])
    default_locale: str = "en"
    persona: str = Field(description="Who the counsellor is, in the clinician's own words")
    global_prohibitions: list[str] = Field(default_factory=list)
    safety: dict[str, Any] = Field(default_factory=dict)
    versions: list[ProgramVersion]

    def resolve(self, requested: str) -> ProgramVersion:
        if requested == "@latest":
            published = [v for v in self.versions if v.status == "published"]
            if not published:
                raise ValueError(f"program {self.id!r} has no published version")
            return published[-1]
        for v in self.versions:
            if v.version == requested:
                return v
        raise ValueError(f"program {self.id!r} has no version {requested!r}")


Step.model_rebuild()
Session.model_rebuild()
