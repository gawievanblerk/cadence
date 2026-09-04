# Building a new programme

How to create a new therapy programme for Cadence, or add to an existing one.
Written for the clinical team. You will edit plain text files. You will not
write code.

For the full reference on each field, see `AUTHORING.md`. This guide is the
order of work.

## One idea before you start

A programme is data, not code. The engine does not know anything about
nicotine. It knows about sessions, steps, things that get recorded, and rules
over those things. Everything that makes the nicotine programme a nicotine
programme lives in one folder of text files. A sleep programme, an alcohol
programme or a pain self management programme is a different folder with the
same shape.

Each running installation of Cadence serves one programme folder at a time.
A second programme is a second folder, and a second installation pointed at it.

## What a programme is made of

```
my-programme/
  program.yaml          persona, prohibitions, safety, versions
  sessions/             one file per session
  rules/pathways.yaml   thresholds and routing rules
  knowledge/            material the counsellor may teach from
  exercises/            homework between sessions
  locales/              translations, optional
```

## Plan on paper first

Before you open a file, answer these on one page. Every one of them becomes a
line in the programme, and it is much cheaper to change on paper.

1. **What does the counsellor sound like?** Two or three sentences. Calm,
   practical, does not lecture. This becomes the persona.
2. **What must it never do?** Medication advice, diagnosis, techniques outside
   the programme. These become the global prohibitions.
3. **Which sessions, in what order, how far apart?** A list with a one line
   purpose each. Mark which sessions are ordinary and which only happen when
   something goes wrong.
4. **What does each session need to record?** Reasons, ratings, situations,
   commitments. Use the list of recognised kinds below.
5. **What are the decisions?** "If confidence is 4 or below, add the confidence
   session." "If two weeks pass with no session, flag for outreach." Each one
   is a rule, and each number in it is a threshold.
6. **What may a session not close without?** These become gates.

## Step by step

### 1. Make the folder

Copy the nicotine programme folder and rename it, or start from the tree
above with empty folders. Copying is quicker, and you can delete what you do
not need after validating.

### 2. Write `program.yaml`

```yaml
id: better-sleep
title: Better Sleep Programme

locales: [en]
default_locale: en

persona: |
  You are a sleep counsellor working through a structured
  programme. You are calm and practical. You treat the client as
  the expert on their own life and yourself as the expert on the
  method. You ask one question at a time.

global_prohibitions:
  - Never recommend, prescribe or adjust any medication or
    supplement. Refer the client to their prescriber or pharmacist.
  - Never diagnose a sleep disorder or any other condition.
  - Do not introduce techniques that are not part of this programme.

safety:
  escalate_on:
    - self_harm
    - suicidal_ideation
    - acute_medical
  escalation_message: |
    I want to stop the programme material here, because what you
    have described matters more than the session plan. Please
    contact your doctor or a crisis line today. I will let your
    counsellor know we spoke.
  contact_line: >-
    Your clinic will be in touch within one working day.

versions:
  - version: "0.1"
    status: draft
    published_at: "2026-09-04"
    author: Clinical lead
    notes: First draft.
    sessions:
      - s01-sleep-baseline
      - s02-wind-down
      - s90-bad-week
```

The `versions` list names the sessions in the version. A session file that is
not listed here is ignored.

### 3. Write the sessions

One file per session in `sessions/`. Start with the header, then the steps.

```yaml
id: s01-sleep-baseline
version: 1
title: Where you are starting from
phase: preparation
sequence: 10
estimated_minutes: 20

objectives:
  - Record the client's current sleep pattern in their own words
  - Record at least two of the client's own reasons for change
  - Take a baseline confidence rating

recall: []

mandatory:
  - id: m01-own-reasons
    description: At least two reasons, in the client's own words.
    requires: "count(history.goal) >= 2"
    satisfied_by: [q-reasons]
```

Then the steps, in the same file:

```yaml
steps:
  - id: welcome
    kind: say
    text: |
      Over the next few weeks we will work on your sleep one step
      at a time. Today I only want to understand where you are
      starting from.

  - id: q-pattern
    kind: ask
    text: |
      Tell me about an ordinary night at the moment. When you go to
      bed, how long it takes to fall asleep, and when you wake.
    capture:
      - name: sleep_pattern
        kind: text
        description: The current sleep pattern, as described.
        maps_to: free_note

  - id: q-reasons
    kind: ask
    text: |
      Why now? What is making you want to change this at this point?
    capture:
      - name: goal
        kind: list[string]
        description: >
          The client's own reasons, one per item, in their words
          rather than clinical language.
        min_items: 1
        maps_to: goal
    followup:
      max: 2
      when: "count(capture.goal) < 2"
      text: |
        Is there anything else behind it?
      instruction: >
        Ask whether there is anything else behind the decision.
        Do not offer examples.

  - id: rate-confidence
    kind: scale
    text: |
      On a scale of nought to ten, how confident are you that you
      can change your sleep routine and keep the change?
    capture:
      - name: confidence
        kind: integer
        description: Baseline confidence, 0-10.
        range: [0, 10]
        maps_to: confidence

  - id: assign-diary
    kind: assign
    exercise_ref: ex-sleep-diary

  - id: close
    kind: say
    text: |
      That is everything for today. Keep the diary, and next time
      we will build the evening routine from what it shows.
```

Things to notice.

- `sequence` sets the order. Use 10, 20, 30 so you can insert a session later
  without renumbering. Anything 900 or above is never scheduled by position.
  It only runs when a rule inserts it.
- `recall` is what the counsellor is told about the client before the session
  starts. Intake needs nothing. Later sessions should ask for what they need
  and nothing else.
- `mandatory` with `requires` is a gate. The session cannot close until it is
  met. `satisfied_by` names the step to reopen if it is not.
- `followup` always needs a `text`. That is the question the client sees if
  the model is unavailable. The `instruction` is for the model only.
- `maps_to` must be one of the recognised kinds. See the list below.

A second ordinary session opens after the first, with a wait:

```yaml
id: s02-wind-down
version: 1
title: The evening routine
phase: action
sequence: 20
estimated_minutes: 25

objectives:
  - Agree a fixed wind down routine the client can actually do

opens_when: >-
  's01-sleep-baseline' in episode.sessions_completed
  and days_since('s01-sleep-baseline') >= 3

recall:
  - goal
  - confidence

steps:
  - id: reflect-diary
    kind: reflect
    text: |
      Read back the reasons recorded last time and ask what the
      diary showed.

  - id: teach-wind-down
    kind: teach
    knowledge_ref: kb-wind-down

  - id: q-routine
    kind: ask
    text: |
      Which one or two of those could you do every evening this
      week, even on a bad day?
    capture:
      - name: technique_used
        kind: list[string]
        description: The routine steps the client commits to.
        min_items: 1
        maps_to: technique_used

  - id: q-commit
    kind: ask
    text: |
      From which night does this start?
    capture:
      - name: commitment
        kind: string
        description: The commitment and the date it runs from.
        maps_to: commitment

  - id: close
    kind: say
    text: |
      We will look at how it went next time. Keep the diary going.
```

A session that only runs when a rule inserts it:

```yaml
id: s90-bad-week
version: 1
title: A hard week
phase: recovery
sequence: 900
estimated_minutes: 15

objectives:
  - Find what got in the way of the routine and adjust it

opens_when: "flags.bad_week == True"

recall:
  - technique_used
  - commitment

mandatory:
  - id: m90-no-blame
    description: >
      A hard week is information about the plan, not the client.
    prohibition: >
      Do not use the words failure or setback. Do not ask why the
      client did not manage it.

steps:
  - id: q-what-got-in-the-way
    kind: ask
    text: |
      Take me through one of the evenings it did not happen. What
      was going on, and where did the routine drop out?
    capture:
      - name: barrier
        kind: list[string]
        description: What got in the way that evening.
        maps_to: barrier

  - id: q-adjust
    kind: ask
    text: |
      What would make it possible on an evening like that?
    capture:
      - name: technique_used
        kind: list[string]
        description: The revised routine.
        maps_to: technique_used

on_complete:
  set_flag:
    bad_week: false
```

### 4. Write the knowledge and the exercises

Knowledge is what a `teach` step draws on. The counsellor explains it in its
own words and may not go beyond it. Write it as you would explain it to a
client.

```yaml
id: kb-wind-down
title: Winding down
audience: client
tags: [technique, core]
body: |
  The hour before bed tells the body what is coming. The same
  three or four things, in the same order, every night, work
  better than anything clever. Dim the lights. Put screens away.
  Do something quiet and boring. Go to bed at the same time
  whether or not you feel sleepy.
```

An exercise is homework. It is given to the client as written.

```yaml
id: ex-sleep-diary
title: Sleep diary
duration_minutes: 3
instructions: |
  Each morning, before anything else, write down three things:
  what time you went to bed, roughly how long it took to fall
  asleep, and what time you got up. Do not try to change anything
  yet. The diary is a measurement.
```

### 5. Write the rules

`rules/pathways.yaml` holds the thresholds and the decisions. Name every
number. Then tuning the programme means changing one line.

```yaml
thresholds:
  low_confidence: 4
  stalled_days: 14

rules:
  - id: r00-safety-escalation
    description: A safety concern stops delivery and calls a human.
    priority: 0
    once: false
    when: "defined(observations.safety_concern)"
    then:
      escalate: true
      notify: [clinician]
      set_flag:
        program_paused: true

  - id: r10-bad-week
    description: >
      A barrier recorded after the routine was agreed inserts the
      hard week session.
    priority: 10
    when: >-
      defined(observations.barrier)
      and defined(observations.commitment)
    then:
      insert_session: s90-bad-week
      set_flag:
        bad_week: true

  - id: r20-low-confidence
    description: Low confidence asks a human to look.
    priority: 20
    when: "observations.confidence <= thresholds.low_confidence"
    then:
      notify: [clinician]
      set_flag:
        review_requested: true

  - id: r30-stalled
    description: No completed session for two weeks flags outreach.
    priority: 30
    when: >-
      days_since(episode.last_session_at)
      >= thresholds.stalled_days
    then:
      notify: [care_coordinator]
      set_flag:
        outreach_due: true
```

Keep the safety rule. Every programme needs one, at priority 0.

### 6. Validate

```bash
cadence content validate --path my-programme --version 0.1
```

While the version is still a draft you have to name it, as above. Once it is
published, `cadence content validate --path my-programme` is enough.

Run it after every change. It reads every file and reports every problem it
can find: a step kind that does not exist, a `maps_to` that is not a recognised
kind, a threshold used but not defined, a rule that inserts a session that is
not in the version, a branch to a step id that does not exist, a knowledge or
exercise reference that does not resolve, a condition that does not parse.

Every one of those is valid text and would otherwise fail quietly, in front of
a client, for as long as it took someone to notice.

If you use the authoring screen at `/ui/author` instead of editing files, the
same validation runs when you save. If the save breaks the programme, the file
is rolled back and every problem is listed.

### 7. Try it

Point a local installation at the folder and be the client.

```bash
export CADENCE_CONTENT_PATH=./my-programme
export CADENCE_CONTENT_VERSION=0.1   # while the version is a draft
cadence chat                         # a session in the terminal
make api                             # then open /ui/ in a browser
```

No AI provider is needed for this. The offline counsellor delivers your text
word for word and reads answers with simple rules. It is exactly right for
checking that the flow, the gates and the rules behave as you intended. It is
not how the conversation will read with a real model.

Walk every path. Give a thin answer and check the follow-up appears. Give a
low rating and check the flag is set. Read the decision log at `/ui/admin` and
confirm that the rules you expected to fire did, and the ones you did not
expect did not.

### 8. Publish

Change the version's `status` from `draft` to `published` in `program.yaml`.
Clients enrolled from then on start on it.

To change a published programme later, do not edit the published version. Add
a new version to the list, with a higher number and a note on what changed,
and list the sessions it contains. Clients already in treatment stay on the
version they started with. That is deliberate.

```bash
cadence content diff 0.1 0.2
```

shows what a new version actually changes, in clinical terms, before you
publish it.

### 9. Translate, if needed

Add a file per language under `locales/`, keyed by the English text. Anything
not translated falls back to English. A translator can work through the
programme in stages and ship what is done.

## Recognised kinds of observation

`maps_to` must be one of these. They are the vocabulary the engine records,
recalls and evaluates rules over. Choose the closest fit. Adding a new kind is
a change to the engine and needs a developer.

| Kind | Use it for |
|---|---|
| goal | The client's own reasons for change |
| commitment | A stated, dated commitment |
| confidence | A confidence rating |
| motivation | A motivation rating |
| trigger_situation | A specific situation that sets off the behaviour |
| craving_intensity | An urge or craving rating |
| technique_used | A coping response or routine step |
| technique_effectiveness | How useful a technique was |
| barrier | What got in the way |
| support_person | Someone who will help |
| life_change | A relevant change in circumstances |
| quit_date | The date the change starts |
| nicotine_use | Whether the target behaviour happened |
| lapse | A recorded lapse and its circumstances |
| safety_concern | Set by the system. Do not capture into it |
| free_note | Anything that does not fit above |

## Checklist before publishing

- Every session in the version list has a file, and every file you want is in
  the list.
- Every session has an `opens_when`, except the first.
- Every `followup` has a `text`.
- Every `ask` that the programme depends on later has a `maps_to`.
- Every number in a rule is a named threshold.
- There is a safety rule at priority 0.
- Every session that a rule inserts has a `sequence` of 900 or above and an
  `opens_when` on the flag the rule sets, and clears that flag `on_complete`.
- `cadence content validate` reports VALID.
- You have walked every path as the client, and read the decision log.

## What the validator cannot check

It cannot tell you whether the programme is good treatment. It cannot tell you
that a question is leading, that a gate is set too tight, that a threshold is
clinically wrong, or that a session is too long. Those are yours. Run the
programme past a colleague as the client before it goes near a real one.
