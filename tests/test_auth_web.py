"""The in-browser consent flow: what the dashboard offers and what it stores."""

import json

import pytest

from carddues import config, gmail
from carddues.web.app import create_app

CLIENT_ID = "1234.apps.googleusercontent.com"


@pytest.fixture
def client(conn):
    app = create_app()
    app.config.update(TESTING=True, SERVER_NAME="127.0.0.1:8765")
    with app.test_client() as test_client:
        yield test_client


def write_client_file(kind="web"):
    payload = {
        kind: {
            "client_id": CLIENT_ID,
            "client_secret": "secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    if kind == "web":
        payload[kind]["redirect_uris"] = ["http://127.0.0.1:8765/oauth/callback"]
    path = config.credentials_path()
    config.ensure_dirs()
    path.write_text(json.dumps(payload))
    return path


def test_dashboard_offers_to_connect_when_there_is_no_token(client, conn):
    write_client_file()
    page = client.get("/").get_data(as_text=True)

    assert "Connect Gmail" in page
    assert "Fetch from Gmail" not in page


def test_dashboard_spells_out_the_redirect_uri_for_a_web_client(client, conn):
    write_client_file("web")
    page = client.get("/").get_data(as_text=True)

    assert "http://127.0.0.1:8765/oauth/callback" in page


def test_dashboard_asks_for_the_client_file_when_it_is_missing(client, conn):
    page = client.get("/").get_data(as_text=True)

    assert "No OAuth client found" in page
    assert str(config.credentials_path()) in page


def test_connected_dashboard_offers_fetch_and_disconnect(client, conn, monkeypatch):
    write_client_file()
    monkeypatch.setattr(gmail, "is_connected", lambda: True)
    page = client.get("/").get_data(as_text=True)

    assert "Fetch from Gmail" in page
    assert "Disconnect Gmail" in page
    assert "Connect Gmail" not in page


def test_starting_the_flow_redirects_to_google(client, conn):
    write_client_file()
    response = client.get("/auth/start")

    assert response.status_code == 302
    target = response.headers["Location"]
    assert target.startswith("https://accounts.google.com/o/oauth2/auth")
    assert "gmail.readonly" in target
    assert "access_type=offline" in target
    assert "prompt=consent" in target
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Foauth%2Fcallback" in target


def test_a_desktop_client_gets_a_path_less_loopback_redirect(client, conn):
    """Desktop clients register no URI, so Google only accepts a bare origin."""
    write_client_file("installed")
    response = client.get("/auth/start")

    target = response.headers["Location"]
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2F&" in target


def test_the_dashboard_root_completes_a_desktop_callback(client, conn, monkeypatch):
    write_client_file("installed")

    def fake_fetch(self, **kwargs):
        self.oauth2session.token = {
            "access_token": "at",
            "refresh_token": "rt",
            "scope": gmail.SCOPES,
            "expires_at": 4102444800,
        }

    monkeypatch.setattr("google_auth_oauthlib.flow.Flow.fetch_token", fake_fetch)

    start = client.get("/auth/start")
    state = _state_from(start.headers["Location"])

    response = client.get(f"/?code=abc&state={state}", follow_redirects=True)

    assert "Gmail connected" in response.get_data(as_text=True)
    assert json.loads(config.token_path().read_text())["refresh_token"] == "rt"


def test_starting_the_flow_without_a_client_file_explains_why(client, conn):
    response = client.get("/auth/start", follow_redirects=True)

    assert "Google OAuth client file not found" in response.get_data(as_text=True)


def test_callback_stores_the_token(client, conn, monkeypatch):
    write_client_file()
    seen = {}

    def fake_fetch(self, **kwargs):
        seen.update(kwargs)
        self.oauth2session.token = {
            "access_token": "at",
            "refresh_token": "rt",
            "scope": gmail.SCOPES,
            "expires_at": 4102444800,
        }

    monkeypatch.setattr("google_auth_oauthlib.flow.Flow.fetch_token", fake_fetch)

    start = client.get("/auth/start")
    state = _state_from(start.headers["Location"])

    response = client.get(
        f"/oauth/callback?code=abc&state={state}", follow_redirects=True
    )

    assert "Gmail connected" in response.get_data(as_text=True)
    assert seen == {"code": "abc"}
    stored = json.loads(config.token_path().read_text())
    assert stored["refresh_token"] == "rt"
    assert config.token_path().stat().st_mode & 0o777 == 0o600


def test_callback_rejects_a_mismatched_state(client, conn):
    write_client_file()
    client.get("/auth/start")

    response = client.get("/oauth/callback?code=abc&state=forged", follow_redirects=True)

    assert "state did not match" in response.get_data(as_text=True)
    assert not config.token_path().exists()


def test_callback_reports_a_refused_consent(client, conn):
    write_client_file()
    response = client.get("/oauth/callback?error=access_denied", follow_redirects=True)

    assert "access_denied" in response.get_data(as_text=True)
    assert not config.token_path().exists()


def test_callback_complains_when_google_withholds_a_refresh_token(
    client, conn, monkeypatch
):
    write_client_file()

    def fake_fetch(self, **kwargs):
        self.oauth2session.token = {
            "access_token": "at",
            "scope": gmail.SCOPES,
            "expires_at": 4102444800,
        }

    monkeypatch.setattr("google_auth_oauthlib.flow.Flow.fetch_token", fake_fetch)

    start = client.get("/auth/start")
    state = _state_from(start.headers["Location"])
    response = client.get(
        f"/oauth/callback?code=abc&state={state}", follow_redirects=True
    )

    assert "did not return a refresh token" in response.get_data(as_text=True)
    assert not config.token_path().exists()


def test_disconnect_forgets_the_token(client, conn):
    config.ensure_dirs()
    config.token_path().write_text("{}")

    response = client.post("/auth/disconnect", follow_redirects=True)

    assert "Forgot the stored token" in response.get_data(as_text=True)
    assert not config.token_path().exists()


def test_a_desktop_client_file_is_recognised(conn):
    write_client_file("installed")
    assert gmail.client_type() == "installed"


def test_connection_needs_a_refreshable_token(conn):
    assert gmail.is_connected() is False
    config.ensure_dirs()
    config.token_path().write_text(
        json.dumps(
            {
                "client_id": CLIENT_ID,
                "client_secret": "secret",
                "refresh_token": "rt",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": gmail.SCOPES,
            }
        )
    )
    assert gmail.is_connected() is True


def test_a_corrupt_token_file_is_not_a_connection(conn):
    config.ensure_dirs()
    config.token_path().write_text("not json")
    assert gmail.is_connected() is False


def _state_from(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)["state"][0]
