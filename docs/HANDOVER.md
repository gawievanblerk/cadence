# Cadence — build status and handover

Working reference implementation of the conversational digital health platform
described in the job posting. Built in one session; runs end to end offline.

## What exists

A git repository, `cadence`, with 88 files and roughly 8,000 lines of Python plus
the treatment content. Two commits. Not yet pushed anywhere.

- `content/` — the nicotine cessation programme as YAML: seven sessions, a lapse
  recovery pathway, a confidence pathway, thresholds and routing rules, three
  knowledge documents, three exercises, an Afrikaans overlay, two published
  versions (1.0 retired, 1.1 published) so version diffing is demonstrable.
- `src/cadence/` — content loader and registry, safe expression language and rule
  engine, four-layer memory with a context assembler, RAG retriever, the provider
  layer with five vendor adapters plus an offline one, the session state machine,
  guardrails, FastAPI transport, CLI, SQLAlchemy schema, field-level encryption.
- `tests/` — 82 tests, all passing, none mocking the thing under test.
- `scripts/` — three demos that run with no API key.
- `docs/` — ARCHITECTURE.md, AUTHORING.md (for the clinical team),
  PROPOSAL-ANSWERS.md.

## Verification performed

Cloned to a clean directory, `pip install -e ".[dev]"`, 82 tests pass, CLI
validates the content, all three demos run, `ruff check` clean, uvicorn boots and
`/healthz` and `/docs` respond.

## The demos, and what each proves

- `make demo` — a client's first month: five sessions, session gates written in
  YAML, session three recalling what session two recorded, a lapse in week one
  that fires a rule and inserts the recovery session ahead of the ordinary
  sequence. 346 lines of terminal output; screen-recordable.
- `make demo-swap` — role bindings, declared capabilities per vendor, and the
  same extraction schema shown adapted three different ways for OpenAI strict
  mode, Anthropic tool calling and Gemini's dialect. With a vendor key exported
  it runs the same client under that vendor and diffs the clinical outcome.
- `make demo-content` — a version diff in clinical terms, then one threshold
  edited from 4 to 2 in YAML, the same client run again, a different pathway. Then
  a deliberately broken rule showing what the validator refuses to publish.

## Still to do before submitting

1. `docs/PROPOSAL-ANSWERS.md` answers questions 2, 3 and 4 from the actual code.
   Questions 1 and 5 are scaffolded but must be written personally — they are
   about real prior work and about who the architect is.
2. Push to a private GitHub repository, or export a tarball, depending on how much
   is to be shown before there is a contract.
3. Decide what goes in the Upwork application itself. The full repository is
   probably too much for a first message; the three demo outputs and the answers
   to questions 2 to 4 are the substance.

## Known stubs, marked in the code

Bearer token auth is a placeholder with the right shape. The vector store is
correct and O(n) rather than indexed. The core is synchronous. Voice is designed
for but not built.
