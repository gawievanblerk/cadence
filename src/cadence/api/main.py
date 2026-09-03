"""HTTP transport.

Thin on purpose. Every route below delegates to `TreatmentEngine`; none of them
contains treatment logic, and none of them knows a vendor's name. The same
engine is driven by the CLI and by the test suite, which is what stops the API
from quietly becoming the place where behaviour lives.

Authentication is a stub with a real shape: a bearer token resolved to a
principal with a role. It is marked clearly rather than dressed up, because a
reference implementation that pretends to have solved auth is worse than one
that shows where auth goes.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse

from cadence.api.authoring import ContentWorkspace
from cadence.api.schemas import (
    EnrolRequest,
    EnrolResponse,
    PublishBody,
    RespondRequest,
    SourceBody,
    StartRequest,
    TurnResponse,
)
from cadence.config import load_settings
from cadence.content.loader import ContentError
from cadence.db.crypto import IdentityCipher
from cadence.orchestration import TreatmentEngine

log = logging.getLogger("cadence.api")

app = FastAPI(
    title="Cadence",
    version="0.1.0",
    description="Conversational treatment engine. Treatment content and client data are not the model's.",
)


@lru_cache(maxsize=1)
def get_engine() -> TreatmentEngine:
    return TreatmentEngine(load_settings())


Engine = Annotated[TreatmentEngine, Depends(get_engine)]


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

ROLES = {"client", "clinician", "content_author", "admin"}


class Principal:
    def __init__(self, subject: str, role: str):
        self.subject = subject
        self.role = role

    def require(self, *roles: str) -> None:
        if self.role not in roles:
            raise HTTPException(403, f"role {self.role!r} may not perform this action")


def principal(authorization: Annotated[str | None, Header()] = None) -> Principal:
    """STUB. Replace with the operator's identity provider.

    The shape is what matters here: every request carries a subject and a role,
    and every route states which roles may call it. Swapping this for OIDC token
    validation touches this function and nothing else.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "a bearer token is required")
    token = authorization.split(" ", 1)[1]
    subject, _, role = token.partition(":")
    if role not in ROLES:
        raise HTTPException(401, f"token role must be one of {sorted(ROLES)}")
    return Principal(subject, role)


Caller = Annotated[Principal, Depends(principal)]


@app.on_event("startup")
def check_configuration() -> None:
    """Refuse to start in a state that would quietly leak.

    Specifically: identity encryption not configured, or configured without a
    real AEAD available. Both are fine in development and neither is fine in
    production, so the check warns loudly rather than failing here, and the
    deployment's readiness probe is expected to assert on it.
    """
    settings = load_settings()
    cipher = IdentityCipher(settings.encryption_key)
    if not cipher.is_production_grade:
        log.warning(
            "identity encryption is not production grade "
            "(key configured: %s, AES-GCM available: %s). "
            "Do not run this configuration with real client data.",
            bool(settings.encryption_key),
            cipher.enabled and cipher.is_production_grade,
        )
    log.info(
        "model roles bound to: %s",
        ", ".join(f"{role}={binding}" for role, binding in settings.bindings.items()),
    )


# ---------------------------------------------------------------------------
# Interfaces
#
# Three plain HTML pages with no build step and no framework, served by the
# same process. They call the same API any other client would, so nothing they
# do is available only to them. They are reference interfaces rather than a
# finished product: enough to show the treatment engine working, and small
# enough to throw away when a real front end is built.
# ---------------------------------------------------------------------------

UI_DIR = Path(__file__).parent / "ui"


@app.get("/ui/{page}", include_in_schema=False)
@app.get("/ui/", include_in_schema=False)
def ui(page: str = "client.html") -> FileResponse:
    names = {
        "": "client.html",
        "client": "client.html",
        "author": "author.html",
        "admin": "admin.html",
        "shared.css": "shared.css",
    }
    filename = names.get(page, page if page.endswith((".html", ".css")) else "client.html")
    path = UI_DIR / Path(filename).name
    if not path.exists():
        raise HTTPException(404, "no such page")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Health and configuration
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz(engine: Engine) -> dict[str, Any]:
    bundle = engine.bundle()
    return {
        "status": "ok",
        "program": bundle.program.id,
        "content_version": bundle.version_id,
        "content_hash": bundle.content_hash,
        "roles": {role: str(b) for role, b in engine.settings.bindings.items()},
    }


# ---------------------------------------------------------------------------
# Treatment
# ---------------------------------------------------------------------------


@app.post("/clients", response_model=EnrolResponse, status_code=201)
def enrol(body: EnrolRequest, engine: Engine, caller: Caller) -> EnrolResponse:
    caller.require("clinician", "admin")
    enrolment = engine.enrol(identity=body.identity, locale=body.locale, version=body.program_version)
    return EnrolResponse(
        client_id=enrolment.client_id,
        episode_id=enrolment.episode_id,
        program_version=enrolment.program_version,
    )


@app.get("/episodes/{episode_id}/next")
def next_session(episode_id: str, engine: Engine, caller: Caller) -> dict[str, Any]:
    caller.require("client", "clinician", "admin")
    return {"episode_id": episode_id, "session_id": engine.next_session(episode_id)}


@app.post("/episodes/{episode_id}/sessions", response_model=TurnResponse)
def start_session(
    episode_id: str, body: StartRequest, engine: Engine, caller: Caller
) -> TurnResponse:
    caller.require("client", "clinician")
    try:
        result = engine.start(episode_id, body.session_id)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return TurnResponse.of(result)


@app.post("/sessions/{run_id}/turns", response_model=TurnResponse)
def respond(run_id: str, body: RespondRequest, engine: Engine, caller: Caller) -> TurnResponse:
    caller.require("client", "clinician")
    try:
        result = engine.respond(run_id, body.text)
    except KeyError as exc:
        raise HTTPException(404, f"no session run {run_id}") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return TurnResponse.of(result)


@app.get("/episodes/{episode_id}/progress")
def progress(episode_id: str, engine: Engine, caller: Caller) -> dict[str, Any]:
    caller.require("client", "clinician", "admin")
    return engine.progress(episode_id)


@app.get("/clients")
def list_clients(engine: Engine, caller: Caller) -> list[dict[str, Any]]:
    """The caseload view. Pseudonymous ids only; names are a separate lookup."""
    caller.require("clinician", "admin")
    from sqlalchemy import select

    from cadence.db.models import ClientRow, EpisodeRow

    with engine.db.session() as s:
        clients = list(s.scalars(select(ClientRow).order_by(ClientRow.created_at)))
        episodes = {e.client_id: e for e in s.scalars(select(EpisodeRow))}

    rows = []
    for client in clients:
        episode = episodes.get(client.id)
        row: dict[str, Any] = {
            "client_id": client.id,
            "pathway": client.pathway,
            "locale": client.locale,
            "flags": client.flags or {},
            "erased": client.deleted_at is not None,
        }
        if episode is not None:
            row |= engine.progress(episode.id)
        rows.append(row)
    return rows


@app.get("/sessions/{run_id}/transcript")
def transcript(run_id: str, engine: Engine, caller: Caller) -> list[dict[str, Any]]:
    caller.require("client", "clinician", "admin")
    return [
        {"role": t.role, "text": t.text, "step_id": t.step_id, "at": t.created_at.isoformat(), "meta": t.meta}
        for t in engine.store.turns(run_id)
    ]


@app.get("/clients/{client_id}/decisions")
def decisions(client_id: str, engine: Engine, caller: Caller) -> list[dict[str, Any]]:
    """Why the programme did what it did. Clinical review, and audit evidence."""
    caller.require("clinician", "admin")
    return [
        {
            "rule_id": d.rule_id,
            "expression": d.expression,
            "inputs": d.inputs,
            "fired": d.fired,
            "effects": d.effects,
            "at": d.created_at.isoformat(),
        }
        for d in engine.store.decisions(client_id)
    ]


# ---------------------------------------------------------------------------
# Content authoring
# ---------------------------------------------------------------------------


@app.get("/admin/content/versions")
def content_versions(engine: Engine, caller: Caller) -> dict[str, Any]:
    caller.require("content_author", "clinician", "admin")
    bundle = engine.bundle()
    return {
        "serving": bundle.version_id,
        "content_hash": bundle.content_hash,
        "versions": [v.model_dump() for v in bundle.program.versions],
    }


@app.post("/admin/content/validate")
def validate_content(engine: Engine, caller: Caller, version: str | None = None) -> dict[str, Any]:
    """Dry-run a content change before publishing it.

    The authoring UI calls this on every save. A failure here is the difference
    between a typo caught in an editor and a treatment rule that silently never
    fires for six months.
    """
    caller.require("content_author", "admin")
    problems = engine.registry.validate(version)
    return {"valid": not problems, "problems": problems}


@app.get("/admin/content/outline")
def content_outline(engine: Engine, caller: Caller) -> dict[str, Any]:
    """Everything the authoring screen needs to render the programme."""
    caller.require("content_author", "clinician", "admin")
    return ContentWorkspace(engine.registry).outline()


@app.get("/admin/content/sessions/{session_id}")
def read_session(session_id: str, engine: Engine, caller: Caller) -> dict[str, Any]:
    caller.require("content_author", "clinician", "admin")
    try:
        return {"id": session_id, "source": ContentWorkspace(engine.registry).session_source(session_id)}
    except FileNotFoundError as exc:
        raise HTTPException(404, f"no session {session_id}") from exc


@app.put("/admin/content/sessions/{session_id}")
def write_session(
    session_id: str, body: SourceBody, engine: Engine, caller: Caller
) -> dict[str, Any]:
    """Save an edited session.

    Writes, revalidates the whole programme, and rolls the file back if the edit
    broke anything. A clinician can be wrong without being dangerous, which is
    the only basis on which giving them write access is reasonable.
    """
    caller.require("content_author", "admin")
    result = ContentWorkspace(engine.registry).save_session(session_id, body.source)
    return result.model_dump()


@app.get("/admin/content/rules")
def read_rules(engine: Engine, caller: Caller) -> dict[str, Any]:
    caller.require("content_author", "clinician", "admin")
    return {"source": ContentWorkspace(engine.registry).rules_source()}


@app.put("/admin/content/rules")
def write_rules(body: SourceBody, engine: Engine, caller: Caller) -> dict[str, Any]:
    caller.require("content_author", "admin")
    return ContentWorkspace(engine.registry).save_rules(body.source).model_dump()


@app.post("/admin/content/publish")
def publish_version(body: PublishBody, engine: Engine, caller: Caller) -> dict[str, Any]:
    """Freeze the working content as a new published version."""
    caller.require("content_author", "admin")
    workspace = ContentWorkspace(engine.registry)
    result = workspace.snapshot_version(body.version, body.notes, caller.subject)
    if not result.saved:
        raise HTTPException(400, result.model_dump())
    return result.model_dump()


@app.get("/admin/content/diff")
def diff_content(left: str, right: str, engine: Engine, caller: Caller) -> dict[str, Any]:
    caller.require("content_author", "clinician", "admin")
    try:
        return engine.registry.diff(left, right)
    except ContentError as exc:
        raise HTTPException(400, exc.problems) from exc


@app.post("/admin/content/reload")
def reload_content(engine: Engine, caller: Caller) -> dict[str, Any]:
    """Pick up a published change without a restart.

    In-flight episodes keep the version they were pinned to; only new episodes
    and new sessions see the reload.
    """
    caller.require("content_author", "admin")
    problems = engine.registry.validate()
    if problems:
        raise HTTPException(400, {"problems": problems})
    engine.registry.reload()
    return {"reloaded": True, "serving": engine.bundle().version_id}


# ---------------------------------------------------------------------------
# Data protection
# ---------------------------------------------------------------------------


@app.get("/clients/{client_id}/export")
def export_client(client_id: str, engine: Engine, caller: Caller) -> dict[str, Any]:
    """GDPR Article 20. Everything held about one person, in a portable format."""
    caller.require("client", "clinician", "admin")
    try:
        return engine.store.export(client_id)
    except KeyError as exc:
        raise HTTPException(404, f"no client {client_id}") from exc


@app.delete("/clients/{client_id}", status_code=200)
def erase_client(
    client_id: str, engine: Engine, caller: Caller, keep_outcomes: bool = True
) -> dict[str, Any]:
    """GDPR Article 17."""
    caller.require("clinician", "admin")
    try:
        removed = engine.store.erase(client_id, keep_deidentified_outcomes=keep_outcomes)
    except KeyError as exc:
        raise HTTPException(404, f"no client {client_id}") from exc
    return {"erased": True, "removed": removed}


@app.get("/admin/egress")
def egress_register(engine: Engine, caller: Caller, response: Response) -> list[dict[str, Any]]:
    """The record of what was sent to which provider, and what was masked."""
    caller.require("admin")
    from sqlalchemy import select

    from cadence.db.models import EgressRow

    with engine.db.session() as s:
        rows = list(s.scalars(select(EgressRow).order_by(EgressRow.created_at.desc()).limit(500)))
    response.headers["Cache-Control"] = "no-store"
    return [
        {
            "at": r.created_at.isoformat(),
            "provider": r.provider,
            "model": r.model,
            "role": r.role,
            "purpose": r.purpose,
            "characters": r.characters,
            "masked": r.masked,
            "client": r.client_pseudonym,
        }
        for r in rows
    ]
