"""Command line entry point.

Everything the operator needs before there is a UI: validating content,
exporting the authoring schemas, inspecting what a client's record actually
contains, and walking a session in a terminal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cadence.config import load_settings
from cadence.content.loader import ContentError, export_schemas, load_bundle
from cadence.content.registry import ContentRegistry
from cadence.llm.router import available_providers
from cadence.orchestration import TreatmentEngine


def _content_validate(args: argparse.Namespace) -> int:
    try:
        bundle = load_bundle(args.path, args.version)
    except ContentError as exc:
        print(f"INVALID  {args.path} @ {args.version}\n", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"VALID    {bundle.program.title}")
    print(f"  version        {bundle.version_id} ({bundle.version.status})")
    print(f"  content hash   {bundle.content_hash}")
    print(f"  sessions       {len(bundle.sessions)}")
    print(f"  rules          {len(bundle.ruleset.rules)}")
    print(f"  knowledge docs {len(bundle.knowledge)}")
    print(f"  exercises      {len(bundle.exercises)}")
    print(f"  locales        {', '.join(bundle.program.locales)}")
    return 0


def _content_schemas(args: argparse.Namespace) -> int:
    written = export_schemas(args.out)
    for path in written:
        print(f"wrote {path}")
    return 0


def _content_diff(args: argparse.Namespace) -> int:
    registry = ContentRegistry(args.path)
    report = registry.diff(args.left, args.right)
    for heading, entries in report.items():
        for entry in entries:
            print(f"{heading:<9} {entry}")
    return 0


def _providers(_: argparse.Namespace) -> int:
    settings = load_settings()
    print("registered providers:", ", ".join(available_providers()))
    for role, binding in settings.bindings.items():
        chain = settings.fallbacks.get(role) or []
        suffix = f"  (fallback: {', '.join(str(b) for b in chain)})" if chain else ""
        print(f"  {role:<11} {binding}{suffix}")
    return 0


def _export(args: argparse.Namespace) -> int:
    engine = TreatmentEngine(load_settings())
    data = engine.store.export(args.client_id)
    output = json.dumps(data, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(output)
    return 0


def _chat(args: argparse.Namespace) -> int:
    """Walk a session in the terminal. Useful for clinical review of wording."""
    engine = TreatmentEngine(load_settings())
    enrolment = engine.enrol()
    print(f"client {enrolment.client_id}  episode {enrolment.episode_id}\n")
    result = engine.start(enrolment.episode_id, args.session)
    while True:
        print(result.text, "\n")
        for note in result.notes:
            print(f"    [{note}]")
        if result.finished or not result.awaiting_client:
            break
        try:
            reply = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not reply:
            continue
        result = engine.respond(result.session_run_id, reply)
    print(json.dumps(engine.progress(enrolment.episode_id), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cadence")
    sub = parser.add_subparsers(dest="command", required=True)

    content = sub.add_parser("content", help="work with treatment content").add_subparsers(
        dest="content_command", required=True
    )

    validate = content.add_parser("validate", help="validate a content directory")
    validate.add_argument("--path", default="content")
    validate.add_argument("--version", default="@latest")
    validate.set_defaults(func=_content_validate)

    schemas = content.add_parser("schemas", help="export JSON Schema for the authoring models")
    schemas.add_argument("--out", default="content/schema")
    schemas.set_defaults(func=_content_schemas)

    diff = content.add_parser("diff", help="diff two published versions")
    diff.add_argument("left")
    diff.add_argument("right")
    diff.add_argument("--path", default="content")
    diff.set_defaults(func=_content_diff)

    providers = sub.add_parser("providers", help="show model routing")
    providers.set_defaults(func=_providers)

    export = sub.add_parser("export", help="export everything held about one client")
    export.add_argument("client_id")
    export.add_argument("--out")
    export.set_defaults(func=_export)

    chat = sub.add_parser("chat", help="walk a session in the terminal")
    chat.add_argument("--session", default=None)
    chat.set_defaults(func=_chat)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
