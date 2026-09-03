"""Version control for treatment content.

Two things need to be true at once, and they pull against each other:

  * The clinical team must be able to change the program whenever they like.
  * A client who started session 3 under version 2.1 must finish their episode
    under version 2.1, not under whatever was published on Tuesday.

The registry resolves that by loading each published version once, caching it
by version id, and handing episodes a pinned bundle. A new publication is
visible to new episodes immediately and to in-flight episodes never, unless a
clinician explicitly migrates them.
"""

from __future__ import annotations

import threading
from pathlib import Path

from cadence.content.loader import ContentBundle, ContentError, load_bundle


class ContentRegistry:
    def __init__(self, root: str | Path, default_version: str = "@latest"):
        self.root = Path(root)
        self.default_version = default_version
        self._cache: dict[str, ContentBundle] = {}
        self._lock = threading.Lock()

    def get(self, version: str | None = None) -> ContentBundle:
        requested = version or self.default_version
        with self._lock:
            if requested in self._cache:
                return self._cache[requested]
            bundle = load_bundle(self.root, requested)
            self._cache[requested] = bundle
            # Also cache under the concrete version so "@latest" and "2.1" share
            # one object and one content hash.
            self._cache.setdefault(bundle.version_id, bundle)
            return bundle

    def reload(self) -> None:
        """Drop the cache. Used by the admin publish endpoint and by `--reload`."""
        with self._lock:
            self._cache.clear()

    def available(self) -> list[str]:
        bundle = self.get()
        return [v.version for v in bundle.program.versions]

    def validate(self, version: str | None = None) -> list[str]:
        """Return validation problems without raising. Used by the admin preview."""
        try:
            load_bundle(self.root, version or self.default_version)
        except ContentError as exc:
            return exc.problems
        return []

    def diff(self, left: str, right: str) -> dict[str, list[str]]:
        """A clinician-readable diff between two published versions.

        Deliberately coarse: sessions and rules added, removed, or changed. The
        line-level diff is git's job and the clinical team should not have to
        read one to sign off a release.
        """
        a, b = self.get(left), self.get(right)
        report: dict[str, list[str]] = {"added": [], "removed": [], "changed": []}

        for sid in sorted(set(b.sessions) - set(a.sessions)):
            report["added"].append(f"session {sid} ({b.sessions[sid].title})")
        for sid in sorted(set(a.sessions) - set(b.sessions)):
            report["removed"].append(f"session {sid} ({a.sessions[sid].title})")
        for sid in sorted(set(a.sessions) & set(b.sessions)):
            if a.sessions[sid].model_dump() != b.sessions[sid].model_dump():
                bump = ""
                if a.sessions[sid].version == b.sessions[sid].version:
                    bump = "  [warning: content changed but `version` was not incremented]"
                report["changed"].append(f"session {sid}{bump}")

        a_rules = {r.id: r for r in a.ruleset.rules}
        b_rules = {r.id: r for r in b.ruleset.rules}
        for rid in sorted(set(b_rules) - set(a_rules)):
            report["added"].append(f"rule {rid}")
        for rid in sorted(set(a_rules) - set(b_rules)):
            report["removed"].append(f"rule {rid}")
        for rid in sorted(set(a_rules) & set(b_rules)):
            if a_rules[rid].model_dump() != b_rules[rid].model_dump():
                report["changed"].append(f"rule {rid}")

        for key in sorted(set(a.ruleset.thresholds) | set(b.ruleset.thresholds)):
            old = a.ruleset.thresholds.get(key)
            new = b.ruleset.thresholds.get(key)
            if old != new:
                report["changed"].append(f"threshold {key}: {old} -> {new}")

        return report
