# Cadence

A provider-agnostic conversational treatment engine for structured, multi-session
digital health programmes. The reference programme included here is nicotine
cessation: seven sessions, a lapse recovery pathway, and a knowledge base, all
of it authored as data rather than code.

The system is built around one separation. The treatment programme, the clinical
rules, and the client record belong to the operator and live in YAML and a
relational database. The language model is rented, sits behind a single
interface, and is told what to say rather than asked what to do.

## Run it

Nothing below needs an API key, a network connection, or a running database
server. The offline provider is a real implementation of the provider interface
that runs on rules instead of weights, so the whole pipeline executes
deterministically.

```bash
pip install -e ".[dev]"

make test           # 94 tests
make demo           # a client's first month, five sessions, including a lapse
make demo-swap      # what changes when you change vendor, and what does not
make demo-content   # a clinician edits a threshold and the pathway changes
make api            # FastAPI on :8000
```

With the server up there are three reference interfaces, all of them calling the
same API any other client would:

| | |
|---|---|
| `/ui/` | the client's counselling session, with progress and the rules that fired |
| `/ui/author` | the clinical team's authoring screen: edit a session or a threshold, save, and watch the validator refuse a broken rule and roll the file back |
| `/ui/admin` | caseload, the decision log including rules that did not fire, and the register of what was sent to which model provider |
| `/docs` | the OpenAPI page |

They are plain HTML with no build step and no framework: enough to show the
engine working, and small enough to throw away when a real front end is built.

To use a real model, bind a role to a vendor and export its key. That is the
whole procedure.

```bash
export CADENCE_ROLE_COUNSELLOR=anthropic:claude-sonnet-4-5
export CADENCE_ROLE_EXTRACTOR=openai:gpt-4o-mini
export CADENCE_ROLE_EMBEDDER=openai:text-embedding-3-small
export ANTHROPIC_API_KEY=... OPENAI_API_KEY=...
make demo
```

## What is where

```
content/            the treatment programme. No Python. Owned by the clinical team.
  program.yaml        persona, global prohibitions, safety policy, version manifest
  sessions/*.yaml     one file per session: steps, captures, mandatories, gates
  rules/*.yaml        thresholds and pathway rules, versioned alongside sessions
  knowledge/*.yaml    the clinical knowledge base that teach steps draw on
  exercises/*.yaml    between-session work
  locales/*.yaml      translation overlays, keyed by source string

src/cadence/
  content/          loads and validates the programme; pins a version per episode
  rules/            a safe expression language and a deterministic rule engine
  memory/           structured, episodic and semantic memory, and the assembler
  rag/              retrieval over the knowledge base
  llm/              the only code in the repository that knows a vendor exists
  orchestration/    the session state machine, guardrails, extraction, prompts
  api/              HTTP transport, content authoring, three reference interfaces
  db/               schema, engine, field-level encryption

tests/              behaviour, not mocks: the assertions are about decisions
scripts/            the three demos
docs/               architecture, authoring guide, privacy notes
```

## The four claims, and where to check them

**The treatment is not the model's.** `src/cadence/orchestration/session_runner.py`
decides which step runs, whether a follow-up is allowed, whether a session may
close, and which pathway a lapse routes into. None of those consult a model.
`make demo-swap` runs the same client under every configured provider and
compares the clinical outcome.

**The vendor is a configuration line.** `src/cadence/llm/` holds every vendor
adapter, and `CADENCE_ROLE_*` holds every binding. Nothing above `llm/` imports
anything below it except through the `Provider` interface, and no prompt template
contains a vendor's name. Adding a vendor is one file and one line.

**The content is not the code's.** `make demo-content` changes one number in a
YAML file and shows a different treatment pathway for the same client with the
same answers. The validator that runs before publication catches misspelled
observation kinds, undefined thresholds, dangling session references and broken
expressions, which are the mistakes that otherwise fail silently for months.

**A clinician can be wrong without being dangerous.** The authoring endpoint writes
the file, revalidates the whole programme, and rolls the file back if the edit broke
anything, reporting every problem. `tests/test_api.py::TestAuthoringRoundTrip` asserts
it; `/ui/author` shows it.

**Memory is designed rather than accumulated.** Four layers with different
guarantees, described in `docs/ARCHITECTURE.md`, assembled per turn under a token
budget. Verbatim transcripts are never carried between sessions, which is what
keeps a nine-month programme's context flat instead of linear.

## Status

This is a working reference implementation, complete enough to run a programme
end to end and to be argued with. It is explicit about what is a stub: bearer
token authentication is a placeholder with the right shape, the interfaces are
reference pages rather than a product, the vector store is correct and O(n) rather
than indexed, and voice is designed for but not built.
Those are marked in the code where they occur rather than left to be discovered.
