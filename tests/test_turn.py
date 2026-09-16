"""The /turn contract, driven by a stubbed model.

Everything here is about what the backend does with a reply, not what a model would say,
so a stub is not a weaker test -- it is the only way to pin down the failure cases that
matter (a truncated reply, a fenced reply, a reply that would wipe the page) without
waiting for a real model to produce one by chance.
"""

import json

import pytest
from fastapi.testclient import TestClient

from backend import main
from tests.fake_ollama import FakeOllama


@pytest.fixture
def app(monkeypatch):
    with FakeOllama() as fake:
        monkeypatch.setattr(main, "OLLAMA_URL", fake.url)
        yield fake, TestClient(main.app)


def events(client, **payload):
    payload.setdefault("action", {})
    with client.stream("POST", "/turn", json=payload) as resp:
        assert resp.status_code == 200
        return [json.loads(line) for line in resp.iter_lines() if line.strip()]


def of_type(evs, kind):
    return [e for e in evs if e["type"] == kind]


# --- the first screen -------------------------------------------------------

def test_shell_streams_the_body_before_it_is_finished(app):
    fake, client = app
    fake.default = "<section data-region='a'>" + "hi " * 40 + "</section>"
    evs = events(client, mode="shell", concept="a test app")
    deltas = of_type(evs, "screen_delta")
    assert len(deltas) > 1, "content arriving in one piece is not being streamed"
    assert "".join(d["text"] for d in deltas).startswith("<section")


def test_shell_drops_commentary_before_the_content(app):
    fake, client = app
    fake.default = 'Sure! Here is the HTML:\n<section data-region="a">hi</section>'
    text = "".join(d["text"] for d in of_type(events(client, mode="shell"), "screen_delta"))
    assert text.startswith("<section")
    assert "Sure!" not in text


def test_shell_unwraps_a_full_document_if_it_gets_one(app):
    # The model is asked for body content; when it writes a whole document anyway, the
    # doctype and head must not end up inside the body the app already opened.
    fake, client = app
    fake.default = ("<!DOCTYPE html><html><head><title>x</title></head><body>"
                    '<section data-region="a">hi</section></body></html>')
    text = "".join(d["text"] for d in of_type(events(client, mode="shell"), "screen_delta"))
    assert text.startswith("<section")
    assert "<head" not in text and "</html>" not in text


def test_shell_keeps_a_closing_fence_off_the_page(app):
    # The document is streamed straight into the iframe's parser, so a trailing fence
    # would otherwise be rendered as visible text at the bottom of the finished app.
    fake, client = app
    fake.default = '```html\n<section data-region="a">hi</section>\n```\n'
    text = "".join(d["text"] for d in of_type(events(client, mode="shell"), "screen_delta"))
    assert "```" not in text
    assert text.rstrip().endswith("</section>")


def test_shell_shows_something_when_the_model_ignores_the_format(app):
    fake, client = app
    fake.default = "I am not going to write HTML today."
    text = "".join(d["text"] for d in of_type(events(client, mode="shell"), "screen_delta"))
    assert "not going to write HTML" in text
    assert "data-region" in text, "unstructured output still has to be addressable"


# --- patches ----------------------------------------------------------------

PATCH = """#plan Show the Siamese photos
#region photo-grid
<div class="card">Siamese</div>
#end"""


def test_patch_returns_only_the_named_region(app):
    fake, client = app
    fake.default = PATCH
    evs = events(client, mode="patch", regions=[{"id": "photo-grid", "html": "<p>old</p>"}])
    assert [e["id"] for e in of_type(evs, "region")] == ["photo-grid"]
    assert of_type(evs, "plan")[0]["text"] == "Show the Siamese photos"


def test_each_region_is_emitted_before_the_next_one_is_written(app):
    # Regions land on screen as they finish rather than when the response does, so the
    # order of events, not just their contents, is part of the contract.
    fake, client = app
    fake.default = ("#plan two\n#region one\n<p>1</p>\n#region two\n<p>2</p>\n#end")
    kinds = [(e["type"], e.get("id")) for e in events(client, mode="patch") if e["type"] in ("plan", "region")]
    assert kinds == [("plan", None), ("region", "one"), ("region", "two")]


def test_a_final_region_survives_a_missing_end_marker(app):
    fake, client = app
    fake.default = "#plan truncated\n#region results\n<p>content</p>"
    assert [e["id"] for e in of_type(events(client, mode="patch"), "region")] == ["results"]


def test_compaction_markers_never_reach_the_page(app):
    # Small models treat the elided screen as a template and echo the marker straight
    # back, which would render as a stray comment where content should be.
    fake, client = app
    fake.default = ("#plan copy\n#region results\n<p>a</p>\n"
                    "<!-- and 5 more <li> like those above: WRITE THEM ALL OUT IN FULL -->\n#end")
    html = of_type(events(client, mode="patch"), "region")[0]["html"]
    assert "more" not in html and "<p>a</p>" in html


def test_fences_inside_a_patch_are_ignored(app):
    fake, client = app
    fake.default = "#plan fenced\n#region results\n```html\n<p>hi</p>\n```\n#end"
    assert of_type(events(client, mode="patch"), "region")[0]["html"] == "<p>hi</p>"


def test_a_turn_that_produces_nothing_is_tried_again(app):
    # A plan with no region under it means the click did nothing at all, and the user
    # cannot tell a broken control from a slow one. It costs almost no tokens to fail
    # this way, so it costs almost nothing to ask again.
    fake, client = app
    replies = iter(["#plan I will show the results\n#end", PATCH])
    fake.default = lambda _: next(replies, PATCH)
    evs = events(client, mode="patch", regions=[{"id": "photo-grid", "html": "<p>old</p>"}])
    assert [e["id"] for e in of_type(evs, "region")] == ["photo-grid"]
    assert len(fake.requests) == 2, "the empty turn was not retried"


def test_a_turn_that_produces_content_is_not_retried(app):
    fake, client = app
    fake.default = PATCH
    events(client, mode="patch", regions=[{"id": "photo-grid", "html": "<p>old</p>"}])
    assert len(fake.requests) == 1


# --- the failure that destroys work ----------------------------------------

def test_a_screen_swap_without_regions_cannot_wipe_the_page(app):
    # A #screen replaces the whole body. A reply with no regions in it is a region's
    # worth of content mislabelled, and trusting it blanks the app.
    fake, client = app
    fake.default = "#screen\n<h2>About</h2><p>hello</p>\n#end"
    evs = events(client, mode="patch", regions=[
        {"id": "nav", "html": "x" * 10},
        {"id": "main", "html": "y" * 500},
    ])
    assert not of_type(evs, "screen")
    demoted = of_type(evs, "region")[0]
    assert demoted["id"] == "main", "demoted content belongs in the biggest region"


def test_a_real_screen_swap_still_works(app):
    fake, client = app
    fake.default = '#screen\n<section data-region="a">new</section>\n#end'
    evs = events(client, mode="patch", regions=[{"id": "old", "html": "x"}])
    assert len(of_type(evs, "screen")) == 1


def test_a_body_wrapper_is_stripped_from_a_screen_swap(app):
    fake, client = app
    fake.default = '#screen\n<body><section data-region="a">new</section></body>\n#end'
    html = of_type(events(client, mode="patch", regions=[{"id": "o", "html": "x"}]), "screen")[0]["html"]
    assert not html.lower().startswith("<body")


def test_an_unreachable_model_is_reported_not_swallowed(app, monkeypatch):
    # Forgetting to start Ollama is the most likely failure in this whole app; it must
    # not look like a truncated stream with nothing to show for it.
    fake, client = app
    monkeypatch.setattr(main, "OLLAMA_URL", "http://127.0.0.1:1")
    message = of_type(events(client, mode="patch"), "error")[0]["message"]
    assert "Ollama" in message


def test_a_model_error_response_is_reported(app):
    fake, client = app
    fake.fail_with = "model 'nope' not found"
    assert "nope" in of_type(events(client, mode="patch"), "error")[0]["message"]


# --- what the model is actually told ---------------------------------------

def test_the_prompt_carries_region_markup_not_a_summary(app):
    fake, client = app
    fake.default = PATCH
    events(client, mode="patch",
           regions=[{"id": "grid", "html": '<div class="card"><h2>Siamese</h2></div>'}],
           styles=".card{border:1px solid}", concept="cats",
           action={"event": "click", "elementData": {"text": "Next"}})
    prompt = "\n".join(m["content"] for m in fake.requests[-1]["messages"])
    assert 'class="card"' in prompt, "the patch must see the markup it has to match"
    assert ".card{border:1px solid}" in prompt
    assert "cats" in prompt and "Next" in prompt


def test_the_first_screen_prompt_carries_the_concept(app):
    fake, client = app
    fake.default = '<section data-region="a">x</section>' 
    events(client, mode="shell", concept="a test app")
    prompt = "\n".join(m["content"] for m in fake.requests[-1]["messages"])
    assert "a test app" in prompt
