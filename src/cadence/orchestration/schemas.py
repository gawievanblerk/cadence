"""Turning a clinician's `capture:` block into a JSON Schema.

The clinical team writes what they want collected in plain terms. The engine
turns that into a schema, hands it to whichever provider is bound to the
extractor role, and validates what comes back. Nobody writes a schema by hand,
and the schema cannot drift from the content, because it is derived from it.

Every field is nullable. "The client did not say" must be representable, or the
extractor will invent something to satisfy a required field, and an invented
confidence rating is worse than a missing one.
"""

from __future__ import annotations

from typing import Any

from cadence.content.models import CaptureField, Step

_JSON_TYPES: dict[str, dict[str, Any]] = {
    "string": {"type": "string"},
    "text": {"type": "string"},
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "list[string]": {"type": "array", "items": {"type": "string"}},
}


def field_schema(field: CaptureField) -> dict[str, Any]:
    schema = dict(_JSON_TYPES[field.kind])
    schema["description"] = field.description.strip()

    if field.range and field.kind in ("integer", "number"):
        schema["minimum"], schema["maximum"] = field.range
    if field.options:
        schema["enum"] = list(field.options)
    if field.min_items and schema.get("type") == "array":
        schema["minItems"] = field.min_items

    base_type = schema["type"]
    schema["type"] = [base_type, "null"]
    return schema


def step_schema(step: Step) -> dict[str, Any]:
    """The extraction schema for one step.

    `_confidence` and `_verbatim` are engine fields rather than clinical ones.
    The first gates whether an observation may drive a rule; the second keeps
    the client's own phrasing for later playback, because reflecting a
    paraphrase back to someone is how you lose their trust in session four.
    """
    properties = {f.name: field_schema(f) for f in step.capture}
    properties["_confidence"] = {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
        "description": (
            "How confident you are that these values are what the client actually "
            "said, from 0 to 1. Use a value below 0.6 if you are inferring rather "
            "than reading."
        ),
    }
    properties["_verbatim"] = {
        "type": ["string", "null"],
        "description": "The client's own words for the most important thing they just said.",
    }
    return {
        "type": "object",
        "title": f"{step.id}_capture",
        "properties": properties,
        "required": [f.name for f in step.capture if f.required] + ["_confidence"],
        "additionalProperties": False,
    }


SAFETY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "safety_triage",
    "properties": {
        "concern": {
            "type": ["string", "null"],
            "enum": ["self_harm", "suicidal_ideation", "acute_medical", "abuse", None],
            "description": "The category of concern, or null if none is present.",
        },
        "quote": {
            "type": ["string", "null"],
            "description": "The exact words that raised the concern.",
        },
        "certain": {
            "type": "boolean",
            "description": "True only if the concern is explicit rather than inferred.",
        },
    },
    "required": ["concern", "certain"],
    "additionalProperties": False,
}


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "session_summary",
    "properties": {
        "headline": {
            "type": "string",
            "description": "One clause describing what happened in this session.",
        },
        "narrative": {
            "type": "string",
            "description": (
                "Three or four sentences a counsellor reading this in two months "
                "would need. What the client said, what changed, what was agreed. "
                "No clinical interpretation, no advice."
            ),
        },
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to five short factual points worth carrying forward.",
        },
    },
    "required": ["headline", "narrative", "key_points"],
    "additionalProperties": False,
}
