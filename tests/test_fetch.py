"""The one endpoint that turns model output into an outbound request.

Everywhere else the model's output only ever reaches a sandboxed iframe. /fetch takes a
URL it chose and calls it from the server. Which API that is belongs to the model: there
is no list of permitted hosts, so the tests here are about what it will call and what
comes back.
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


# --- what may be called ------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://api.github.com/repos/a/b",
    "https://some-api-nobody-listed.example/v1/things",
    "http://plain-http.example/data",
])
def test_any_api_the_model_names_is_called(client, monkeypatch, url):
    calls = []
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_httpx(FakeResponse([b"{}"]), calls))
    result = fetch(client, url)
    assert "error" not in result, result
    requested = next(args for args, kw in calls if args != "client")
    assert requested == ("GET", url)


def test_only_web_urls_are_fetched(client):
    for url in ["file:///etc/passwd", "data:text/plain,hi", "ftp://example.com/x"]:
        assert "http" in fetch(client, url)["error"], url


def test_a_host_that_cannot_be_reached_is_reported(client, monkeypatch):
    class Unreachable:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        def stream(self, *a, **kw): raise main.httpx.ConnectError("no route")

    monkeypatch.setattr(main.httpx, "AsyncClient", Unreachable)
    assert "Could not reach" in fetch(client, "https://nowhere.test/x")["error"]


# --- what comes back ---------------------------------------------------------

def test_a_call_returns_the_body(client, monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_httpx(FakeResponse([b'{"ok": true}'])))
    result = fetch(client, "https://api.test/data")
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"ok": True}
    assert not result["truncated"]


def test_the_call_is_anonymous_and_follows_redirects(client, monkeypatch):
    # Plenty of APIs redirect to a canonical URL. Any forwarded cookie or auth header
    # would be the browser's, sent somewhere the user never chose.
    calls = []
    monkeypatch.setattr(main.httpx, "AsyncClient", fake_httpx(FakeResponse([b"{}"]), calls))
    fetch(client, "https://api.test/data")

    client_kwargs = next(kw for tag, kw in calls if tag == "client")
    assert client_kwargs["follow_redirects"] is True
    request_kwargs = next(kw for entry, kw in calls if entry != "client")
    sent = {k.lower() for k in request_kwargs.get("headers", {})}
    assert not (sent & {"cookie", "authorization"})


def test_an_oversized_response_is_truncated(client, monkeypatch):
    # An unbounded body would be pasted straight into a prompt, where every character is
    # paid for on a local model.
    monkeypatch.setattr(main, "FETCH_MAX_BYTES", 100)
    monkeypatch.setattr(
        main.httpx, "AsyncClient", fake_httpx(FakeResponse([b"x" * 50] * 10)),
    )
    result = fetch(client, "https://api.test/big")
    assert len(result["body"]) <= 100
    assert result["truncated"]
