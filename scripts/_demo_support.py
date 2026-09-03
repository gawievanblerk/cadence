"""Shared plumbing for the demo scripts.

Two things here are demo-only and are marked as such wherever they are used:
backdating completed sessions so that a programme designed to run over months
can be walked through in ten seconds, and a scripted client whose answers are
fixed so that a run is reproducible.
"""

from __future__ import annotations

import sys
import textwrap
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cadence.config import Settings  # noqa: E402
from cadence.db.models import SessionRunRow  # noqa: E402
from cadence.orchestration import TreatmentEngine  # noqa: E402

WIDTH = 78


def build_engine(db_name: str = "demo", **overrides: str) -> TreatmentEngine:
    path = ROOT / "var" / f"{db_name}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    env = {
        "CADENCE_DATABASE_URL": f"sqlite:///{path}",
        "CADENCE_CONTENT_PATH": str(ROOT / "content"),
        **overrides,
    }
    return TreatmentEngine(Settings.from_env(env))


def rule(title: str = "", char: str = "=") -> None:
    if title:
        print(f"\n{char * 4} {title} {char * max(0, WIDTH - len(title) - 6)}")
    else:
        print(char * WIDTH)


def counsellor(text: str) -> None:
    for line in text.split("\n"):
        if not line.strip():
            print()
            continue
        print(textwrap.fill(line.strip(), WIDTH, initial_indent="  ", subsequent_indent="  "))
    print()


def client(text: str) -> None:
    print(textwrap.fill(text, WIDTH, initial_indent="> ", subsequent_indent="  "))
    print()


def note(text: str) -> None:
    print(textwrap.fill(f"[{text}]", WIDTH, initial_indent="    ", subsequent_indent="     "))


def run_session(
    engine: TreatmentEngine,
    episode_id: str,
    answers: dict[str, str | list[str]],
    *,
    session_id: str | None = None,
    quiet: bool = False,
    max_turns: int = 40,
) -> object:
    """Walk one session with answers addressed by step id.

    Addressed by step rather than by position because the engine may re-ask a
    step - a follow-up, or a step re-opened because a mandatory was unmet - and
    a positional script would silently answer the wrong question when it did.
    Where a step has a list of answers, each re-ask takes the next one.
    """
    result = engine.start(episode_id, session_id)
    used: dict[str, int] = {}

    for _ in range(max_turns):
        if not quiet:
            counsellor(result.text)
            for n in result.notes:
                note(n)
        if result.finished or not result.awaiting_client:
            break

        step = result.step_id or ""
        scripted = answers.get(step)
        if scripted is None:
            if not quiet:
                note(f"the scripted client has nothing to say at step {step!r}")
            break
        if isinstance(scripted, list):
            index = min(used.get(step, 0), len(scripted) - 1)
            reply = scripted[index]
            used[step] = index + 1
        else:
            reply = scripted

        if not quiet:
            client(reply)
        result = engine.respond(result.session_run_id, reply)
    return result


def backdate(engine: TreatmentEngine, episode_id: str, days: int) -> None:
    """DEMO ONLY. Move every completed session run back in time.

    The programme's session gates are written in real days - "at least one day
    after intake", "at least five days after quit day" - because that is how a
    clinician thinks about it. Rather than weaken the gates for the demo, the
    demo moves the clock.
    """
    with engine.db.session() as s:
        for row in s.query(SessionRunRow).filter(SessionRunRow.episode_id == episode_id):
            if row.completed_at:
                row.completed_at = row.completed_at - timedelta(days=days)
            if row.started_at:
                row.started_at = row.started_at - timedelta(days=days)
