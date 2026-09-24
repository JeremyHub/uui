"""Claude through the Claude Code CLI, translated into what /chat already speaks.

The CLI is replaced by a script that prints the events `claude -p --output-format
stream-json` does, so these run offline and spend nothing.
"""

import asyncio
import json
import stat
import textwrap

import pytest

from backend import claude_code


def fake_cli(tmp_path, events, *, hang=False, exit_code=0):
    """A stand-in `claude` that records its arguments and stdin, then prints `events`."""
    script = tmp_path / "claude"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, os, sys, time
        with open({str(tmp_path / "call.json")!r}, "w") as f:
            json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
                        "max": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
                        "pid": os.getpid()}}, f)
        for e in {events!r}:
            print(json.dumps(e), flush=True)
        if {hang!r}:
            time.sleep(60)
        sys.exit({exit_code})
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def text(t):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}}}


OK = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}

PAYLOAD = {
    "model": "claude:haiku",
    "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}],
    "stream": True,
    "options": {"num_predict": 123, "temperature": 0.4},
}


async def collect(payload=PAYLOAD):
    return [json.loads(line) async for line in claude_code.stream_chat(payload)]


@pytest.mark.asyncio
async def test_text_arrives_in_the_format_the_engine_already_reads(tmp_path, monkeypatch):
    thinking = {"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}}
    monkeypatch.setattr(claude_code, "CLAUDE_BIN", str(fake_cli(
        tmp_path, [{"type": "system", "subtype": "init"}, thinking, text("#plan x\n"), text("#end"), OK])))
    lines = await collect()
    assert [l["message"]["content"] for l in lines if not l.get("done")] == ["#plan x\n", "#end"], (
        "only reply text belongs in the page -- not thinking, not the CLI's own events"
    )
    assert lines[-1]["done"] is True


@pytest.mark.asyncio
async def test_the_app_prompt_replaces_claude_codes_own_and_nothing_else_joins_in(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "CLAUDE_BIN", str(fake_cli(tmp_path, [OK])))
    await collect()
    call = json.loads((tmp_path / "call.json").read_text())
    argv = call["argv"]
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--tools") + 1] == "", "a page generator has no business running tools"
    assert argv[argv.index("--setting-sources") + 1] == "", "the user's hooks and plugins joined in"
    assert "--no-session-persistence" in argv
    assert call["stdin"] == "USER"
    assert call["max"] == "123", "the app's output cap was dropped"


@pytest.mark.asyncio
async def test_a_failure_is_reported_rather_than_an_empty_reply(tmp_path, monkeypatch):
    failed = {"type": "result", "subtype": "success", "is_error": True,
              "result": "Not logged in · Please run /login"}
    monkeypatch.setattr(claude_code, "CLAUDE_BIN", str(fake_cli(tmp_path, [failed])))
    assert "Not logged in" in (await collect())[-1]["error"]


@pytest.mark.asyncio
async def test_a_cli_that_dies_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_code, "CLAUDE_BIN", str(fake_cli(tmp_path, [], exit_code=3)))
    assert "exited (3)" in (await collect())[-1]["error"]


@pytest.mark.asyncio
async def test_an_abandoned_reply_stops_the_cli(tmp_path, monkeypatch):
    # The app aborts a guess the moment the user does something else. On a metered model
    # a CLI left running would keep spending the user's usage on a reply nobody reads.
    monkeypatch.setattr(claude_code, "CLAUDE_BIN", str(fake_cli(tmp_path, [text("a")], hang=True)))
    stream = claude_code.stream_chat(PAYLOAD)
    await stream.__anext__()
    await stream.aclose()
    pid = json.loads((tmp_path / "call.json").read_text())["pid"]
    await asyncio.sleep(0.1)
    import os
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_claude_models_are_offered_only_when_the_cli_is_there(monkeypatch):
    monkeypatch.setattr(claude_code, "CLAUDE_BIN", "")
    assert claude_code.models() == []
    monkeypatch.setattr(claude_code, "CLAUDE_BIN", "/usr/bin/claude")
    ids = [m["id"] for m in claude_code.models()]
    assert ids and all(i.startswith("claude:") for i in ids)
    assert all(m["metered"] for m in claude_code.models())
