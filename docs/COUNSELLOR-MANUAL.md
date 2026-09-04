# Counsellor manual

For counsellors, clinicians and care coordinators who look after clients on a
Cadence programme. Nothing in this document needs a computer background.

## What Cadence is

Cadence runs a treatment programme as a series of conversations. A client talks
to a counsellor in a chat window. The counsellor asks the questions the clinical
team wrote, listens to the answers, records what matters, and follows the rules
the clinical team set for what happens next.

Three separate things make that work, and it helps to keep them apart in your
head.

**The programme.** The sessions, the questions, the homework, the clinical
rules and the safety limits. Your clinical team writes these in plain text
files. Nobody else decides what the programme says.

**The engine.** The part that keeps score. It decides which session comes next,
whether a session may close, which rule has fired, and whether a client needs a
human. It does this with arithmetic on what has been recorded. It does not use
an AI model to make any of those decisions.

**The language model.** The AI that turns the clinical team's written question
into natural conversation and reads the client's reply. It is told what to say
and asked what the client meant. It is never asked what should happen next.

You are the fourth part. The system records, routes and flags. It does not
phone anyone, and it does not replace clinical judgement. When it raises a flag,
a person has to act on it.

## How a client moves through a programme

**Enrolment.** A client is enrolled once, and that starts an episode. The
episode is pinned to the version of the programme that was current on that day.
If the clinical team publishes a new version next month, clients already in
treatment stay on the version they started with.

**Sessions in order.** The programme has a fixed sequence of ordinary sessions.
For the nicotine programme that is intake, mapping the urges, the quit plan,
quit day, and the first week review. Each session has an opening condition,
usually that the previous session is done and a number of days have passed. A
client cannot skip ahead.

**Inserted sessions.** Some sessions never appear in the ordinary order. They
only appear when a rule puts them there. In the nicotine programme the lapse
recovery session and the confidence building session work this way. When a rule
inserts one, it runs before the next ordinary session.

**Pathways.** Every client is on a pathway. It is a label for the kind of care
they are getting right now.

| Pathway | Meaning |
|---|---|
| standard | The ordinary programme |
| lapse_recovery | A lapse has been recorded and the recovery session is queued or in progress |
| intensive | Three or more lapses. A human review has been requested |
| maintenance | Four weeks past the quit date with no recorded use |

## What happens inside a session

A session is a list of steps. Each step is one of a small number of types. The
client never sees the type. You will, in the decision log and the authoring
screen, so here is what they mean.

- **Say.** The counsellor says something the clinical team wrote. It may reword
  it to sound natural. It may not change the meaning or add advice.
- **Ask.** A question. The answer is read and the facts in it are recorded, for
  example the client's reasons for stopping, or a situation in which they smoke.
- **Follow-up.** If an answer is thin, the counsellor may ask once or twice
  more. The clinical team sets the maximum. The counsellor cannot go past it.
- **Scale.** A rating on a fixed range, usually nought to ten.
- **Choice.** A question with fixed options. The counsellor offers exactly those
  options and no others.
- **Teach.** The counsellor explains a piece of clinical material the team
  wrote, in its own words, without going beyond the material.
- **Reflect.** The counsellor reads back what is already on record about the
  client and checks it still holds.
- **Assign.** Homework between sessions, given as the clinical team wrote it.
- **Branch.** A fork. The engine picks the next step from what was just
  recorded. No model is involved.

**Session gates.** A session can have conditions that must be met before it may
close. For example, the intake cannot close until the client has given at least
two reasons of their own for stopping. If a gate is unmet, the engine reopens
the step that would satisfy it. It does this at most twice, then records the
gate as unmet for your review rather than trapping the client in a loop.

**Prohibitions.** Each session, and the programme as a whole, has a list of
things the counsellor must never do. Never recommend or adjust medication. Never
diagnose. Never praise abstinence or express disappointment at a lapse. These
are given to the model as instructions and also checked against every reply
before the client sees it. If a reply breaks a rule twice, the client gets the
clinical team's written text instead.

## Safety

Every client message is screened before anything else happens. If it contains a
sign of self harm, suicidal thinking or an acute medical problem, the session
stops at once. The client sees the programme's safety message and the contact
line. The session is marked as abandoned, a safety concern is recorded, and the
client is flagged `program_paused`.

The system does not contact anyone. It does not send a text, an email or a
call. **Someone on the team must check the caseload and follow up.** Treat the
`program_paused` flag as a same day action.

## The clinical review screen

Open it at `/ui/admin`. It has three parts.

**Caseload.** One row per client.

| Column | What it tells you |
|---|---|
| Client | The client's pseudonymous id. Names are never shown here |
| Pathway | Which pathway they are on now |
| Progress | Sessions completed out of the programme |
| Confidence | Their most recent confidence rating |
| Lapses | How many lapses have been recorded |
| Queued by rule | Sessions a rule has inserted that have not run yet |
| Flags | Markers set by rules. See the list below |

**Decision log.** Every rule that was evaluated for a client, whether it fired
or not, with the values it read. This is the answer to "why did the system do
that". If a client was routed into lapse recovery, the log shows the rule, the
condition, and the recorded values that made it true. Rules that did not fire
are listed too, so you can see what the engine considered and rejected.

**Data sent to model providers.** A register of every call made to an outside
AI service: which provider, for what purpose, how many characters, and what
classes of personal information were masked before sending. Counts, not
values. You will rarely need it, but it is the evidence if anyone asks what left
the building.

## Flags

Flags are set by rules and appear in the caseload. They are your to do list.

| Flag | Set when | What to do |
|---|---|---|
| program_paused | A safety concern was detected | Contact the client today |
| review_requested | Three or more lapses, or a very low confidence rating | Review the case and decide on next steps |
| outreach_due | No completed session for two weeks | Reach out. The system will not send a reminder |
| lapse_active | A lapse was recorded and recovery has not yet run | Nothing. The recovery session is queued. Clears when it completes |
| low_confidence_support | Confidence was rated at or below the low threshold | Nothing. The confidence session is queued |
| intake_done | Intake completed | Nothing |

## A daily routine

1. Open the caseload.
2. Look for `program_paused` first. Act on it before anything else.
3. Look for `review_requested` and `outreach_due`. Plan those contacts.
4. For anyone you are about to speak to, open their decision log and their
   progress. Read what the engine recorded and why it routed them where it did.
5. If something in the log looks wrong, do not correct it in the system. Note
   the client id and the rule, and raise it with the clinical team. A rule that
   fires wrongly for one client fires wrongly for everyone.

## Privacy, in short

- The client's name and contact details are stored encrypted, apart from
  everything else. The counsellor, the engine and the AI model work from a
  pseudonymous id and never see them.
- Before anything is sent to an AI provider, names, phone numbers, emails and
  similar identifiers are masked.
- Transcripts of one session are never carried into the next. The counsellor is
  given a short list of recorded facts, chosen by the clinical team for that
  session, and nothing more.
- A client can ask for everything held about them. The export includes the
  decision log, so they can see what the system did as well as what it stored.
- A client can ask for erasure. Conversation text, summaries and identifiers are
  removed.

## Trying a session yourself

The quickest way to understand the programme is to be the client.

- On a phone, open `/app/`. This is what a client holds in their hand: one
  conversation, a progress line, and a clear message between sessions. Use the
  browser's Add to Home Screen and it opens like an app. The Demo switch at
  the top shows what the engine did after each answer.
- In the browser, open `/ui/`, enrol a test client, and work through the intake.
  The panel on the right shows progress, the programme, and every rule that
  fired as you go.
- On a terminal, `cadence chat` walks a session in text.

Use a made up name. Test clients sit in the same caseload as real ones until
they are deleted.

## What the current build does not do

This is a working reference system, not a finished product. Be aware of these
limits when you rely on it.

- **Nothing is sent.** Rules can name who should be notified, and the decision
  log records it, but no message goes out. People must check the caseload.
- **A paused client is not locked out.** The `program_paused` flag is set after
  a safety escalation, but the system does not yet refuse to start their next
  session. Watch the flag.
- **The offline counsellor is plain.** Without an AI provider configured, the
  counsellor delivers the clinical team's text word for word and reads answers
  with simple rules. It is enough to test the flow of a programme. It is not how
  the conversation will read with a real model behind it.
- **Sign in is a placeholder.** The screens ask for a role, not a password. Do
  not put real client data into a build that has not had proper sign in added.
