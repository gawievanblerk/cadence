"""Seed a demo caseload into whatever database the environment points at.

Three clients, so the clinical review screen has something to review: one who
is four sessions in, one who has been through a lapse, and one who has only
just enrolled. Answers are scripted, and the session dates are moved back so
the day-based gates between sessions have already opened.

    python scripts/seed_demo.py            # add the three clients
    python scripts/seed_demo.py --reset    # wipe every table first

On a server: docker compose -f docker-compose.prod.yml exec cadence \
             python scripts/seed_demo.py --reset

DEMO ONLY. --reset drops every table. Never point this at real client data.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cadence.config import Settings, load_settings  # noqa: E402
from cadence.db.models import Base, SessionRunRow  # noqa: E402
from cadence.orchestration import TreatmentEngine  # noqa: E402


def seed(settings: Settings, *, reset: bool = False) -> tuple[str, str]:
    """Run the scripted caseload. Returns the first client's episode and id."""
    engine = TreatmentEngine(settings)
    if reset:
        Base.metadata.drop_all(engine.db.engine)
        Base.metadata.create_all(engine.db.engine)

    def walk(episode_id: str, answers: dict, session_id: str | None = None, limit: int = 40):
        result = engine.start(episode_id, session_id)
        used: dict[str, int] = {}
        for _ in range(limit):
            if result.finished or not result.awaiting_client:
                return result
            scripted = answers.get(result.step_id or "")
            if scripted is None:
                return result
            if isinstance(scripted, list):
                index = min(used.get(result.step_id, 0), len(scripted) - 1)
                reply = scripted[index]
                used[result.step_id] = index + 1
            else:
                reply = scripted
            result = engine.respond(result.session_run_id, reply)
        return result

    def backdate(episode_id: str, days: int) -> None:
        """DEMO ONLY. The session gates are written in real days, so move the clock."""
        with engine.db.session() as session:
            rows = session.query(SessionRunRow).filter(SessionRunRow.episode_id == episode_id)
            for row in rows:
                if row.completed_at:
                    row.completed_at -= timedelta(days=days)
                if row.started_at:
                    row.started_at -= timedelta(days=days)

    intake = {
        "q-current-use": "Twenty a day, cigarettes, about fifteen years.",
        "q-first-of-day": "Ten minutes after waking, before coffee.",
        "q-reasons": (
            "My daughter asked me to; the money is getting ridiculous; "
            "I am out of breath on the stairs at work."
        ),
        "q-previous-attempts": "Patches in 2019, six weeks, then a bad quarter at work.",
        "rate-confidence": "About a 4, if I am honest.",
        "probe-low-confidence": "I would have to believe I could get through the first week.",
        "q-quit-date": "In 2 weeks.",
    }
    triggers = {
        "recall-last-time": "Yes, that is still right.",
        "q-triggers": (
            "First thing with coffee on the back step; then in the car after I drop my "
            "daughter at school; and after lunch when I go outside with a colleague."
        ),
        "rate-strongest": "The one in the car is the worst. Probably an 8.",
        "q-attach-technique": "The car after the school run.",
    }
    plan = {
        "recall-triggers": "That is the list, and the one after dinner as well.",
        "q-plan-each": (
            "In the car I will keep mints in the door and phone my brother; on the back "
            "step I will take my coffee round to the front; after lunch I will walk the "
            "long way round the block."
        ),
        "q-support": "My brother. He will definitely ask.",
        "q-environment": "The evening before",
        "q-confirm-date": "Yes, the date still stands.",
    }
    quit_day = {
        "rate-confidence": "A 6 today. Better than I expected.",
        "rehearse": "The drive home. I will phone my brother from the car park first.",
    }
    week_one = {
        "q-any-use": "Yes, I had one on the Friday.",
        "q-lapse-circumstances": (
            "In the car park after a bad meeting. A colleague offered me one and I took it."
        ),
        "q-techniques-used": (
            "The mints most days and the walk twice. About a 6 for how useful they were."
        ),
        "rate-confidence": "A 3.",
    }
    programme = (
        (intake, None),
        (triggers, "s02-triggers"),
        (plan, "s03-quit-plan"),
        (quit_day, "s04-quit-day"),
    )

    subject = engine.enrol(identity={"name": "Client A"})
    for answers, session_id in programme:
        backdate(subject.episode_id, 5)
        walk(subject.episode_id, answers, session_id)
    backdate(subject.episode_id, 8)

    other = engine.enrol(identity={"name": "Client B"})
    for answers, session_id in programme:
        backdate(other.episode_id, 5)
        walk(other.episode_id, answers, session_id)
    backdate(other.episode_id, 8)
    walk(other.episode_id, week_one, "s05-week-one-review")

    newcomer = engine.enrol(identity={"name": "Client C"})
    walk(newcomer.episode_id, {"q-current-use": "About ten a day, and some vaping in the evening."})

    return subject.episode_id, subject.client_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="drop every table before seeding")
    args = parser.parse_args()
    settings = load_settings()
    episode, client = seed(settings, reset=args.reset)
    print(f"seeded three clients into {settings.database_url}")
    print(f"first client {client}, episode {episode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
