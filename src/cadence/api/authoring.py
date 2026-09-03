"""Content authoring endpoints.

The clinical team's editor talks to these. The important one is `save_session`,
which is deliberately paranoid: it writes, revalidates the whole programme, and
rolls the file back if the edit broke anything. A clinician can therefore be
wrong without being dangerous, which is the only basis on which handing them
write access is reasonable.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from cadence.content.loader import ContentError, load_bundle
from cadence.content.models import Session
from cadence.content.registry import ContentRegistry


class SaveResult(BaseModel):
    saved: bool
    problems: list[str] = []
    rolled_back: bool = False
    content_hash: str | None = None


class ContentWorkspace:
    """File-level operations on the treatment content directory."""

    def __init__(self, registry: ContentRegistry):
        self.registry = registry
        self.root = Path(registry.root)

    # -- reading -------------------------------------------------------------

    def outline(self, version: str | None = None) -> dict[str, Any]:
        bundle = self.registry.get(version)
        return {
            "program": bundle.program.title,
            "version": bundle.version_id,
            "status": bundle.version.status,
            "content_hash": bundle.content_hash,
            "sessions": [
                {
                    "id": s.id,
                    "title": s.title,
                    "phase": s.phase,
                    "sequence": s.sequence,
                    "version": s.version,
                    "steps": len(s.steps),
                    "mandatory": len(s.mandatory),
                    "inserted_only": s.sequence >= 900,
                    "objectives": s.objectives,
                }
                for s in bundle.ordered_sessions()
            ],
            "thresholds": bundle.ruleset.thresholds,
            "rules": [
                {
                    "id": r.id,
                    "description": r.description,
                    "priority": r.priority,
                    "when": r.when,
                    "then": r.then.model_dump(exclude_defaults=True),
                }
                for r in sorted(bundle.ruleset.rules, key=lambda r: r.priority)
            ],
            "knowledge": [
                {"id": d.id, "title": d.title, "tags": d.tags} for d in bundle.knowledge.values()
            ],
            "exercises": [{"id": e.id, "title": e.title} for e in bundle.exercises.values()],
        }

    def session_source(self, session_id: str) -> str:
        path = self._session_path(session_id)
        if not path.exists():
            raise FileNotFoundError(session_id)
        return path.read_text(encoding="utf-8")

    def rules_source(self, version: str | None = None) -> str:
        return self._rules_path(version).read_text(encoding="utf-8")

    # -- writing -------------------------------------------------------------

    def save_session(self, session_id: str, source: str) -> SaveResult:
        return self._guarded_write(self._session_path(session_id), source, model=Session)

    def save_rules(self, source: str, version: str | None = None) -> SaveResult:
        return self._guarded_write(self._rules_path(version), source)

    def _guarded_write(self, path: Path, source: str, model: type | None = None) -> SaveResult:
        """Write, revalidate the whole programme, and roll back if it broke.

        Validating the file on its own is not enough. A session can be
        individually well-formed and still break the programme by referring to a
        knowledge document that was deleted, or by a rule elsewhere pointing at a
        step this edit renamed. So the check is always whole-programme.
        """
        try:
            parsed = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            return SaveResult(saved=False, problems=[f"not valid YAML: {exc}"])

        if model is not None:
            try:
                model.model_validate(parsed)
            except ValidationError as exc:
                return SaveResult(
                    saved=False,
                    problems=[
                        f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                        for e in exc.errors()
                    ],
                )

        backup = path.read_bytes() if path.exists() else None
        path.write_text(source, encoding="utf-8")
        try:
            bundle = load_bundle(self.root, self.registry.default_version)
        except ContentError as exc:
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(backup)
            return SaveResult(saved=False, problems=exc.problems, rolled_back=True)

        self.registry.reload()
        return SaveResult(saved=True, content_hash=bundle.content_hash)

    # -- publication ---------------------------------------------------------

    def snapshot_version(self, new_version: str, notes: str, author: str) -> SaveResult:
        """Publish the working content as a new version.

        Copies the current rules file to a version-pinned one and appends the
        version to the manifest, so clients already in treatment keep reading
        the rules they started under.
        """
        manifest_path = self.root / "program.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if any(v["version"] == new_version for v in manifest["versions"]):
            return SaveResult(saved=False, problems=[f"version {new_version} already exists"])

        current = manifest["versions"][-1]
        pinned = self.root / "rules" / f"pathways@{current['version']}.yaml"
        if not pinned.exists():
            shutil.copy(self._rules_path(), pinned)

        manifest["versions"].append(
            {
                "version": new_version,
                "status": "published",
                "published_at": _today(),
                "author": author,
                "notes": notes,
                "sessions": list(current["sessions"]),
            }
        )
        backup = manifest_path.read_bytes()
        manifest_path.write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        try:
            load_bundle(self.root, new_version)
        except ContentError as exc:
            manifest_path.write_bytes(backup)
            return SaveResult(saved=False, problems=exc.problems, rolled_back=True)

        self.registry.reload()
        return SaveResult(saved=True)

    # -- paths ---------------------------------------------------------------

    def _session_path(self, session_id: str) -> Path:
        safe = Path(session_id).name  # no traversal out of the content directory
        return self.root / "sessions" / f"{safe}.yaml"

    def _rules_path(self, version: str | None = None) -> Path:
        bundle = self.registry.get(version)
        pinned = self.root / "rules" / f"pathways@{bundle.version_id}.yaml"
        return pinned if pinned.exists() else self.root / "rules" / "pathways.yaml"


def _today() -> str:
    from datetime import date

    return date.today().isoformat()
