from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from cadence.orchestration.session_runner import TurnResult


class EnrolRequest(BaseModel):
    identity: dict[str, Any] = Field(
        default_factory=dict,
        description="Direct identifiers. Encrypted at rest and never sent to a model provider.",
    )
    locale: str = "en"
    program_version: str | None = None


class EnrolResponse(BaseModel):
    client_id: str
    episode_id: str
    program_version: str


class StartRequest(BaseModel):
    session_id: str | None = Field(
        default=None, description="Omit to let the engine choose the next open session."
    )


class RespondRequest(BaseModel):
    text: str


class SourceBody(BaseModel):
    source: str = Field(description="The complete YAML source of the file being saved.")


class PublishBody(BaseModel):
    version: str
    notes: str = ""


class TurnResponse(BaseModel):
    session_run_id: str
    text: str
    step_id: str | None
    awaiting_client: bool
    finished: bool
    escalated: bool
    rules_fired: list[str]
    notes: list[str]

    @classmethod
    def of(cls, result: TurnResult) -> TurnResponse:
        return cls(
            session_run_id=result.session_run_id,
            text=result.text,
            step_id=result.step_id,
            awaiting_client=result.awaiting_client,
            finished=result.finished,
            escalated=result.escalated,
            rules_fired=result.rules_fired,
            notes=result.notes,
        )
