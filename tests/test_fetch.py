"""The one endpoint that turns model output into an outbound request.

Everywhere else the model's output only ever reaches a sandboxed iframe. /fetch takes a
URL it chose and calls it from the server, so the interesting tests here are the ones
about what it refuses.
"""

import json

import pytest
from fastapi.testclient import TestClient

from backend import main


@pytest.fixture
def client():
    return TestClient(main.app)


def fetch(client, url):
    response = client.get("/fetch", params={"url": url})
    assert response.status_code == 200, "refusals are reported in the body, not the status"
    return response.json()


# --- what may be called ------------------------------------------------------

def test_an_allowlisted_host_is_permitted():
    assert main.allowed_host("api.github.com")


def test_a_subdomain_of_an_allowlisted_host_is_permitted():
    assert main.allowed_host("en.wikipedia.org")


def test_an_unlisted_host_is_refused():
    assert not main.allowed_host("evil.net")


def test_an_allowlisted_name_as_a_prefix_does_not_pass():
    # The classic way past a suffix check: register example.com.evil.net and the
    # allowlist waves it through. Without the dot this is an open relay.
    assert not main.allowed_host("api.github.com.evil.net")
    assert not main.allowed_host("notwikipedia.org")


def test_the_allowlist_is_case_insensitive():
    assert main.allowed_host("API.GitHub.COM")


def test_an_unlisted_host_is_refused_by_the_endpoint(client):
    result = fetch(client, "https://evil.net/secrets")
    assert "not on the allowed" in result["error"]


def test_only_https_is_fetched(client):
    for url in ["http://api.github.com/x", "file:///etc/passwd", "data:text/plain,hi"]:
        assert "https" in fetch(client, url)["error"], url


def test_a_host_that_resolves_to_localhost_is_refused(client, monkeypatch):
    # An allowlisted name can still point inside the network it is being fetched from,
    # by accident or because someone controls its DNS. The allowlist alone does not
    # decide this.
    monkeypatch.setattr(main, "API_HOSTS", ("internal.test",))
    monkeypatch.setattr(
        main.socket, "getaddrinfo",
        lambda host, port, *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )
    assert "public address" in fetch(client, "https://internal.test/admin")["error"]


def test_a_host_that_resolves_to_a_private_range_is_refused(client, monkeypatch):
    monkeypatch.setattr(main, "API_HOSTS", ("internal.test",))
    monkeypatch.setattr(
        main.socket, "getaddrinfo",
        lambda host, port, *a, **kw: [(2, 1, 6, "", ("10.1.2.3", 0))],
    )
    assert "public address" in fetch(client, "https://internal.test/admin")["error"]


def test_a_name_that_does_not_resolve_is_refused(client, monkeypatch):
    monkeypatch.setattr(main, "API_HOSTS", ("nowhere.test",))
    monkeypatch.setattr(
        main.socket, "getaddrinfo",
        lambda *a, **kw: (_ for _ in ()).throw(main.socket.gaierror("no such host")),
    )
    assert "error" in fetch(client, "https://nowhere.test/x")


# --- what comes back ---------------------------------------------------------

class FakeResponse:
    """Stands in for an API. Nothing here should make a real network call."""

    def __init__(self, chunks, status=200, content_type="application/json"):
        self.chunks = chunks
        self.status_code = status
        self.headers = {"content-type": content_type}

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


def fake_httpx(response, recorder=None):
    class FakeStream:
        def __init__(self, *args, **kwargs):
            if recorder is not None:
                recorder.append((args, kwargs))

        async def __aenter__(self): return response
        async def __aexit__(self, *exc): return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            if recorder is not None:
                recorder.append(("client", kwargs))

        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        def stream(self, *args, **kwargs): return FakeStream(*args, **kwargs)

    return FakeClient


@pytest.fixture
def permitted(monkeypatch):
    monkeypatch.setattr(main, "API_HOSTS", ("api.test",))
    monkeypatch.setattr(main, "resolves_to_public_address", lambda host: True)


def test_a_permitted_call_returns_the_body(client, permitted, monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_httpx(FakeResponse([b'{"ok": true}'])))
    result = fetch(client, "https://api.test/data")
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"ok": True}
    assert not result["truncated"]


def test_the_call_is_anonymous_and_does_not_follow_redirects(client, permitted, monkeypatch):
    # A redirect is a way back off the allowlist, and any forwarded cookie or auth header
    # would be the browser's, sent somewhere the user never chose.
    calls = []
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_httpx(FakeResponse([b"{}"]), calls))
    fetch(client, "https://api.test/data")

    client_kwargs = next(kw for tag, kw in calls if tag == "client")
    assert client_kwargs["follow_redirects"] is False
    request_kwargs = next(kw for entry, kw in calls if entry != "client")
    sent = {k.lower() for k in request_kwargs.get("headers", {})}
    assert not (sent & {"cookie", "authorization"})


def test_an_oversized_response_is_truncated(client, permitted, monkeypatch):
    # An unbounded body would be pasted straight into a prompt, where every character is
    # paid for on a local model.
    monkeypatch.setattr(main, "FETCH_MAX_BYTES", 100)
    monkeypatch.setattr(
        main.httpx, "AsyncClient", fake_httpx(FakeResponse([b"x" * 50] * 10)),
    )
    result = fetch(client, "https://api.test/big")
    assert len(result["body"]) <= 100
    assert result["truncated"]
