from __future__ import annotations

from pathlib import Path

import pytest

from cadence.config import Settings
from cadence.content.registry import ContentRegistry
from cadence.db.engine import Database
from cadence.orchestration import TreatmentEngine

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "CADENCE_DATABASE_URL": f"sqlite:///{tmp_path / 'test.db'}",
            "CADENCE_CONTENT_PATH": str(CONTENT),
        }
    )


@pytest.fixture()
def registry() -> ContentRegistry:
    return ContentRegistry(CONTENT)


@pytest.fixture()
def bundle(registry: ContentRegistry):
    return registry.get()


@pytest.fixture()
def database(settings: Settings) -> Database:
    db = Database(settings.database_url)
    db.create_all()
    return db


@pytest.fixture()
def engine(settings: Settings, database: Database) -> TreatmentEngine:
    return TreatmentEngine(settings, database=database)


def walk(engine: TreatmentEngine, episode_id: str, answers: dict[str, str | list[str]], session_id=None,
         max_turns: int = 40):
    """Drive a session with answers addressed by step id."""
    result = engine.start(episode_id, session_id)
    used: dict[str, int] = {}
    for _ in range(max_turns):
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


INTAKE_ANSWERS = {
    "q-current-use": "Twenty a day, cigarettes, for about fifteen years.",
    "q-first-of-day": "Ten minutes after waking.",
    "q-reasons": "My daughter asked me to; the money; I am out of breath on the stairs.",
    "q-previous-attempts": "Patches in 2019, six weeks.",
    "rate-confidence": "About a 7.",
    "probe-low-confidence": "Getting through the first week.",
    "q-quit-date": "In 2 weeks.",
}
