from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Roles, not model names. Everything above the LLM layer asks for a role; the
# binding from role to provider and model lives in configuration and nowhere
# else. This is the whole of the "switch provider without rebuilding" claim,
# and it is deliberately boring.
ROLES = ("counsellor", "extractor", "summariser", "embedder")

DEFAULT_BINDINGS = {
    "counsellor": "scripted:demo",
    "extractor": "scripted:demo",
    "summariser": "scripted:demo",
    "embedder": "hashing:local-384",
}


@dataclass(frozen=True)
class Binding:
    provider: str
    model: str

    @classmethod
    def parse(cls, spec: str) -> Binding:
        provider, _, model = spec.partition(":")
        if not provider:
            raise ValueError(f"invalid model binding {spec!r}; expected provider:model")
        return cls(provider.strip(), model.strip())

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.provider}:{self.model}"


def _pin_postgres_driver(url: str) -> str:
    """Rewrite a bare ``postgresql://`` or ``postgres://`` URL to use psycopg.

    Hosting platforms hand out the bare scheme, and SQLAlchemy reads it as
    psycopg2, which this image does not ship. Anything that already names a
    driver, and every other backend, passes through untouched.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///var/cadence.db"
    content_path: Path = Path("content")
    content_version: str = "@latest"
    bindings: dict[str, Binding] = field(default_factory=dict)
    fallbacks: dict[str, list[Binding]] = field(default_factory=dict)
    redact_egress: bool = True
    allowed_egress_providers: frozenset[str] = frozenset()
    encryption_key: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(env if env is not None else os.environ)

        bindings: dict[str, Binding] = {}
        fallbacks: dict[str, list[Binding]] = {}
        for role in ROLES:
            spec = env.get(f"CADENCE_ROLE_{role.upper()}") or DEFAULT_BINDINGS[role]
            bindings[role] = Binding.parse(spec)
            chain = env.get(f"CADENCE_ROLE_{role.upper()}_FALLBACK", "").strip()
            fallbacks[role] = [Binding.parse(s) for s in chain.split(",") if s.strip()]

        allowed = env.get(
            "CADENCE_ALLOWED_EGRESS_PROVIDERS",
            "scripted,hashing,ollama,openai,anthropic,gemini,deepseek",
        )
        return cls(
            database_url=_pin_postgres_driver(
                env.get("CADENCE_DATABASE_URL", "sqlite:///var/cadence.db")
            ),
            content_path=Path(env.get("CADENCE_CONTENT_PATH", "content")),
            content_version=env.get("CADENCE_CONTENT_VERSION", "@latest"),
            bindings=bindings,
            fallbacks=fallbacks,
            redact_egress=env.get("CADENCE_REDACT_EGRESS", "true").lower() != "false",
            allowed_egress_providers=frozenset(
                p.strip() for p in allowed.split(",") if p.strip()
            ),
            encryption_key=env.get("CADENCE_ENCRYPTION_KEY") or None,
        )

    def with_binding(self, role: str, spec: str) -> Settings:
        """Return a copy with one role rebound.

        Used by the provider-swap demo and by the admin API's dry-run, both of
        which need to exercise a different provider without a process restart.
        """
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        return Settings(
            database_url=self.database_url,
            content_path=self.content_path,
            content_version=self.content_version,
            bindings={**self.bindings, role: Binding.parse(spec)},
            fallbacks=self.fallbacks,
            redact_egress=self.redact_egress,
            allowed_egress_providers=self.allowed_egress_providers,
            encryption_key=self.encryption_key,
        )


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:  # pragma: no cover - dotenv is optional at runtime
        pass
    return Settings.from_env()
