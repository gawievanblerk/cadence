"""Prompt templates.

They live in files rather than in string literals for two reasons: they are
reviewed by people who are not reading Python, and keeping them provider-neutral
is easier to police when they are visibly one artefact rather than scattered
f-strings. No template here contains a vendor's name, a vendor's formatting
convention, or a vendor-specific instruction.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_DIR = Path(__file__).parent


@lru_cache(maxsize=1)
def environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )


def render(name: str, **values: object) -> str:
    return environment().get_template(name).render(**values).strip()
