from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from cadence.content.loader import ContentError, load_bundle
from cadence.content.registry import ContentRegistry
from conftest import CONTENT


def test_published_content_is_valid(bundle) -> None:
    assert bundle.version_id == "1.1"
    assert bundle.content_hash
    assert "s01-intake" in bundle.sessions
    assert bundle.ruleset.thresholds["low_confidence"] == 4


def test_versions_are_isolated_from_each_other() -> None:
    old = load_bundle(CONTENT, "1.0")
    new = load_bundle(CONTENT, "1.1")
    assert "s91-confidence-build" not in old.sessions
    assert "s91-confidence-build" in new.sessions
    assert old.ruleset.thresholds["low_confidence"] != new.ruleset.thresholds["low_confidence"]
    assert old.content_hash != new.content_hash


def test_registry_caches_by_resolved_version(registry: ContentRegistry) -> None:
    assert registry.get("@latest") is registry.get("1.1")


def test_diff_is_readable(registry: ContentRegistry) -> None:
    report = registry.diff("1.0", "1.1")
    assert any("s91-confidence-build" in entry for entry in report["added"])
    assert any("low_confidence" in entry for entry in report["changed"])


@pytest.fixture()
def editable(tmp_path: Path) -> Path:
    target = tmp_path / "content"
    shutil.copytree(CONTENT, target)
    return target


def _rewrite_rules(root: Path, mutate) -> None:
    path = root / "rules" / "pathways.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


class TestValidationCatchesTheSilentMistakes:
    """Each of these parses as valid YAML and would fail silently at runtime."""

    def test_misspelled_observation_kind(self, editable: Path) -> None:
        _rewrite_rules(
            editable,
            lambda d: d["rules"].append(
                {
                    "id": "r-typo",
                    "description": "x",
                    "when": "observations.confidance <= 3",
                    "then": {"escalate": True},
                }
            ),
        )
        with pytest.raises(ContentError) as exc:
            load_bundle(editable)
        assert any("confidance" in p for p in exc.value.problems)

    def test_misspelled_threshold(self, editable: Path) -> None:
        _rewrite_rules(
            editable,
            lambda d: d["rules"].append(
                {
                    "id": "r-typo",
                    "description": "x",
                    "when": "observations.confidence <= thresholds.low_confidance",
                    "then": {"escalate": True},
                }
            ),
        )
        with pytest.raises(ContentError) as exc:
            load_bundle(editable)
        assert any("low_confidance" in p for p in exc.value.problems)

    def test_rule_inserting_a_session_that_does_not_exist(self, editable: Path) -> None:
        _rewrite_rules(
            editable,
            lambda d: d["rules"].append(
                {
                    "id": "r-ghost",
                    "description": "x",
                    "when": "observations.confidence <= 3",
                    "then": {"insert_session": "s99-nonexistent"},
                }
            ),
        )
        with pytest.raises(ContentError) as exc:
            load_bundle(editable)
        assert any("s99-nonexistent" in p for p in exc.value.problems)

    def test_step_referencing_a_missing_knowledge_document(self, editable: Path) -> None:
        path = editable / "sessions" / "s02-triggers.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for step in data["steps"]:
            if step["id"] == "teach-urge-surfing":
                step["knowledge_ref"] = "kb-does-not-exist"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(ContentError) as exc:
            load_bundle(editable)
        assert any("kb-does-not-exist" in p for p in exc.value.problems)

    def test_branch_to_an_unknown_step(self, editable: Path) -> None:
        path = editable / "sessions" / "s05-week-one-review.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for step in data["steps"]:
            if step["id"] == "branch-on-use":
                step["branches"][0]["goto"] = "q-nowhere"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(ContentError) as exc:
            load_bundle(editable)
        assert any("q-nowhere" in p for p in exc.value.problems)


def test_a_content_edit_changes_behaviour_without_touching_code(editable: Path) -> None:
    _rewrite_rules(editable, lambda d: d["thresholds"].__setitem__("low_confidence", 9))
    edited = load_bundle(editable)
    assert edited.ruleset.thresholds["low_confidence"] == 9
    assert edited.content_hash != load_bundle(CONTENT).content_hash


def test_locale_overlay_falls_back_to_source(bundle) -> None:
    known = "Nought to ten, today: how confident are you?"
    assert bundle.localise(known, "af") != known
    assert bundle.localise("never translated", "af") == "never translated"
