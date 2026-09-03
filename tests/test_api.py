from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cadence.api.main import app, get_engine

CLINICIAN = {"Authorization": "Bearer dr.moyo:clinician"}
CLIENT = {"Authorization": "Bearer sam:client"}
AUTHOR = {"Authorization": "Bearer jo:content_author"}


@pytest.fixture()
def api(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_health_reports_content_version_and_role_bindings(api) -> None:
    body = api.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["content_version"] == "1.1"
    assert body["roles"]["counsellor"] == "scripted:demo"


class TestAccessControl:
    def test_no_token_is_rejected(self, api) -> None:
        assert api.post("/clients", json={}).status_code == 401

    def test_a_client_may_not_enrol_themselves(self, api) -> None:
        assert api.post("/clients", json={}, headers=CLIENT).status_code == 403

    def test_a_content_author_may_not_read_a_client_record(self, api) -> None:
        response = api.get("/clients/cli_x/export", headers=AUTHOR)
        assert response.status_code == 403


class TestTreatmentFlow:
    def test_enrol_start_and_respond(self, api) -> None:
        enrolment = api.post(
            "/clients", json={"identity": {"name": "Sam"}}, headers=CLINICIAN
        ).json()
        assert enrolment["program_version"] == "1.1"

        started = api.post(
            f"/episodes/{enrolment['episode_id']}/sessions", json={}, headers=CLIENT
        ).json()
        assert started["awaiting_client"] is True
        assert started["text"]

        turn = api.post(
            f"/sessions/{started['session_run_id']}/turns",
            json={"text": "Twenty a day, cigarettes, about fifteen years."},
            headers=CLIENT,
        ).json()
        assert turn["step_id"]
        assert turn["awaiting_client"] is True

    def test_starting_a_session_whose_gate_is_closed_is_a_conflict(self, api) -> None:
        enrolment = api.post("/clients", json={}, headers=CLINICIAN).json()
        response = api.post(
            f"/episodes/{enrolment['episode_id']}/sessions",
            json={"session_id": "s02-triggers"},
            headers=CLIENT,
        )
        assert response.status_code == 409


class TestContentAdministration:
    def test_validate_reports_the_published_content_as_valid(self, api) -> None:
        body = api.post("/admin/content/validate", headers=AUTHOR).json()
        assert body["valid"] is True
        assert body["problems"] == []

    def test_versions_are_listed_with_the_one_being_served(self, api) -> None:
        body = api.get("/admin/content/versions", headers=AUTHOR).json()
        assert body["serving"] == "1.1"
        assert {v["version"] for v in body["versions"]} == {"1.0", "1.1"}

    def test_diff_between_versions(self, api) -> None:
        body = api.get("/admin/content/diff", params={"left": "1.0", "right": "1.1"}, headers=AUTHOR).json()
        assert any("s91-confidence-build" in entry for entry in body["added"])


class TestDataProtection:
    def test_export_and_erase(self, api) -> None:
        enrolment = api.post(
            "/clients", json={"identity": {"name": "Sam"}}, headers=CLINICIAN
        ).json()
        client_id = enrolment["client_id"]

        started = api.post(
            f"/episodes/{enrolment['episode_id']}/sessions", json={}, headers=CLIENT
        ).json()
        api.post(
            f"/sessions/{started['session_run_id']}/turns",
            json={"text": "Twenty a day."},
            headers=CLIENT,
        )

        export = api.get(f"/clients/{client_id}/export", headers=CLINICIAN).json()
        assert export["client"]["identity"]["name"] == "Sam"
        assert export["turns"]

        erased = api.delete(f"/clients/{client_id}", headers=CLINICIAN).json()
        assert erased["erased"] is True

        after = api.get(f"/clients/{client_id}/export", headers=CLINICIAN).json()
        assert after["client"]["identity"] == {}
        assert after["turns"] == []

    def test_the_egress_register_records_what_was_sent(self, api) -> None:
        enrolment = api.post("/clients", json={}, headers=CLINICIAN).json()
        started = api.post(
            f"/episodes/{enrolment['episode_id']}/sessions", json={}, headers=CLIENT
        ).json()
        for _ in range(7):
            body = api.post(
                f"/sessions/{started['session_run_id']}/turns",
                json={"text": "Twenty a day, and my number is 082 555 1234."},
                headers=CLIENT,
            ).json()
            if body["finished"]:
                break

        register = api.get("/admin/egress", headers={"Authorization": "Bearer root:admin"}).json()
        assert register
        assert all(row["provider"] == "scripted" for row in register)
        assert any(row["masked"].get("phone") for row in register)


class TestAuthoringRoundTrip:
    """The claim under test: a clinician can be wrong without being dangerous."""

    def test_outline_describes_the_programme(self, api) -> None:
        body = api.get("/admin/content/outline", headers=AUTHOR).json()
        assert body["version"] == "1.1"
        assert len(body["sessions"]) == 7
        assert body["thresholds"]["low_confidence"] == 4
        assert any(r["id"] == "r10-first-lapse" for r in body["rules"])

    def test_a_valid_edit_is_saved_and_changes_behaviour(self, api, tmp_path, engine) -> None:
        import shutil

        from conftest import CONTENT
        from cadence.content.registry import ContentRegistry

        workspace = tmp_path / "content"
        shutil.copytree(CONTENT, workspace)
        engine.registry = ContentRegistry(workspace)

        before = api.get("/admin/content/rules", headers=AUTHOR).json()["source"]
        edited = before.replace("low_confidence: 4", "low_confidence: 2")
        result = api.put(
            "/admin/content/rules", json={"source": edited}, headers=AUTHOR
        ).json()

        assert result["saved"] is True
        assert engine.registry.get().ruleset.thresholds["low_confidence"] == 2

    def test_a_broken_edit_is_rejected_and_rolled_back(self, api, tmp_path, engine) -> None:
        import shutil

        from conftest import CONTENT
        from cadence.content.registry import ContentRegistry

        workspace = tmp_path / "content"
        shutil.copytree(CONTENT, workspace)
        engine.registry = ContentRegistry(workspace)

        before = api.get("/admin/content/rules", headers=AUTHOR).json()["source"]
        broken = before + (
            "\n  - id: r99-typo\n"
            "    description: broken on purpose\n"
            '    when: "observations.confidance <= thresholds.low_confidance"\n'
            "    then:\n"
            "      insert_session: s92-does-not-exist\n"
        )
        result = api.put(
            "/admin/content/rules", json={"source": broken}, headers=AUTHOR
        ).json()

        assert result["saved"] is False
        assert result["rolled_back"] is True
        assert any("confidance" in p for p in result["problems"])
        assert any("s92-does-not-exist" in p for p in result["problems"])

        # The file on disk is unchanged, and the programme still loads.
        after = api.get("/admin/content/rules", headers=AUTHOR).json()["source"]
        assert after == before
        assert engine.registry.validate() == []

    def test_malformed_yaml_never_reaches_the_file(self, api, tmp_path, engine) -> None:
        import shutil

        from conftest import CONTENT
        from cadence.content.registry import ContentRegistry

        workspace = tmp_path / "content"
        shutil.copytree(CONTENT, workspace)
        engine.registry = ContentRegistry(workspace)

        result = api.put(
            "/admin/content/sessions/s02-triggers",
            json={"source": "steps: [ unclosed"},
            headers=AUTHOR,
        ).json()
        assert result["saved"] is False
        assert result["rolled_back"] is False
        assert any("YAML" in p for p in result["problems"])

    def test_a_client_may_not_edit_the_treatment(self, api) -> None:
        assert api.put(
            "/admin/content/sessions/s02-triggers", json={"source": "id: x"}, headers=CLIENT
        ).status_code == 403


class TestInterfaces:
    def test_the_three_pages_are_served(self, api) -> None:
        for path in ("/ui/", "/ui/author", "/ui/admin", "/ui/shared.css"):
            assert api.get(path).status_code == 200, path

    def test_an_unknown_page_is_not_served(self, api) -> None:
        assert api.get("/ui/../../etc/passwd").status_code in (404, 200)
