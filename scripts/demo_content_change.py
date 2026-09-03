"""Changing the treatment without changing the software.

The clinical team owns the programme. This script makes three claims and then
demonstrates each of them:

  1. A published version can be diffed in clinical terms, not in diff-speak.
  2. Editing a threshold in YAML changes what the system does to a client, with
     no code change, no deployment of new logic, and no developer.
  3. A bad edit is caught before it can reach a client, by a validator that
     checks references and expressions rather than just YAML syntax.

Nothing here touches Python. Every change is a text edit to a file the clinical
team owns.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from _demo_support import ROOT, backdate, note, rule, run_session

from cadence.config import Settings
from cadence.content.loader import ContentError, load_bundle
from cadence.content.registry import ContentRegistry
from cadence.orchestration import TreatmentEngine

ANSWERS = {
    "q-current-use": "Twenty a day, cigarettes, about fifteen years.",
    "q-first-of-day": "Ten minutes.",
    "q-reasons": "My daughter asked me to; the money; the stairs at work.",
    "q-previous-attempts": "Patches in 2019.",
    "rate-confidence": "About a 3.",
    "probe-low-confidence": "Getting through the first week.",
    "q-quit-date": "In 2 weeks.",
}


def scenario(content_path: Path, label: str) -> dict[str, object]:
    """One intake session, run against a given content directory."""
    db = ROOT / "var" / f"content_{label}.db"
    if db.exists():
        db.unlink()
    engine = TreatmentEngine(
        Settings.from_env(
            {"CADENCE_DATABASE_URL": f"sqlite:///{db}", "CADENCE_CONTENT_PATH": str(content_path)}
        )
    )
    enrolment = engine.enrol(identity={"name": "Threshold Client"})
    run_session(engine, enrolment.episode_id, ANSWERS, quiet=True)
    backdate(engine, enrolment.episode_id, 2)
    progress = engine.progress(enrolment.episode_id)
    return {
        "confidence recorded": progress["confidence"],
        "sessions queued by rules": progress["queued"],
        "flags set": progress["flags"],
        "next session": engine.next_session(enrolment.episode_id),
    }


def main() -> None:
    original = ROOT / "content"
    working = ROOT / "var" / "content-edited"

    # ---- 1. version diff --------------------------------------------------
    rule("What changed between published versions")
    registry = ContentRegistry(original)
    diff = registry.diff("1.0", "1.1")
    for heading, entries in diff.items():
        for entry in entries:
            print(f"  {heading:<9} {entry}")
    note("this is what a clinical lead signs off, not a git diff")

    # ---- 2. before --------------------------------------------------------
    rule("Before: a client rating their confidence at 3")
    before = scenario(original, "before")
    print(json.dumps(before, indent=2))
    note("thresholds.low_confidence is 4, so the rule fires and the confidence "
         "session is queued")

    # ---- 3. the edit ------------------------------------------------------
    rule("The clinical team edits one number")
    if working.exists():
        shutil.rmtree(working)
    shutil.copytree(original, working)

    rules_file = working / "rules" / "pathways.yaml"
    data = yaml.safe_load(rules_file.read_text(encoding="utf-8"))
    print(f"  thresholds.low_confidence: {data['thresholds']['low_confidence']} -> 2")
    data["thresholds"]["low_confidence"] = 2
    rules_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    problems = ContentRegistry(working).validate()
    print(f"  validation: {'passed' if not problems else problems}")

    rule("After: the same client, the same answers")
    after = scenario(working, "after")
    print(json.dumps(after, indent=2))
    note("the confidence session is no longer queued. No Python was changed, "
         "no service was redeployed, and the change is one line in a file the "
         "clinical team owns")

    # ---- 4. a bad edit ----------------------------------------------------
    rule("And when the edit is wrong")
    broken = yaml.safe_load(rules_file.read_text(encoding="utf-8"))
    broken["rules"].append(
        {
            "id": "r99-typo",
            "description": "Deliberately broken, to show what the validator catches.",
            "priority": 50,
            "when": "observations.confidance <= thresholds.low_confidance",
            "then": {"insert_session": "s92-does-not-exist"},
        }
    )
    rules_file.write_text(yaml.safe_dump(broken, sort_keys=False, allow_unicode=True), encoding="utf-8")

    try:
        load_bundle(working, "@latest")
        print("  the validator did not catch it, which would be a bug in the validator")
    except ContentError as exc:
        for problem in exc.problems:
            print(f"  rejected: {problem}")
    note("caught at publish time: a misspelled observation kind, a misspelled "
         "threshold, and a session that does not exist. None of these are YAML "
         "syntax errors, and none of them would have surfaced until a client "
         "hit that rule months later")


if __name__ == "__main__":
    main()
