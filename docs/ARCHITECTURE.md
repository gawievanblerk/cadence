# Architecture

## The premise

A structured treatment programme delivered conversationally has two halves that
want different things. The clinical half needs to be exact, auditable, versioned,
and identical for every client who meets the same criteria. The conversational
half needs to be fluent, responsive, and forgiving of how people actually talk.

Most systems in this space collapse the two, put the treatment into a long prompt,
and hope the model holds it. That works in a demo and degrades everywhere else:
the programme becomes unauditable, the clinical team cannot change it without an
engineer, the behaviour changes when the vendor ships a new model, and by month
three the client's history no longer fits in the window.

Cadence keeps the halves apart. A state machine decides what happens. A model
decides how it is said.

## Layers

```
                 HTTP / CLI
                     |
              TreatmentEngine                assembly only
                     |
              SessionRunner                  the state machine
        /        |         |        \
   RuleEngine  Memory   Retriever  Guardrails
        |        |         |
     content/  database  content/
                     |
                ModelRouter                  the trust boundary
                     |
   openai | anthropic | gemini | deepseek | ollama | scripted
```

Dependencies point downward only. Nothing above `ModelRouter` imports a vendor
adapter, and nothing in `content/` is executable.

## The provider seam

`cadence.llm.base.Provider` is the entire vendor surface. It has three methods:
`complete` for prose, `structured` for JSON that must conform to a schema, and
`embed` for vectors. Requests are expressed in `Message`, `CompletionRequest` and
JSON Schema, none of which belong to a vendor.

Adapters are plain HTTP over `httpx`, not vendor SDKs. An SDK brings its own retry
policy, auth handling, error taxonomy and release cadence, so a portfolio of five
SDKs is five dependency trees that must all keep working. Five HTTP adapters of
about a hundred lines each are less code and fewer surprises.

Each provider declares capabilities rather than having them assumed. The router
verifies at start-up that every role is bound to a provider that can do that
role's job, which is why binding the embedder to DeepSeek fails when the process
boots rather than at three in the morning.

### Structured output is the only real difference

Vendors genuinely diverge on one thing: how you get guaranteed-shape JSON out of
them. OpenAI has strict `json_schema` with conditions the general schema draft
does not impose. Anthropic expresses it as a forced tool call. Gemini wants an
OpenAPI-flavoured dialect. DeepSeek has JSON mode but no schema. Ollama takes a
schema in `format`.

All five are absorbed inside the adapters. `strictify` adapts a schema to OpenAI's
strict subset by making every property required and nullable, which is what our
extraction schemas mean anyway. `to_gemini_schema` translates by whitelist rather
than passthrough, because Gemini rejects keys it does not know. Providers without
native support get a compatibility path: schema in the prompt, tolerant JSON
extraction, validation, and exactly one repair attempt with the validator's own
complaint fed back. More than one repair means the schema is wrong, not the model.

Extraction code above the seam calls `router.structured()` and gets a validated
dict. It cannot tell which vendor answered, and it never branches on one.

### Roles, not models

Four roles: `counsellor` writes the turn the client reads, `extractor` turns a
reply into typed observations, `summariser` compresses a finished session, and
`embedder` makes vectors. Each is bound to a `provider:model` pair by an
environment variable. Nothing else in the system names a vendor.

This is also how you run a sensible portfolio rather than a monoculture: a strong
model for counselling, a cheap fast one for extraction, a local one for embeddings
so the corpus never leaves your infrastructure. And it is how you migrate, one
role at a time, comparing outcomes as you go.

## The content layer

The clinical team's deliverable is a directory of YAML. A session is a sequence
of typed steps. `say` delivers authored text. `ask` asks a question and extracts
structured observations from the answer. `scale` takes a rating on a fixed range.
`choice` offers fixed options. `teach` explains a knowledge document. `reflect`
plays back what is on record. `assign` sets between-session work. `branch`
evaluates rules and jumps without any model call at all.

Two kinds of clinical rule attach to a session. A `requires` mandatory is a gate:
the session cannot be marked complete while it is unmet, and the orchestrator
re-opens the step that would satisfy it. A `prohibition` mandatory is injected
into every prompt in the session and also checked against generated text before
it reaches the client. A prohibition in a prompt is a request; a prohibition
plus a post-generation check is a control.

Gates, conditions and rules are written in a small expression language:

```yaml
opens_when: "'s01-intake' in episode.sessions_completed and days_since('s01-intake') >= 1"
requires:   "count(history.trigger_situation) >= 3"
when:       "observations.confidence <= thresholds.low_confidence"
```

It is a whitelist walk over Python's parse tree, not `eval`. The allowed node
types are the whole language. A call to an unlisted function, an import, an
attribute on a real object, a comprehension: all of them are parse-time errors.
Being a subset of Python syntax means the clinical team gets editor highlighting
and we did not have to write a parser.

### Validation is the part that earns its keep

Publication runs three passes. Schema: is each file shaped like the thing it
claims to be. Reference: does every `knowledge_ref`, `exercise_ref` and `goto`
resolve. Expression: will every condition evaluate, does it use only names the
engine supplies, is every observation kind real, is every threshold defined.

The third pass is the one that matters. `observations.confidance <= thresholds.low_confidance`
is valid YAML, parses cleanly, evaluates to false forever, and silently disables
a treatment rule. Nobody notices for six months. The validator refuses to publish
it.

### Versioning

A published version pins a set of sessions and a rule file. An episode records
the version and the content hash it started under, and reads that version for its
whole life. New content is visible to new episodes immediately and to in-flight
episodes never, unless a clinician explicitly migrates them. A client who started
session three under 1.0 finishes under 1.0.

`ContentRegistry.diff` produces a clinician-readable comparison of two versions:
sessions and rules added, removed or changed, thresholds moved, and a warning
where a session's content changed without its version number being incremented.
That is what gets signed off, rather than a line diff.

## Memory

Four layers, because they answer different questions and have different costs.

**Structured.** Typed observations with a timestamp, a provenance, and an
extractor confidence. `confidence`, `craving_intensity`, `trigger_situation`,
`lapse`, `commitment` and the rest. This is what the rules read, and it is a
database query rather than an inference call. Scalar kinds answer with their
latest value because "confidence" means today's confidence; list kinds accumulate.
Observations below a confidence floor are recorded but excluded from rule
evaluation, so a model that half-hears "I had one" cannot reroute a pathway on its
own.

**Episodic.** One compressed summary per completed session, written once at the
end of that session. This, not the transcript, is what a session three months
later reads.

**Semantic.** Vectors over session summaries and over the client's own words at
steps that captured something. Not every turn: embedding all of them makes recall
noisier rather than richer. This layer answers the questions the programme did not
know to ask, such as the patio-smoking brother-in-law mentioned once in session
two who is relevant to the family gathering in session nine.

**Knowledge.** Retrieval over the clinical corpus, scoped by the content author
rather than by similarity alone. A `teach` step names its document and gets that
document. Similarity search only adds supporting passages, and only client-facing
ones. A counsellor answering a withdrawal question with a passage about relapse
from four sessions ahead is not being helpful, it is being early.

### Assembly

Per turn, in priority order, under a token budget: the programme frame (persona,
objectives, prohibitions, never dropped), the structured fact sheet restricted to
what the session's `recall:` block asked for, the last few episodic summaries,
authored knowledge for the step, semantic recall, and the current session's
verbatim turns.

When the budget binds, whole sections are dropped lowest priority first. Nothing
is truncated mid-section, because a half-quoted clinical document is a clinical
risk and a half-rendered fact sheet silently loses a fact.

Verbatim turns from past sessions are never included. That single rule is what
keeps context flat across a nine-month programme rather than growing with it.

## Determinism where it counts

The rule engine evaluates authored expressions over the projection and returns
effects plus a decision record for every rule it considered, including the ones
that did not fire. "Why did it not escalate" is the question that actually gets
asked in clinical review, and it is unanswerable if only firings are logged. Each
record stores the rule, its expression, the specific context values the expression
read, and the effects applied.

Safety is deliberately not model-dependent. A deterministic screen runs on every
client utterance before anything clinical happens. It costs nothing, cannot fail
open on a provider outage, and means the highest-consequence path in the system
does not depend on an inference call succeeding.

Adaptive follow-ups are bounded by a counter rather than by an instruction. A
model asked to probe at most twice will sometimes probe five times. A model whose
third probe is never requested cannot.

When generation fails, or breaches a prohibition twice, the client gets the
clinician's authored text verbatim. Degraded, but never unsafe and never silent.

## Privacy and ownership

Direct identifiers live in one encrypted column, sealed with a key held outside
the database. Nothing joins on it, nothing indexes it, and the context assembler
cannot reach it. The counsellor works from a pseudonymous id and typed
observations, so minimisation is structural rather than a rule someone has to
remember.

The router is the only place a request crosses the trust boundary, so it is where
the egress allowlist is enforced and where the processing register is written.
Every outbound call records the provider, the model, the role, the purpose, the
character count and the classes of identifier that were masked. Counts, not
values. That register is the evidence for an Article 30 record.

Article 20 export returns everything held about a person, including the decision
log, because someone is entitled to know not only what was recorded about them but
what the system did with it. Article 17 erasure removes verbatim conversation,
embeddings, summaries and identifiers; whether de-identified outcome observations
are retained is a parameter, because that is a policy decision rather than an
engineering one.

Ownership follows from the same structure. The programme is YAML in the operator's
repository. The client record is ordinary tables that run on SQLite or Postgres
without a rewrite. There is no proprietary datastore, no vendor SDK, no framework
that owns the control flow, and no cloud service that cannot be replaced.

## Known limits

The vector store computes cosine similarity in Python over JSON columns. Correct,
portable, and O(n) per query. Adequate well past a pilot, replaced rather than
tuned when it stops being adequate; the interface is there for that.

Bearer token authentication is a stub with the right shape. Every request carries
a subject and a role, every route declares which roles may call it, and swapping
in an identity provider touches one function.

The core is synchronous. FastAPI runs sync endpoints in a threadpool, which is
fine at pilot concurrency. Making the provider adapters async is a mechanical
change confined to `llm/`, and the interface was designed to permit it.

Voice is designed for and not built. Speech to text feeds the same
`respond(run_id, text)` entry point, and text to speech consumes the same
generated turn, so the state machine does not change; what changes is turn-taking
and barge-in, which is a transport concern.
