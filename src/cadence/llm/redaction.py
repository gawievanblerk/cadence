"""What leaves the perimeter, and in what state.

The platform processes health information about identifiable people, and the
single largest privacy surface is the text posted to a third-party inference
API. Three controls apply, in this order:

  1. Minimisation. Direct identifiers never enter a prompt in the first place:
     the context assembler works from the client's pseudonymous id and from
     typed observations, and `Client.identity` is not among the things it can
     reach. Redaction is therefore a backstop, not the primary control.
  2. Redaction. Free-text the client typed can still contain a name, a phone
     number or an address. It is masked on the way out.
  3. Egress policy. A provider not on the allowlist is refused before the
     request is built, so a misconfigured binding fails closed.

Every outbound call is recorded: which provider, which role, how many
characters, and what classes of identifier were masked. That record is the
evidence for a GDPR Article 30 processing register, and it is why the log
stores counts rather than the values it masked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Order matters. The more specific identifier patterns run first, because a
# 13-digit national ID also matches a loose phone pattern, and mislabelling it
# would understate what was actually sent.
#
# Over-masking is a real failure mode, not a safe default. A quit date turned
# into [PHONE] on the way to the extractor is corrupted clinical data, and it
# fails silently: the extraction simply returns null and the session records
# nothing. So the loose patterns carry a `verify` predicate, and a candidate
# that does not pass it is left alone.
_ISO_DATE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")


def _looks_like_a_phone_number(candidate: str) -> bool:
    digits = sum(c.isdigit() for c in candidate)
    if digits < 9 or digits > 15:
        return False
    return not _ISO_DATE.match(candidate.strip())


_PATTERNS: list[tuple[str, re.Pattern[str], Any]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), None),
    ("url", re.compile(r"https?://\S+"), None),
    ("sa_id", re.compile(r"(?<!\d)\d{13}(?!\d)"), None),
    (
        "card",
        re.compile(r"(?<!\d)(?:\d[ -]?){14,19}(?!\d)"),
        lambda c: 14 <= sum(x.isdigit() for x in c) <= 19,
    ),
    (
        "phone",
        re.compile(r"(?<![\w-])\+?\d[\d\s().-]{6,}\d(?![\w-])"),
        _looks_like_a_phone_number,
    ),
    ("postcode", re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"), None),
]


@dataclass
class RedactionReport:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.counts


class EgressPolicy:
    """Decides whether a provider may be sent client-derived text at all."""

    def __init__(self, allowed: frozenset[str] | set[str], redact: bool = True):
        self.allowed = frozenset(allowed)
        self.redact = redact

    def check(self, provider_id: str) -> None:
        if self.allowed and provider_id not in self.allowed:
            raise PermissionError(
                f"provider {provider_id!r} is not on the egress allowlist; "
                "no client-derived text will be sent to it"
            )

    def apply(self, text: str, *, known_names: list[str] | None = None) -> RedactionReport:
        if not self.redact:
            return RedactionReport(text)
        return redact(text, known_names=known_names)


def redact(text: str, *, known_names: list[str] | None = None) -> RedactionReport:
    """Mask direct identifiers, preserving the shape of the sentence.

    Masks are typed placeholders rather than a uniform block, because an
    extractor reading "I told [NAME] about it" can still record that a support
    person was told; reading "I told ███ about it" cannot.
    """
    counts: dict[str, int] = {}
    out = text

    for name in known_names or []:
        if len(name) < 3:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.I)
        out, n = pattern.subn("[NAME]", out)
        if n:
            counts["name"] = counts.get("name", 0) + n

    for label, pattern, verify in _PATTERNS:
        hits = 0

        def replace(match: re.Match[str], _label: str = label, _verify: Any = verify) -> str:
            nonlocal hits
            if _verify is not None and not _verify(match.group(0)):
                return match.group(0)
            hits += 1
            return f"[{_label.upper()}]"

        out = pattern.sub(replace, out)
        if hits:
            counts[label] = counts.get(label, 0) + hits

    return RedactionReport(out, counts)


@dataclass
class EgressRecord:
    """One line of the processing register."""

    provider: str
    model: str
    role: str
    characters: int
    masked: dict[str, int]
    purpose: str
    client_pseudonym: str
    at: str
