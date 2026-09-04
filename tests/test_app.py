"""The installable phone client at /app/.

It is a static shell that talks to the same API as everything else, so what
these tests pin down is that the shell, its manifest, its offline worker and
its assets are served with the types a browser needs to treat it as an app,
and that nothing outside the app folder can be fetched through the route.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cadence.api.main import app, get_engine


@pytest.fixture()
def api(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


class TestAppShell:
    def test_the_shell_is_served_at_the_app_root(self, api) -> None:
        response = api.get("/app/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert 'rel="manifest"' in response.text
        assert "serviceWorker" in response.text

    def test_the_root_without_a_slash_redirects_to_the_app(self, api) -> None:
        response = api.get("/app", follow_redirects=False)
        assert response.status_code in (301, 307, 308)
        assert response.headers["location"].endswith("/app/")

    def test_the_manifest_declares_a_standalone_app(self, api) -> None:
        response = api.get("/app/manifest.webmanifest")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/manifest+json")
        manifest = response.json()
        assert manifest["start_url"] == "/app/"
        assert manifest["display"] == "standalone"
        assert manifest["name"]
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        assert {"192x192", "512x512"} <= sizes

    def test_every_icon_in_the_manifest_exists(self, api) -> None:
        manifest = api.get("/app/manifest.webmanifest").json()
        for icon in manifest["icons"]:
            response = api.get(icon["src"])
            assert response.status_code == 200, icon["src"]
            assert response.headers["content-type"].startswith("image/"), icon["src"]

    def test_the_service_worker_is_served_as_javascript(self, api) -> None:
        response = api.get("/app/sw.js")
        assert response.status_code == 200
        content_type = response.headers["content-type"]
        assert content_type.startswith(("text/javascript", "application/javascript"))

    def test_the_bundled_font_is_served_as_a_font(self, api) -> None:
        response = api.get("/app/atkinson-hyperlegible-400.woff2")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("font/woff2")


class TestAppRouteSafety:
    def test_a_missing_file_is_a_404(self, api) -> None:
        assert api.get("/app/nothing-here.js").status_code == 404

    def test_paths_outside_the_app_folder_are_refused(self, api) -> None:
        # The API module itself sits one folder up from the app folder.
        assert api.get("/app/%2e%2e/main.py").status_code == 404
        assert api.get("/app/..%2Fmain.py").status_code == 404

    def test_only_app_file_types_are_served(self, api) -> None:
        # The folder holds no Python, but the route must refuse the type anyway
        # rather than rely on the folder staying clean.
        assert api.get("/app/anything.py").status_code == 404
