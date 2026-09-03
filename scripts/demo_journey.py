"""A client's first month, in one run.

Five sessions across roughly three weeks of programme time. What to watch for:

  * Session 2 opens only because session 1 completed and a day has passed. The
    gate is a line of YAML, not a line of Python.
  * Session 3 reads back trigger situations recorded in session 2. That recall
    is a database query, not a long context window.
  * Session 5 records a lapse. A rule fires, the lapse recovery session is
    inserted ahead of the ordinary sequence, and the client's pathway changes.
    No model was asked whether that should happen.
  * Nothing in this run needs an API key.
"""

from __future__ import annotations

import json

from _demo_support import backdate, build_engine, note, rule, run_session

INTAKE = {
    "q-current-use": [
        "Cigarettes. It has been about a packet a day since my thirties.",
        "Twenty a day, give or take. Twenty-five on a bad week.",
    ],
    "q-first-of-day": "Ten minutes. Before coffee, before anything.",
    "q-reasons": [
        "My daughter asked me to stop; the money is getting ridiculous; I am out of breath on the stairs at work.",
        "And I do not want to be the grandfather who is always outside.",
    ],
    "q-previous-attempts": "I stopped for six weeks in 2019 using patches. Then we had a bad quarter at work and I picked it up again.",
    "rate-confidence": "About a 4, if I am honest.",
    "probe-low-confidence": "I would have to believe I could get through the first week without falling over.",
    "q-quit-date": "In 2 weeks, let us say.",
}

TRIGGERS = {
    "recall-last-time": "Yes, that is still right.",
    "q-triggers": [
        "First thing with coffee on the back step; then in the car after I drop my daughter at school; and after lunch when I go outside with Priya from accounts.",
        "There is also the one after dinner, standing at the kitchen door.",
    ],
    "rate-strongest": "The one in the car is the worst. Probably an 8.",
    "q-attach-technique": "The one in the car after the school run.",
}

PLAN = {
    "recall-triggers": "That is the list, and the one after dinner as well.",
    "q-plan-each": [
        "In the car I will keep mints in the door and phone my brother; on the back step I will take my coffee round to the front instead; after lunch I will walk the long way round the block with Priya; after dinner I will do the washing up straight away.",
        "That covers all of them I think.",
    ],
    "q-support": "My brother. He will definitely ask.",
    "q-environment": "The evening before",
    "q-confirm-date": "Yes, the date still stands.",
}

QUIT_DAY = {
    "rate-confidence": "A 6 today. Better than I expected.",
    "rehearse": "The drive home. I will phone my brother from the car park before I set off.",
}

WEEK_ONE = {
    "q-any-use": "Yes, I had one. On the Friday.",
    "q-lapse-circumstances": "I was in the car park after a bad meeting and Priya offered me one and I took it. I sat in the car afterwards feeling stupid and then drove home.",
    "q-techniques-used": "I used the mints most days and the walk with Priya twice. About a 6 for how useful they were.",
    "rate-confidence": "A 5.",
}


def main() -> None:
    engine = build_engine("journey")
    enrolment = engine.enrol(identity={"name": "Sam Okonjo", "email": "sam@example.com"})

    rule("Enrolment")
    print(f"  client        {enrolment.client_id}")
    print(f"  episode       {enrolment.episode_id}")
    print(f"  program       {enrolment.program_version} "
          f"(content hash {engine.bundle().content_hash})")
    note("the episode is pinned to this content version for its whole life")

    rule("Session 1 - First conversation and baseline")
    run_session(engine, enrolment.episode_id, INTAKE)
    backdate(engine, enrolment.episode_id, 3)

    rule("Session 2 - Mapping the urges")
    note("this session's gate: session 1 complete, and at least one day since")
    run_session(engine, enrolment.episode_id, TRIGGERS, session_id="s02-triggers")
    backdate(engine, enrolment.episode_id, 4)

    rule("Session 3 - Building the plan")
    note("the opening step reads back what session 2 recorded, from structured memory")
    run_session(engine, enrolment.episode_id, PLAN, session_id="s03-quit-plan")
    backdate(engine, enrolment.episode_id, 5)

    rule("Session 4 - Quit day")
    run_session(engine, enrolment.episode_id, QUIT_DAY, session_id="s04-quit-day")
    backdate(engine, enrolment.episode_id, 7)

    rule("Session 5 - The first week")
    note("watch the branch after the client reports use, and the rule that follows")
    result = run_session(engine, enrolment.episode_id, WEEK_ONE, session_id="s05-week-one-review")

    rule("What the rules decided")
    for rule_id in result.rules_fired:
        print(f"  fired: {rule_id}")
    for decision in engine.store.decisions(enrolment.client_id)[-6:]:
        state = "FIRED " if decision.fired else "      "
        print(f"  {state} {decision.rule_id:<22} {decision.expression}")
        if decision.inputs:
            print(f"         inputs: {json.dumps(decision.inputs)}")

    rule("Where the client is now")
    print(json.dumps(engine.progress(enrolment.episode_id), indent=2))
    print(f"\n  next session: {engine.next_session(enrolment.episode_id)}")
    note("the lapse recovery session was inserted ahead of the ordinary sequence")

    rule("What was actually remembered")
    projection = engine.store.projection(
        client_id=enrolment.client_id, episode_id=enrolment.episode_id
    )
    for kind in ("trigger_situation", "technique_used", "goal", "confidence", "lapse", "commitment"):
        value = projection["history" if isinstance(projection["observations"][kind], list) else "observations"][kind]
        print(f"  {kind:<20} {value}")

    rule("Model usage for the whole month")
    print(json.dumps(engine.router.usage_summary(), indent=2))
    note("every call above went to the offline scripted provider; no network, no keys")


if __name__ == "__main__":
    main()
