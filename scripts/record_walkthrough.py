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




if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_demo import seed

    from cadence.config import Settings

    # A fresh SQLite file per recording, so the video always starts from the
    # same caseload.
    database = ROOT / "var" / "ui.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    episode, client = seed(
        Settings.from_env(
            {
                "CADENCE_DATABASE_URL": f"sqlite:///{database}",
                "CADENCE_CONTENT_PATH": str(ROOT / "content"),
            }
        )
    )

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
