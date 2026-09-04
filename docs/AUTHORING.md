# Authoring guide

For the clinical team. Everything in this document is a change you can make and
publish without a developer.

## Where the programme lives

Seven kinds of file, all of them plain text.

`program.yaml` holds the counsellor's persona in your own words, the prohibitions
that apply to every session, the safety policy, and the list of published
versions. `sessions/` holds one file per session. `rules/pathways.yaml` holds the
thresholds and the rules that route clients between sessions. `knowledge/` holds
the clinical documents a session can teach from. `exercises/` holds
between-session work. `locales/` holds translations.

## Writing a session

A session is a header and a list of steps.

```yaml
id: s02-triggers
version: 3
title: Mapping the urges
phase: preparation
sequence: 20
estimated_minutes: 25

objectives:
  - Record at least three specific situations in which the client uses nicotine

opens_when: "'s01-intake' in episode.sessions_completed and days_since('s01-intake') >= 1"

recall:
  - trigger_situation
  - confidence
```

`sequence` decides the order of the ordinary programme. Anything numbered 900 or
above is never scheduled by position; it only appears when a rule inserts it,
which is how lapse recovery works.

`opens_when` decides when the session becomes available. Leave it out and the
session opens as soon as its turn comes.

`recall` decides what the counsellor is told about the client before the session
starts. Ask for what the session needs and nothing else. This is not a
performance nicety: a counsellor handed the client's entire history writes vaguer
turns than one handed six relevant facts.

Increment `version` whenever you change a session's content. The version diff
warns you if you forget.

## Steps

```yaml
steps:
  - id: q-triggers
    kind: ask
    text: |
      Think about yesterday, or the last ordinary day you had. Walk me through
      the moments you reached for it.
    capture:
      - name: trigger_situation
        kind: list[string]
        description: >
          Specific situations, each one concrete enough to plan against.
          "After a meal with my brother" counts; "when I am stressed" does not.
        min_items: 1
        maps_to: trigger_situation
    followup:
      max: 3
      when: "count(capture.trigger_situation) < 3"
      instruction: >
        Ask for one more situation. If a situation given is vague, ask for the
        last concrete instance of it instead of accepting the category.
```

The kinds available are `say`, `ask`, `scale`, `choice`, `teach`, `reflect`,
`assign` and `branch`. Their meanings are in `docs/ARCHITECTURE.md`; the ones you
will use most are `say`, `ask` and `scale`.

Your `text` is authoritative. The counsellor is instructed to deliver its meaning
in natural conversational phrasing and not to change it, add advice, or add
anything you did not write. If generation fails, the client gets your text
verbatim.

`capture` is what the session records. The `description` is read by the extractor,
so write it as an instruction to a careful note-taker. The examples of what counts
and what does not, as in the block above, do more work than any other line you
will write.

`maps_to` says which kind of observation the value is stored as. Use it whenever
the field name is not already one of the recognised kinds; `cadence content
validate` lists them if you get one wrong.

`followup` is adaptive probing with a hard ceiling. `max` is enforced by the
engine, not requested in a prompt, so it cannot be exceeded. `instruction` is
written for the model and the client never sees it. `text` is the follow-up
question in your own words: the counsellor keeps to its meaning, and it is what
the client gets verbatim if generation fails. Always write one; without it the
engine asks a neutral "anything else?" instead.

`style_hint` adjusts the tone of one step: "two sentences, no more", "level, not
celebratory".

## Mandatory rules

Two shapes, and the difference matters.

```yaml
mandatory:
  - id: m02-three-triggers
    description: Three distinct trigger situations before this session can close.
    requires: "count(history.trigger_situation) >= 3"
    satisfied_by: [q-triggers]

  - id: m05-no-praise
    description: Do not praise abstinence or express disappointment at use.
    prohibition: >
      Do not congratulate, praise, or express pride, and do not express
      disappointment or surprise. Respond to what happened, not to how it
      reflects on the client.
```

A `requires` mandatory is a gate. The session cannot be marked complete while it
is unmet, and the engine re-opens the step named in `satisfied_by`. It will do so
at most twice, and then record the mandatory as unmet for clinical review rather
than trapping the client in a loop.

A `prohibition` is added to every prompt in the session and separately checked
against what the counsellor generates. Write prohibitions as instructions about
behaviour rather than as topics to avoid.

## Rules and thresholds

```yaml
thresholds:
  low_confidence: 4
  very_low_confidence: 2

rules:
  - id: r20-low-confidence
    description: Confidence at or below the low threshold inserts the confidence session.
    priority: 20
    when: "observations.confidence <= thresholds.low_confidence"
    then:
      insert_session: s91-confidence-build
      set_flag:
        low_confidence_support: true
```

Name your thresholds rather than writing numbers into rules. Tuning the programme
then means changing one number, and every rule that depends on it moves together.

`priority` orders evaluation, lowest first. Safety rules sit at 0. An escalation
raised by an earlier rule cannot be undone by a later one.

`once` defaults to true, so a rule fires at most once per episode. Set it to false
for rules that should fire every time their condition holds.

## Writing conditions

The vocabulary available to you:

`observations.<kind>` is the most recent value, or the accumulated list for kinds
that accumulate. `history.<kind>` is always the full list in time order.
`thresholds.<name>` is a number you defined. `episode.sessions_completed`,
`episode.quit_date`, `episode.last_session_at`, `episode.session_count`.
`client.pathway` and `flags.<name>`. `capture.<field>` is what the current session
has collected so far.

The functions: `count(x)`, `defined(x)`, `latest(x)`, `mean(x)`, `contains(list, text)`,
`days_since(x)` where x is a date, a session id, or `episode.quit_date`, plus
`len`, `min`, `max`, `abs`, `round`, `sum`, `any`, `all`.

A comparison against a missing value is always false. A rule will not fire because
an observation is absent. That includes `days_since` of a date that has not been
set: a client with no quit date has not been "28 days past" it.

## Publishing

```bash
cadence content validate                 # before anything else
cadence content diff 1.0 1.1             # what a version bump actually changes
```

Validation is not a formality. It catches misspelled observation kinds, undefined
thresholds, rules that insert sessions which do not exist, steps that branch to
step ids which do not exist, and knowledge references that do not resolve. Every
one of those is valid YAML and would otherwise fail silently, in production, for
as long as it took someone to notice a pathway that never triggers.

Add a new version to `program.yaml` rather than editing a published one. Clients
already in treatment stay on the version they started, which is deliberate:
changing the programme under someone who is halfway through it is a clinical
problem, not a deployment convenience.

## Translation

`locales/af.yaml` maps an English source string to its translation. Anything not
translated falls back to English rather than failing, so a translator can work
through the programme incrementally and ship what is finished.

```yaml
"Nought to ten, today: how confident are you?": "Nul tot tien, vandag: hoe seker voel jy?"
```
