"""Loading and validating a treatment program from files.

The clinical team's deliverable is a directory of YAML and Markdown. This
module turns that directory into an immutable, content-hashed bundle, or
refuses to and says exactly why.

Validation runs in three passes because the failure modes are different:

  1. Schema - is each file shaped like the thing it claims to be?
  2. Reference - does every knowledge_ref, exercise_ref and goto resolve?
  3. Expression - will every `when` / `requires` / `opens_when` evaluate, and
                  does it only refer to names the engine actually supplies?

Pass 3 is the one that matters most in practice. A typo in a threshold name is
invisible in review and would otherwise show up months later as a pathway that
quietly never triggers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from cadence.content.models import (
    Exercise,
    KnowledgeDoc,
    Program,
    ProgramVersion,
    RuleSet,
    Session,
)
from cadence.domain.enums import ObservationKind
from cadence.rules.expressions import static_check

# The complete set of root names an authored expression may use. This list is
# the contract; the orchestrator's context projection must supply all of them.
EXPRESSION_ROOTS = {
    "observations",
    "history",
    "thresholds",
    "episode",
    "client",
    "session",
    "capture",
    "marks",
    "flags",
}


class ContentError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("treatment content failed validation:\n  - " + "\n  - ".join(problems))


@dataclass(frozen=True)
class ContentBundle:
    """One immutable, validated version of a treatment program."""

    program: Program
    version: ProgramVersion
    sessions: dict[str, Session]
    ruleset: RuleSet
    knowledge: dict[str, KnowledgeDoc]
    exercises: dict[str, Exercise]
    locales: dict[str, dict[str, str]] = field(default_factory=dict)
    content_hash: str = ""

    @property
    def version_id(self) -> str:
        return self.version.version

    def ordered_sessions(self) -> list[Session]:
        return sorted(self.sessions.values(), key=lambda s: s.sequence)

    def session(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError as exc:
            raise ContentError([f"session {session_id!r} is not part of version {self.version_id}"]) from exc

    def localise(self, text: str, locale: str) -> str:
        """Apply a locale overlay.

        Overlays are keyed by the English source string so a clinician can add a
        language without renumbering anything, and an untranslated string falls
        back to the source rather than to an error.
        """
        if locale == self.program.default_locale:
            return text
        return self.locales.get(locale, {}).get(text.strip(), text)


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def load_bundle(root: str | Path, version: str = "@latest") -> ContentBundle:
    root = Path(root)
    problems: list[str] = []
    touched: list[Path] = []

    manifest_path = root / "program.yaml"
    if not manifest_path.exists():
        raise ContentError([f"no program.yaml in {root}"])
    touched.append(manifest_path)

    try:
        program = Program.model_validate(_read_yaml(manifest_path))
    except ValidationError as exc:
        raise ContentError([f"program.yaml: {_fmt(exc)}"]) from exc

    try:
        resolved = program.resolve(version)
    except ValueError as exc:
        raise ContentError([str(exc)]) from exc

    # ---- pass 1: schema ---------------------------------------------------
    sessions: dict[str, Session] = {}
    for session_id in resolved.sessions:
        path = root / "sessions" / f"{session_id}.yaml"
        if not path.exists():
            problems.append(f"version {resolved.version} lists session {session_id!r} but {path.name} is missing")
            continue
        touched.append(path)
        try:
            session = Session.model_validate(_read_yaml(path))
        except ValidationError as exc:
            problems.append(f"{path.name}: {_fmt(exc)}")
            continue
        if session.id != session_id:
            problems.append(f"{path.name}: declares id {session.id!r} but is filed as {session_id!r}")
        sessions[session.id] = session

    # Rules are versioned alongside sessions. A version-specific file wins; the
    # shared file is the fallback, so a program that never forks its rules only
    # needs one.
    versioned = root / "rules" / f"pathways@{resolved.version}.yaml"
    rules_path = versioned if versioned.exists() else root / "rules" / "pathways.yaml"
    ruleset = RuleSet()
    if rules_path.exists():
        touched.append(rules_path)
        try:
            ruleset = RuleSet.model_validate(_read_yaml(rules_path) or {})
        except ValidationError as exc:
            problems.append(f"{rules_path.name}: {_fmt(exc)}")

    knowledge: dict[str, KnowledgeDoc] = {}
    for path in sorted((root / "knowledge").glob("*.yaml")):
        touched.append(path)
        try:
            doc = KnowledgeDoc.model_validate(_read_yaml(path))
            knowledge[doc.id] = doc
        except ValidationError as exc:
            problems.append(f"{path.name}: {_fmt(exc)}")

    exercises: dict[str, Exercise] = {}
    for path in sorted((root / "exercises").glob("*.yaml")):
        touched.append(path)
        try:
            ex = Exercise.model_validate(_read_yaml(path))
            exercises[ex.id] = ex
        except ValidationError as exc:
            problems.append(f"{path.name}: {_fmt(exc)}")

    locales: dict[str, dict[str, str]] = {}
    for path in sorted((root / "locales").glob("*.yaml")):
        touched.append(path)
        data = _read_yaml(path) or {}
        if not isinstance(data, dict):
            problems.append(f"{path.name}: expected a mapping of source string to translation")
            continue
        locales[path.stem] = {str(k).strip(): str(v) for k, v in data.items()}

    # ---- pass 2: references ----------------------------------------------
    for session in sessions.values():
        for step in session.steps:
            if step.knowledge_ref and step.knowledge_ref not in knowledge:
                problems.append(
                    f"{session.id}/{step.id}: knowledge_ref {step.knowledge_ref!r} does not exist"
                )
            if step.exercise_ref and step.exercise_ref not in exercises:
                problems.append(
                    f"{session.id}/{step.id}: exercise_ref {step.exercise_ref!r} does not exist"
                )
    for rule in ruleset.rules:
        target = rule.then.insert_session
        if target and target not in sessions:
            problems.append(
                f"rule {rule.id}: inserts session {target!r}, which is not in version {resolved.version}"
            )

    # ---- pass 3: expressions ---------------------------------------------
    roots = EXPRESSION_ROOTS
    for session in sessions.values():
        _check_expr(problems, session.opens_when, f"{session.id}.opens_when", roots)
        for m in session.mandatory:
            _check_expr(problems, m.requires, f"{session.id}/{m.id}.requires", roots)
        for step in session.steps:
            _check_expr(problems, step.when, f"{session.id}/{step.id}.when", roots)
            if step.followup:
                _check_expr(problems, step.followup.when, f"{session.id}/{step.id}.followup.when", roots)
            for i, br in enumerate(step.branches):
                _check_expr(problems, br.when, f"{session.id}/{step.id}.branches[{i}].when", roots)
    for rule in ruleset.rules:
        _check_expr(problems, rule.when, f"rule {rule.id}.when", roots)

    # Names that resolve to null instead of raising are the classic silent
    # failure: the expression parses, evaluates to false forever, and the rule
    # it guards simply never fires.
    known_kinds = {k.value for k in ObservationKind}
    defined_thresholds = set(ruleset.thresholds)

    def audit(expression: str | None, where: str) -> None:
        if not expression:
            return
        for name in _attributes_of(expression, "thresholds"):
            if name not in defined_thresholds:
                problems.append(f"{where}: threshold {name!r} is not defined in {rules_path.name}")
        for root in ("observations", "history"):
            for name in _attributes_of(expression, root):
                if name not in known_kinds:
                    problems.append(
                        f"{where}: {root}.{name} is not a recorded observation kind "
                        f"(valid kinds: {', '.join(sorted(known_kinds))})"
                    )
        for name in _attributes_of(expression, "episode"):
            if name not in EPISODE_FIELDS:
                problems.append(
                    f"{where}: episode.{name} does not exist "
                    f"(available: {', '.join(sorted(EPISODE_FIELDS))})"
                )

    for rule in ruleset.rules:
        audit(rule.when, f"rule {rule.id}")
    for session in sessions.values():
        audit(session.opens_when, f"{session.id}.opens_when")
        for m in session.mandatory:
            audit(m.requires, f"{session.id}/{m.id}.requires")
        for step in session.steps:
            audit(step.when, f"{session.id}/{step.id}.when")
            if step.followup:
                audit(step.followup.when, f"{session.id}/{step.id}.followup.when")
            for i, br in enumerate(step.branches):
                audit(br.when, f"{session.id}/{step.id}.branches[{i}].when")

    if problems:
        raise ContentError(problems)

    return ContentBundle(
        program=program,
        version=resolved,
        sessions=sessions,
        ruleset=ruleset,
        knowledge=knowledge,
        exercises=exercises,
        locales=locales,
        content_hash=_hash(touched),
    )


def _check_expr(problems: list[str], expr: str | None, where: str, roots: set[str]) -> None:
    if not expr:
        return
    for problem in static_check(expr, roots):
        problems.append(f"{where}: {problem}")


def _attributes_of(expression: str, root: str) -> list[str]:
    """Every `root.attr` mentioned in an expression.

    Used to catch the mistakes that YAML validation and syntax checking both
    miss: `observations.confidance`, `thresholds.low_confidance`. Both parse
    cleanly, both evaluate to null, and both would silently disable a treatment
    rule for as long as nobody noticed.
    """
    import ast

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return []
    return [
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == root
    ]


# The keys the orchestrator's projection actually puts on `episode`.
EPISODE_FIELDS = {
    "id",
    "program_version",
    "sessions_completed",
    "queued_sessions",
    "quit_date",
    "started_at",
    "last_session_at",
    "session_count",
}


def _fmt(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def export_schemas(destination: str | Path) -> list[Path]:
    """Emit JSON Schema for the authoring models.

    The admin editor and any external tooling are generated from these, so the
    clinician-facing form can never drift from what the engine will accept.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in (
        ("session", Session),
        ("ruleset", RuleSet),
        ("program", Program),
        ("knowledge", KnowledgeDoc),
        ("exercise", Exercise),
    ):
        path = destination / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written
