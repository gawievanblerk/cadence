"""Record a short walkthrough of the three interfaces.

Captions are injected into the page during the run rather than added in an
editor afterwards, so the video can be regenerated from the current code at any
time and cannot drift from what the system actually does.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import time

from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "var" / "video"
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8321"

CAPTION_JS = """
(text) => {
  let bar = document.getElementById('__cap');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = '__cap';
    bar.style.cssText = [
      'position:fixed', 'left:0', 'right:0', 'bottom:0', 'z-index:99999',
      'background:rgba(20,22,21,0.93)', 'color:#f4f1ea', 'padding:13px 26px',
      'font:15px/1.45 Georgia,serif', 'letter-spacing:0.01em',
      'border-top:2px solid #2f5d50', 'transition:opacity .25s'
    ].join(';');
    document.body.appendChild(bar);
    document.body.style.paddingBottom = '70px';
  }
  bar.textContent = text;
}
"""

ANSWERS = [
    "Yes, I had one. On the Friday.",
    "I was in the car park after a bad meeting and a colleague offered me one and I took it. "
    "I sat in the car afterwards feeling stupid and then drove home.",
    "I used the mints most days and the walk twice. About a 6 for how useful they were.",
    "A 4.",
]

BROKEN_RULE = """
  - id: r99-review
    description: Draft rule, being tried out by the clinical team.
    priority: 50
    when: "observations.confidance <= thresholds.low_confidance"
    then:
      insert_session: s92-coping-refresher
"""


async def main(episode: str, client: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1360, "height": 860},
            record_video_dir=str(OUT),
            record_video_size={"width": 1360, "height": 860},
        )
        page = await context.new_page()

        async def cap(text: str, hold: float = 0.0) -> None:
            await page.evaluate(CAPTION_JS, text)
            if hold:
                await page.wait_for_timeout(int(hold * 1000))

        # ---- 1. the client's session ------------------------------------
        await page.goto(f"{BASE}/ui/?episode={episode}&client={client}&session=s05-week-one-review")
        await page.wait_for_timeout(1600)
        await cap(
            "A client four sessions into the programme, opening their one-week review. "
            "Nothing here is running against a paid model: this is the offline provider.",
            4.2,
        )
        await cap(
            "The counsellor asks the question the clinical team authored. "
            "The panel on the right is the client's record, not a chat history.",
            3.6,
        )

        for index, answer in enumerate(ANSWERS):
            if index == 1:
                await cap("The client reports using nicotine. Watch the right-hand panel.", 2.4)
            if index == 3:
                await cap("A confidence rating, which a treatment rule is watching.", 2.2)
            await page.click("#reply")
            await page.type("#reply", answer, delay=17)
            await page.wait_for_timeout(500)
            await page.click("#send")
            await page.wait_for_timeout(1900)

        await page.wait_for_timeout(600)
        await cap(
            "The pathway has changed to lapse recovery and two sessions have been inserted "
            "ahead of the ordinary sequence. That was arithmetic over recorded observations. "
            "No model was asked whether it should happen.",
            5.6,
        )
        await page.evaluate("document.getElementById('engine-log').scrollIntoView({block:'center'})")
        await cap(
            "The rules that fired, with the exact conditions that made them fire.",
            3.8,
        )

        # ---- 2. the clinical team's authoring screen ---------------------
        await page.goto(f"{BASE}/ui/author")
        await page.wait_for_timeout(1800)
        await cap(
            "The same system from the clinical team's side. Sessions, steps, mandatory "
            "rules, and every treatment rule with its condition and its effect.",
            4.6,
        )
        await page.evaluate("document.getElementById('rules').scrollIntoView({block:'center'})")
        await cap(
            "Thresholds are named rather than written into rules, so tuning the programme "
            "means changing one number.",
            3.8,
        )

        # ---- 3. the validator refusing a bad edit ------------------------
        await page.evaluate("window.scrollTo(0,0)")
        await page.click("#open-rules")
        await page.wait_for_timeout(1400)
        await cap("A clinician drafts a new rule, with two misspellings in it.", 3.0)
        source = await page.input_value("#editor")
        await page.fill("#editor", source.rstrip() + "\n" + BROKEN_RULE)
        await page.wait_for_timeout(1400)
        await cap("It is valid YAML. It would parse, evaluate to false forever, and never fire.", 3.6)
        await page.click("#save")
        await page.wait_for_timeout(2000)
        await page.evaluate("document.getElementById('banner').scrollIntoView({block:'center'})")
        await cap(
            "Refused. Three specific problems named, and the file rolled back to its previous "
            "contents. The author's text stays in the editor so the mistake can be fixed.",
            5.6,
        )

        # ---- 4. clinical review -----------------------------------------
        await page.goto(f"{BASE}/ui/admin")
        await page.wait_for_timeout(1900)
        await cap(
            "Clinical review. Caseload, pathways, and the sessions that rules inserted.",
            3.8,
        )
        await page.evaluate("document.getElementById('decisions').scrollIntoView({block:'center'})")
        await page.wait_for_timeout(400)
        await cap(
            "The decision log. Every rule evaluation is recorded, including the ones that did "
            "not fire, with the values each rule read. That is the answer to 'why did it not "
            "escalate', which is the question review actually asks.",
            6.0,
        )
        await page.evaluate("document.getElementById('egress').scrollIntoView({block:'center'})")
        await page.wait_for_timeout(400)
        await cap(
            "And the record of what was sent to which model provider, and which classes of "
            "identifier were masked on the way out. Counts, never values.",
            5.0,
        )
        await cap("Cadence. The whole run above needed no API key and no network.", 4.0)

        await context.close()
        await browser.close()

    # Playwright writes webm. Convert where ffmpeg is available, because an mp4
    # is what anyone receiving this will actually be able to open.
    webm = max(OUT.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    target = ROOT / "var" / "cadence-walkthrough.mp4"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm),
             "-c:v", "libx264", "-preset", "slow", "-crf", "26",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)],
            check=True,
        )
        print(f"wrote {target}")
    except (OSError, subprocess.CalledProcessError):
        print(f"ffmpeg unavailable; the recording is at {webm}")




# ---------------------------------------------------------------------------
# Seeding. Four sessions of one client's programme are run headlessly so the
# recording can pick up at the week-one review, which is where the interesting
# thing happens. A second client is taken past a lapse so the caseload in the
# clinical view is not a single row.
# ---------------------------------------------------------------------------


def seed() -> tuple[str, str]:
    """Run four sessions of one client's programme headlessly.

    The recording then picks up at the week-one review, which is where the
    interesting thing happens. A second client is taken past a lapse so the
    caseload in the clinical view is not a single row, and a third has only
    just enrolled.
    """
    from datetime import timedelta

    from cadence.config import Settings
    from cadence.db.models import SessionRunRow
    from cadence.orchestration import TreatmentEngine

    database = ROOT / "var" / "ui.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()

    engine = TreatmentEngine(
        Settings.from_env(
            {
                "CADENCE_DATABASE_URL": f"sqlite:///{database}",
                "CADENCE_CONTENT_PATH": str(ROOT / "content"),
            }
        )
    )

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


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    episode, client = seed()

    env = dict(
        os.environ,
        CADENCE_DATABASE_URL=f"sqlite:///{ROOT / 'var' / 'ui.db'}",
        CADENCE_CONTENT_PATH=str(ROOT / "content"),
        PYTHONPATH=str(ROOT / "src"),
    )
    server = subprocess.Popen(
        ["python3", "-m", "uvicorn", "cadence.api.main:app", "--port", "8321", "--log-level", "warning"],
        env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(4)
    try:
        asyncio.run(main(episode, client))
    finally:
        server.terminate()
