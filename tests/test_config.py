"""Settings that come from the environment.

The database URL is the one setting that hosting platforms hand us in a shape
we did not choose. Railway, Heroku and Render all give ``postgresql://`` or
``postgres://``, and SQLAlchemy reads the bare scheme as "use psycopg2", which
is not installed. The settings loader pins the driver so the platform's URL
works as given.
"""

from __future__ import annotations

import pytest

from cadence.config import Settings


class TestDatabaseUrl:
    @pytest.mark.parametrize(
        "given, expected",
        [
            (
                "postgresql://cadence:pw@postgres.railway.internal:5432/railway",
                "postgresql+psycopg://cadence:pw@postgres.railway.internal:5432/railway",
            ),
            (
                "postgres://cadence:pw@host:5432/db",
                "postgresql+psycopg://cadence:pw@host:5432/db",
            ),
        ],
    )
    def test_a_platform_postgres_url_is_pinned_to_the_installed_driver(
        self, given: str, expected: str
    ) -> None:
        settings = Settings.from_env({"CADENCE_DATABASE_URL": given})
        assert settings.database_url == expected

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+psycopg://cadence:pw@host:5432/db",
            "sqlite:///var/cadence.db",
            "sqlite:///:memory:",
        ],
    )
    def test_an_explicit_driver_or_another_backend_is_left_alone(self, url: str) -> None:
        settings = Settings.from_env({"CADENCE_DATABASE_URL": url})
        assert settings.database_url == url

    def test_the_default_is_sqlite(self) -> None:
        assert Settings.from_env({}).database_url == "sqlite:///var/cadence.db"
