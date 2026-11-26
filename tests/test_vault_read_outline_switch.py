"""vault_read's outline switch: a note too big to return whole comes back as its outline.

With VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS set, a plain read of a Markdown note whose reply
would pass it returns the note's outline instead of its content, so a client that caps a
tool's result gets something it can use and reads one section next; `whole` reads past
it. Off (0) by default, and then every read is as before. The switch looks at the reply
it is about to send, not at the note, since the reply's wrapping adds to the note's size.

Section reads, `outline` and `whole` are never switched; neither are vault_batch_read and
the daily note's read. The description names the switch only when it is on, and carries
no number, so changing the threshold leaves the tool's definition as it was.
"""

import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from obsidian_vault_mcp import config, context, server

from ._live_server import child_env


def call_tool(name: str, arguments: dict) -> dict:
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    if isinstance(result, tuple):
        result = result[0]
    return json.loads("".join(getattr(block, "text", "") for block in result))


def read(path: str, **arguments) -> dict:
    return call_tool("vault_read", {"path": path, **arguments})


def raw_read(path: str, **arguments) -> str:
    result = asyncio.run(server.mcp.call_tool("vault_read", {"path": path, **arguments}))
    if isinstance(result, tuple):
        result = result[0]
    return "".join(getattr(block, "text", "") for block in result)


BIG = "---\nstatus: active\n---\n# Big\n\nintro\n\n## One\n\n" + "one " * 300 + "\n\n## Two\n\n" + "two " * 100 + "\n"


@pytest.fixture
def big(vault_dir):
    (vault_dir / "big.md").write_text(BIG, encoding="utf-8")
    return "big.md"


@pytest.fixture
def switch(monkeypatch):
    def set_limit(limit: int) -> None:
        monkeypatch.setattr(config, "VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS", limit)

    return set_limit


# --- When it switches ---


def test_with_the_switch_off_a_big_note_comes_back_whole(big):
    assert read(big)["content"] == BIG


def test_just_over_the_limit_the_reply_is_the_outline(big, switch):
    reply_length = len(raw_read(big))
    switch(reply_length - 1)

    result = read(big)

    assert "content" not in result
    assert result["over_limit"] is True
    assert result["limit"] == reply_length - 1


def test_at_the_limit_the_note_comes_back_whole(big, switch):
    switch(len(raw_read(big)))

    assert read(big)["content"] == BIG


def test_the_limit_is_on_the_reply_not_on_the_note(vault_dir, switch):
    # JSON escapes quotes and newlines, so the reply passes the limit before the note does.
    note = '# Quoted\n' + '"q"\n' * 200
    (vault_dir / "quoted.md").write_text(note, encoding="utf-8")
    switch(len(note) + 10)

    assert len(raw_read("quoted.md", whole=True)) > len(note) + 10
    assert read("quoted.md")["over_limit"] is True


def test_the_outline_the_switch_sends(big, switch):
    switch(1000)

    result = read(big)

    assert list(result) == ["path", "over_limit", "limit", "next", "size", "outline", "frontmatter", "metadata"]
    assert result["path"] == "big.md"
    assert result["limit"] == 1000
    assert "heading" in result["next"] and "whole=true" in result["next"]
    assert result["size"] == {"chars": len(BIG), "lines": len(BIG.split("\n")) - 1}
    assert [e["heading"] for e in result["outline"]] == ["# Big", "## One", "## Two"]
    assert result["frontmatter"] == {"status": "active"}


def test_a_section_over_the_limit_is_flagged_in_the_outline(big, switch):
    switch(1000)

    entries = {e["heading"]: e for e in read(big)["outline"]}

    assert entries["# Big"]["over_limit"] is True
    assert entries["## One"]["over_limit"] is True
    assert entries["## One"]["chars"] > 1000
    assert "over_limit" not in entries["## Two"]
    assert entries["## Two"]["chars"] <= 1000


def test_a_section_is_flagged_by_its_reply_not_its_text(vault_dir, switch):
    # Quotes and newlines are escaped in the reply, so it passes the limit before the text does.
    note = "# Top\n## Quoted\n" + '"q"\n' * 300 + "## Next\n"
    (vault_dir / "quoted.md").write_text(note, encoding="utf-8")
    switch(1500)

    entry = next(e for e in read("quoted.md", outline=True)["outline"] if e["heading"] == "## Quoted")

    assert entry["chars"] < 1500
    assert entry["over_limit"] is True
    assert len(raw_read("quoted.md", heading="## Quoted")) > 1500


def test_each_flag_says_whether_that_sections_reply_passes_the_limit(big, switch):
    switch(800)

    for entry in read(big, outline=True)["outline"]:
        reply = raw_read(big, heading=entry["heading"])
        assert entry.get("over_limit", False) == (len(reply) > 800), entry


TRICKY = (
    '﻿# Top "quoted" with a back\\slash\n\tA tab, a control \x01, é and 😀\n'
    '## Child "two"\nline "q" \\ end\n### Deep\nthe last line, no newline'
)


def test_each_flag_is_exact_at_its_sections_reply_length(vault_dir, switch):
    # The flag adds a section's reply length up from its parts; it must equal the reply itself,
    # escapes and all, so a section is flagged one character past its limit and not at it.
    (vault_dir / "tricky.md").write_text(TRICKY, encoding="utf-8")
    entries = read("tricky.md", outline=True)["outline"]
    assert [e["heading"] for e in entries] == ['# Top "quoted" with a back\\slash', '## Child "two"', "### Deep"]

    for entry in entries:
        length = len(raw_read("tricky.md", heading=entry["heading"]))
        switch(length)
        assert "over_limit" not in next(e for e in read("tricky.md", outline=True)["outline"] if e["line"] == entry["line"])
        switch(length - 1)
        assert next(e for e in read("tricky.md", outline=True)["outline"] if e["line"] == entry["line"])["over_limit"]
        switch(0)


def test_a_long_parent_over_many_children_is_flagged_in_time(vault_dir, switch):
    # Serialising each child's reply would repeat the parent's heading in every path.
    (vault_dir / "wide.md").write_text("# " + "P" * 200_000 + "\n" + "## c\nx\n" * 50_000, encoding="utf-8")
    switch(40_000)

    started = time.perf_counter()
    outline = read("wide.md", outline=True)["outline"]

    assert time.perf_counter() - started < 15
    # Every child's reply carries the parent's heading in its path, so each passes the limit too.
    assert len(outline) == 50_001 and all(entry["over_limit"] for entry in outline)


def test_an_outline_no_smaller_than_the_note_is_not_sent(vault_dir, switch):
    # A note of little but headings, a log say: its outline would outweigh the note it replaces.
    note = "".join(f"### 10:{minute % 60:02d}\nok\n" for minute in range(3_500))
    (vault_dir / "log.md").write_text(note, encoding="utf-8")
    switch(40_000)

    assert len(raw_read("log.md", whole=True)) > 40_000
    assert len(raw_read("log.md", outline=True)) > len(raw_read("log.md", whole=True))
    assert read("log.md")["content"] == note


def test_with_the_switch_off_no_entry_is_flagged(big):
    assert all("over_limit" not in e for e in read(big, outline=True)["outline"])


def test_a_note_with_no_headings_comes_back_whole(vault_dir, switch):
    note = "plain text\n" * 200
    (vault_dir / "plain.md").write_text(note, encoding="utf-8")
    switch(100)

    assert read("plain.md")["content"] == note


def test_a_file_that_is_not_markdown_comes_back_whole(vault_dir, switch):
    text = "# Looks like a heading\n" + "x" * 2000 + "\n"
    (vault_dir / "notes.txt").write_text(text, encoding="utf-8")
    switch(100)

    assert read("notes.txt")["content"] == text


# --- What it never switches ---


def test_whole_reads_past_the_limit(big, switch):
    switch(1000)

    result = read(big, whole=True)

    assert list(result) == ["path", "content", "metadata", "frontmatter"]
    assert result["content"] == BIG


@pytest.mark.parametrize("limit", [0, 10_000_000])
def test_whole_is_a_plain_read_under_the_limit_or_with_the_switch_off(big, switch, limit):
    switch(limit)

    assert read(big, whole=True) == read(big)


def test_a_section_read_is_not_switched(big, switch):
    switch(100)

    result = read(big, heading="## One")

    assert result["content"].startswith("## One\n") and len(result["content"]) > 100


def test_an_outline_asked_for_carries_no_switch_keys(big, switch):
    switch(100)

    result = read(big, outline=True)

    assert list(result) == ["path", "size", "outline", "frontmatter", "metadata"]
    assert all(e.get("over_limit") for e in result["outline"])


def test_with_the_switch_off_heading_and_outline_still_work(big):
    assert read(big, heading="## Two")["content"].startswith("## Two\n")
    assert [e["heading"] for e in read(big, outline=True)["outline"]] == ["# Big", "## One", "## Two"]


def test_vault_batch_read_is_not_switched(big, switch):
    switch(100)

    result = call_tool("vault_batch_read", {"paths": [big]})

    assert result["files"][0]["content"] == BIG


def test_the_daily_note_read_is_not_switched(vault_dir, switch, monkeypatch):
    monkeypatch.setattr(config, "VAULT_DAILY_NOTES_FOLDER", "")
    monkeypatch.setattr(config, "VAULT_DAILY_NOTES_FORMAT", "%Y-%m-%d")
    (vault_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md").write_text(BIG, encoding="utf-8")
    switch(100)

    assert call_tool("vault_daily_note_read", {})["content"] == BIG


def test_a_repeat_no_path_tells_apart_points_to_whole(vault_dir):
    (vault_dir / "log.md").write_text("# Log\n## Entry\none\n## Entry\ntwo\n", encoding="utf-8")

    assert "whole=true" in read("log.md", heading="## Entry")["error"]


# --- Inputs and the schema ---


@pytest.mark.parametrize(
    "arguments, named",
    [
        ({"heading": "## One", "whole": True}, "'heading', 'whole'"),
        ({"outline": True, "whole": True}, "'outline', 'whole'"),
        ({"heading": "## One", "outline": True, "whole": True}, "'heading', 'outline', 'whole'"),
    ],
)
def test_whole_with_another_way_to_read_is_refused(big, arguments, named):
    with pytest.raises(ToolError) as excinfo:
        read(big, **arguments)

    assert f"Use only one of {named}, not several" in str(excinfo.value)


@pytest.mark.parametrize(
    "value",
    [pytest.param("maybe", id="string"), pytest.param(["x"], id="list"), pytest.param(2, id="number")],
)
def test_a_whole_that_is_not_a_boolean_is_refused(big, value):
    with pytest.raises(ToolError) as excinfo:
        read(big, whole=value)

    assert "whole" in str(excinfo.value)


def test_whole_is_in_the_tool_schema_as_a_boolean_defaulting_to_false(vault_dir):
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "vault_read")
    schema = tool.inputSchema

    assert schema["properties"]["whole"]["type"] == "boolean"
    assert schema["properties"]["whole"]["default"] is False
    assert schema["required"] == ["path"]


# --- The setting ---


@pytest.mark.parametrize("raw", ["40k", "40,000", "4.5", "-1", "off"])
def test_a_bad_threshold_stops_startup_and_names_the_variable(vault_dir, monkeypatch, raw):
    monkeypatch.setenv("VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS", raw)

    with pytest.raises(ValueError, match="VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS"):
        config.validate_config()


@pytest.mark.parametrize("raw", ["", "0", "40000", " 40000 "])
def test_a_good_threshold_or_none_starts(vault_dir, monkeypatch, raw):
    monkeypatch.setenv("VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS", raw)

    config.validate_config()  # must not raise


# --- The description ---


@pytest.mark.parametrize("threshold", [0, 40_000])
def test_both_descriptions_are_short_and_carry_no_number(threshold):
    description = server.vault_read_description(threshold)

    assert len(description) < 2048
    assert not any(character.isdigit() for character in description)
    assert description.startswith(
        "Read a file from the Obsidian vault, returning content, metadata, and parsed YAML frontmatter. "
    )
    assert "Pass heading" in description and "Set outline=true" in description


def test_the_description_names_the_switch_and_whole_only_when_on():
    on, off = server.vault_read_description(40_000), server.vault_read_description(0)

    assert "too big to return whole" in on and "Set whole=true" in on
    assert "too big" not in off and "whole" not in off
    assert server.vault_read_description(1) == server.vault_read_description(150_000) == on


@pytest.mark.parametrize(
    "setting, threshold",
    [pytest.param(None, 0, id="without-the-setting"), pytest.param("40000", 40_000, id="with-it")],
)
def test_a_server_started_with_or_without_the_setting(tmp_path, setting, threshold):
    # The setting and the description are read at import, so each case starts its own process,
    # from an environment built from scratch: a value exported in the shell can't reach it.
    vault = tmp_path / "vault"
    vault.mkdir()
    script = (
        "import asyncio, json\n"
        "from obsidian_vault_mcp import config, server\n"
        "tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == 'vault_read')\n"
        "print(json.dumps([config.VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS, tool.description]))\n"
    )
    extra = {} if setting is None else {"VAULT_MCP_READ_OUTLINE_THRESHOLD_CHARS": setting}
    env = child_env(tmp_path / "home", vault, 0, extra)

    done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == [threshold, server.vault_read_description(threshold)]


# --- The audit ---


@pytest.fixture
def audit_log(tmp_path, monkeypatch, vault_dir):
    log_path = tmp_path / "audit" / "reads.jsonl"
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_PATH", str(log_path))
    monkeypatch.setattr(config, "VAULT_AUDIT_LOG_INCLUDE_READS", True)
    token = context.set_request_context(principal="test-principal", request_id="req-1", client="pytest")
    yield log_path
    context.reset_request_context(token)


def test_an_outline_sent_by_the_switch_is_audited_as_a_successful_read(big, switch, audit_log):
    switch(1000)
    read(big)

    (record,) = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines() if line]
    assert record["operation"] == "vault_read"
    assert record["target_path"] == big
    assert record["operation_status"] == "success"
