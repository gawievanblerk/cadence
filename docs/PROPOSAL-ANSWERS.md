# Answers to the five questions

Written against the working code in this repository. Every claim below points at
a file you can open or a command you can run.

---

## 1. One substantial conversational AI/LLM system you personally designed or built, and your exact responsibility

**This section must be written by the applicant and cannot be drafted for you.**
It is the one answer the client will check hardest, and an invented one is worse
than a modest one.

Structure it as: the system in one sentence, the scale in numbers, the specific
architectural decisions that were yours, and the thing that went wrong and what
you changed. Name your role precisely. "I designed the retrieval and memory
layers and wrote the extraction pipeline; a colleague owned the front end" is
worth more than "I led the project".

Candidates from your own record worth considering, strongest first:

- The health insurance administration platform you built for the Mozambican
  insurer: schema-per-tenant isolation, a claims state machine, a rules-based
  adjudication engine with reason codes, five-role RBAC and a per-tenant audit
  log. It is not conversational, but it is the closest match to the parts of
  this brief that actually carry the risk: deterministic clinical-style rules,
  auditability, multi-tenant privacy, sole technical ownership.
- The natural-language playbook authoring tool. That one is directly about
  turning structured domain content into something a non-programmer can author,
  which is question 4.
- The video-feedback-to-engineering-artefact extraction pipeline, if you want a
  structured-extraction example.

If you lead with the insurance platform, connect it explicitly: adjudication
rules with reason codes are the same shape as treatment rules with a decision
log, and this repository's `rules/` module is that idea applied to a clinical
pathway.

---

## 2. Separating the treatment programme from the underlying LLM

The short answer is that the programme never talks to a model. A state machine
does, and it asks for one thing per turn.

Three separations, all of them enforced structurally rather than by convention.

**A single provider interface.** `cadence.llm.base.Provider` has three methods:
`complete`, `structured`, `embed`. Requests are expressed in vendor-neutral types
and JSON Schema. Every adapter is plain HTTP over `httpx`, not a vendor SDK,
because five SDKs are five dependency trees that all have to keep working
together. Nothing above `cadence/llm/` imports anything below it, and no prompt
template in `orchestration/prompts/` contains a vendor's name.

**Roles, not models.** The system needs four jobs done: counsellor, extractor,
summariser, embedder. Each is bound to a `provider:model` pair by an environment
variable and nowhere else. Migrating from OpenAI to Gemini is:

```bash
CADENCE_ROLE_COUNSELLOR=gemini:gemini-2.0-flash
```

Roles also let you run a portfolio rather than a monoculture: a strong model for
counselling, a cheap fast one for extraction, a locally hosted one for embeddings
so the corpus never leaves your infrastructure. And they let you migrate one role
at a time, comparing outcomes as you go, instead of switching everything at once
and hoping.

**Structured output normalised at the seam.** This is where vendors genuinely
differ and where most portability claims quietly fail. OpenAI has strict
`json_schema` with conditions the general schema draft does not impose; Anthropic
uses a forced tool call; Gemini wants an OpenAPI-flavoured dialect; DeepSeek has
JSON mode but no schema; Ollama takes a schema in `format`. All five are absorbed
inside the adapters. `strictify()` adapts to OpenAI's strict subset,
`to_gemini_schema()` translates by whitelist, and providers with no native support
get schema-in-prompt with validation and exactly one repair attempt carrying the
validator's own complaint. Extraction code calls `router.structured()` and gets a
validated dict. It cannot tell who answered.

The capability check runs at start-up: a role bound to a provider that cannot do
that role's job fails when the process boots, not at three in the morning.

What makes the separation real rather than aspirational is that the model is not
making clinical decisions in the first place. Which step runs next, whether a
follow-up is permitted, whether a session may close, whether a lapse routes into
recovery: all Python and YAML. Change the vendor and the wording changes. Nothing
else does.

```bash
make demo-swap
```

runs the same client under every configured provider and compares the clinical
outcome: pathway, queued sessions, observations recorded.

---

## 3. Long-term client memory across months of treatment

Not one mechanism. Four, because "what is this client's confidence rating" and
"what did they mention once about their brother-in-law" are different questions
with different costs and different failure modes.

**Structured client record.** Typed observations, each with a timestamp, the
session and step that produced it, and the extractor's confidence.
`confidence`, `craving_intensity`, `trigger_situation`, `lapse`, `commitment`,
`goal`, `barrier`, `technique_used`, `quit_date` and the rest. This is what the
treatment rules read, and reading it is a database query, not an inference call.
Scalar kinds answer with their latest value; list kinds accumulate. Observations
below a confidence floor are recorded but excluded from rule evaluation, so a
model that half-hears "I had one" cannot reroute a treatment pathway on its own.

**Episodic summaries.** One compressed record per completed session, written once
at the end of it. That, not the transcript, is what session twelve reads.
Compressing once and reusing is what keeps the cost of month nine the same as the
cost of month one.

**Semantic recall.** Vectors over summaries and over the client's own words at
steps that captured something. Deliberately not every turn: embedding all of them
makes recall noisier rather than richer, because the top hit for "family" becomes
whichever of forty near-identical greetings scored highest. This layer answers
what the programme did not know to ask.

**Knowledge retrieval.** Over the clinical corpus, scoped by the content author.
A `teach` step names its document and gets that document; similarity search only
adds supporting passages, and only client-facing ones. A counsellor answering a
withdrawal question with a relapse passage from four sessions ahead is not being
helpful, it is being early.

Per turn these are assembled in priority order under a token budget: programme
frame first and never dropped, then the fact sheet restricted to what the session
declared it needs, then summaries, then authored knowledge, then semantic recall.
When the budget binds, whole sections are dropped lowest priority first, never
truncated mid-section, because a half-quoted clinical document is a clinical risk.

The rule that makes it work over months: verbatim turns from past sessions are
never included. Within a session the counsellor hears what was just said. Across
sessions it reads structure. That is what keeps context flat instead of linear,
and it is asserted in the test suite rather than assumed.

---

## 4. Letting the clinical team change sessions, questions and rules without a programmer

The treatment lives in `content/`, which contains no code. Sessions, thresholds,
rules, knowledge documents, exercises, translations. A clinician editing a
threshold from 4 to 2 changes which clients get routed into the confidence
session, and that is the whole procedure:

```bash
make demo-content
```

runs a client, edits one number in YAML, runs the identical client again, and
shows a different pathway. No Python changed, no redeployment of logic.

Three things make this safe enough to hand over.

**A schema the authoring UI is generated from.** The models in
`content/models.py` are the contract. `cadence content schemas` exports JSON
Schema from them, so a clinician-facing form can never accept something the
engine will reject.

**A validator that catches the silent mistakes.** Not YAML syntax, which is easy
and rarely the problem. `observations.confidance <= thresholds.low_confidance`
parses cleanly, evaluates to false forever, and disables a treatment rule until
somebody notices a pathway that never fires. Publication is refused for that, for
undefined thresholds, for rules that insert sessions which do not exist, for steps
that branch to step ids which do not exist, and for knowledge references that do
not resolve.

**Version control that protects clients already in treatment.** A published
version pins its sessions and its rules. An episode records the version and
content hash it began under and reads that version for its whole life. New content
reaches new episodes immediately and in-flight episodes never, unless a clinician
migrates them deliberately. `cadence content diff 1.0 1.1` produces the
clinician-readable comparison that gets signed off, including a warning where a
session's content changed without its version number being incremented.

The expression language clinicians write conditions in is a whitelist walk over
Python's parse tree rather than `eval`. It can return a value and do nothing else.
Being a subset of Python syntax means editor highlighting comes free.

The honest limit: adding a new *kind* of step, or a new observation type, is a
code change. That boundary is drawn deliberately. Everything within the existing
vocabulary is theirs; extending the vocabulary is an engineering decision with
clinical review, which is the correct place for it to be.

---

## 5. Who is the technical architect

**To be completed by the applicant.** If applying as an individual, say so
plainly and say what you will not be doing yourself. If applying with anyone
else, name the architect, state that they will be personally on every technical
call, and describe each person's role in one line.

The client has said explicitly that they want direct access to the person
responsible for the architecture. An individual applicant has an advantage here
and should use it rather than apologise for the absence of a team.

---

## What is in this repository

A working reference implementation, not a slide deck. It runs offline with no API
keys, because the offline provider is a real implementation of the provider
interface that happens to run on rules instead of weights. That is what makes the
demos honest and the test suite deterministic.

```bash
make test           # 82 tests, none of them mocking the thing under test
make demo           # a client's first month: five sessions, a lapse, a rerouted pathway
make demo-swap      # what changes when the vendor changes, and what does not
make demo-content   # a clinician edits a threshold and the treatment changes
```

It is also explicit about what is a stub. Bearer token authentication is a
placeholder with the right shape. The vector store is correct and O(n) rather
than indexed. Voice is designed for and not built. Those are marked in the code
where they occur rather than left to be found.
