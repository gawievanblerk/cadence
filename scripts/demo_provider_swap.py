"""Changing the model provider, and what it does and does not change.

The claim under test: treatment content, treatment rules, client data and
business logic are independent of the model vendor, so switching vendors
changes the wording of a turn and nothing else.

This script:

  1. shows the role-to-provider bindings, which live in configuration;
  2. rebinds each role to a different vendor and boots the router, which
     verifies capabilities at start-up rather than at 3am;
  3. shows how the *same* extraction schema is adapted for each vendor's
     structured-output mechanism - the one place vendors genuinely differ;
  4. runs the identical session under every provider whose key is present and
     compares the clinical outcome: observations recorded, rules fired,
     pathway assigned.

With no API keys set it still does 1 to 3, and runs 4 against the offline
provider. Export any of OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY or
DEEPSEEK_API_KEY and that vendor joins the comparison.
"""

from __future__ import annotations

import json
import os

from _demo_support import build_engine, note, rule, run_session

from cadence.config import Binding, Settings
from cadence.llm.providers.gemini import to_gemini_schema
from cadence.llm.providers.openai import strictify
from cadence.llm.router import ModelRouter, build_provider
from cadence.orchestration.schemas import step_schema

CANDIDATES = [
    ("scripted:demo", None),
    ("openai:gpt-4o-mini", "OPENAI_API_KEY"),
    ("anthropic:claude-opus-5", "ANTHROPIC_API_KEY"),
    ("gemini:gemini-2.0-flash", "GEMINI_API_KEY"),
    ("deepseek:deepseek-chat", "DEEPSEEK_API_KEY"),
    ("ollama:llama3.1:8b", "OLLAMA_BASE_URL"),
]

WEEK_ONE = {
    "q-any-use": "Yes, I had one on the Friday.",
    "q-lapse-circumstances": "In the car park after a bad meeting. Someone offered me one and I took it.",
    "q-techniques-used": "The mints most days, and the walk twice. About a 6 for how useful.",
    "rate-confidence": "A 3.",
}

INTAKE = {
    "q-current-use": "Twenty a day, cigarettes, for about fifteen years.",
    "q-first-of-day": "Ten minutes after waking.",
    "q-reasons": "My daughter asked me to; the money; I am out of breath on the stairs.",
    "q-previous-attempts": "Patches in 2019, six weeks.",
    "rate-confidence": "About a 4.",
    "probe-low-confidence": "Getting through the first week.",
    "q-quit-date": "In 2 weeks.",
}
TRIGGERS = {
    "recall-last-time": "Still right.",
    "q-triggers": "Coffee on the back step; the car after the school run; after lunch outside with a colleague.",
    "rate-strongest": "The car one, an 8.",
    "q-attach-technique": "The car after the school run.",
}
PLAN = {
    "recall-triggers": "That is the list.",
    "q-plan-each": "Mints in the car; coffee at the front instead; walk the block after lunch.",
    "q-support": "My brother.",
    "q-environment": "The evening before",
    "q-confirm-date": "Yes, it stands.",
}
QUIT_DAY = {
    "rate-confidence": "A 6.",
    "rehearse": "The drive home. I will phone my brother first.",
}


def show_bindings() -> None:
    rule("Where the vendor choice actually lives")
    settings = Settings.from_env({})
    for role, binding in settings.bindings.items():
        print(f"  CADENCE_ROLE_{role.upper():<11} = {binding}")
    note("four environment variables. There is no other place in the codebase "
         "where a vendor is named by orchestration or content code")


def show_capabilities() -> None:
    rule("What each vendor can do, as declared by its adapter")
    print(f"  {'binding':<32} {'key':<18} capabilities")
    for spec, key_var in CANDIDATES:
        provider = build_provider(Binding.parse(spec))
        caps = ", ".join(sorted(c.value for c in provider.capabilities()))
        present = "-" if key_var is None else ("set" if os.environ.get(key_var) else "not set")
        print(f"  {spec:<32} {present:<18} {caps}")
    note("the router refuses to boot if a role is bound to a provider that "
         "lacks the capability that role needs; DeepSeek cannot be the embedder")


def show_schema_adaptation() -> None:
    rule("The one place vendors genuinely differ: structured output")
    engine = build_engine("swap_schema")
    bundle = engine.bundle()
    step = next(s for s in bundle.session("s05-week-one-review").steps if s.id == "q-any-use")
    schema = step_schema(step)

    print("  The clinician wrote this capture block:\n")
    for field in step.capture:
        print(f"    - {field.name} ({field.kind}): {field.description.strip()[:60]}")

    print("\n  Anthropic - the schema becomes a forced tool call, used as authored:")
    print(_indent(json.dumps(schema["properties"]["nicotine_use"], indent=2)))

    print("\n  OpenAI - strict mode requires every property present and nullable:")
    print(_indent(json.dumps(strictify(schema)["required"], indent=2)))

    print("\n  Gemini - translated into its OpenAPI-flavoured dialect:")
    print(_indent(json.dumps(to_gemini_schema(schema)["properties"]["nicotine_use"], indent=2)))

    print("\n  DeepSeek - no schema mode, so JSON mode plus validate-and-repair.")
    note("all four differences are absorbed inside the provider adapters. The "
         "extraction code above them calls router.structured() and gets a "
         "validated dict, whichever vendor answered")


def clinical_outcome(spec: str) -> dict[str, object]:
    """Run one client through five sessions under a given counsellor provider."""
    engine = build_engine(f"swap_{spec.split(':')[0]}")
    settings = (
        engine.settings.with_binding("counsellor", spec)
        .with_binding("extractor", spec)
        .with_binding("summariser", spec)
    )
    engine.router = ModelRouter(settings)
    engine.semantic.router = engine.router

    enrolment = engine.enrol(identity={"name": "Comparison Client"})
    for answers, session_id in (
        (INTAKE, None),
        (TRIGGERS, "s02-triggers"),
        (PLAN, "s03-quit-plan"),
        (QUIT_DAY, "s04-quit-day"),
        (WEEK_ONE, "s05-week-one-review"),
    ):
        _backdate(engine, enrolment.episode_id)
        run_session(engine, enrolment.episode_id, answers, session_id=session_id, quiet=True)

    progress = engine.progress(enrolment.episode_id)
    return {
        "pathway": progress["pathway"],
        "queued": progress["queued"],
        "confidence": progress["confidence"],
        "lapses": progress["lapses_recorded"],
        "sessions_completed": len(progress["sessions_completed"]),
    }


def _backdate(engine, episode_id: str) -> None:
    from _demo_support import backdate

    backdate(engine, episode_id, 6)


def compare() -> None:
    rule("Same client, same content, different vendors")
    runnable = [
        spec
        for spec, key_var in CANDIDATES
        if key_var is None or os.environ.get(key_var)
    ]
    results: dict[str, dict[str, object]] = {}
    for spec in runnable:
        try:
            results[spec] = clinical_outcome(spec)
            print(f"  {spec:<32} ok")
        except Exception as exc:  # noqa: BLE001 - a vendor being down is a demo outcome
            print(f"  {spec:<32} skipped ({type(exc).__name__}: {str(exc)[:60]})")

    print()
    for spec, outcome in results.items():
        print(f"  {spec}\n    {json.dumps(outcome)}")

    distinct = {json.dumps(v, sort_keys=True) for v in results.values()}
    print()
    if len(distinct) == 1 and len(results) > 1:
        print("  Identical clinical outcome across every provider run.")
    elif len(results) == 1:
        note("only the offline provider was runnable; set a vendor key to "
             "compare a hosted model against it")
    else:
        note("outcomes differed; the difference is in what the extractor read "
             "from free text, which is exactly the surface a model swap "
             "should be evaluated on")


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def main() -> None:
    show_bindings()
    show_capabilities()
    show_schema_adaptation()
    compare()


if __name__ == "__main__":
    main()
