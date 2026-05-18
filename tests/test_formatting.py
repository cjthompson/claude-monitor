from claude_monitor.formatting import format_event

def test_subagent_stop_shows_last_assistant_message():
    data = {
        "agent_id": "abcdefgh-1234",
        "agent_type": "general-purpose",
        "last_assistant_message": "Here's the result of the analysis.",
    }
    is_paused = lambda sid: False
    get_panel = lambda d: None
    oneline = lambda s, n=0: s

    label, detail = format_event(
        data, "SubagentStop", is_pane_paused=is_paused, get_panel=get_panel, oneline=oneline
    )
    assert label == "[magenta]AGENT-  [/]", f"unexpected label: {label!r}"
    assert "  -> " in detail, f"arrow prefix missing: {detail!r}"
    assert "analysis" in detail, f"last_assistant_message missing from detail: {detail!r}"
    assert "abcdefgh" in detail, f"agent_id truncated missing: {detail!r}"


def test_subagent_stop_no_suffix_when_message_empty():
    data = {
        "agent_id": "abcdefgh-1234",
        "agent_type": "general-purpose",
        "last_assistant_message": "",
    }
    is_paused = lambda sid: False
    get_panel = lambda d: None
    oneline = lambda s, n=0: s

    label, detail = format_event(
        data, "SubagentStop", is_pane_paused=is_paused, get_panel=get_panel, oneline=oneline
    )
    assert label == "[magenta]AGENT-  [/]"
    assert detail == "general-purpose [abcdefgh]", f"unexpected detail: {detail!r}"


def test_subagent_stop_message_truncated_at_100():
    data = {
        "agent_id": "test-id-0000",
        "agent_type": "general-purpose",
        "last_assistant_message": "x" * 200,
    }
    is_paused = lambda sid: False
    get_panel = lambda d: None
    oneline = lambda s, n=0: s

    label, detail = format_event(
        data, "SubagentStop", is_pane_paused=is_paused, get_panel=get_panel, oneline=oneline
    )
    # Message should be truncated to 100 chars + "  -> " prefix (7 chars) = 107 extra chars
    # detail ends with "  -> " + 100 x's
    assert detail.endswith("  -> " + ("x" * 100)), f"truncation failed: {detail[-20:]!r}"


def test_post_tool_use_failure_formats_error():
    data = {
        "tool_name": "Bash",
        "error": {"message": "Command exited with code 1: rm -rf /"},
    }
    is_paused = lambda sid: False
    get_panel = lambda d: None
    oneline = lambda s, n=0: s

    label, detail = format_event(
        data, "PostToolUseFailure", is_pane_paused=is_paused, get_panel=get_panel, oneline=oneline
    )
    assert label == "[bold red]TOOLFAIL[/]", f"unexpected label: {label!r}"
    assert "Bash" in detail, f"tool_name missing from detail: {detail!r}"
    assert "rm -rf" in detail, f"error message missing: {detail!r}"


def test_post_tool_use_failure_string_error():
    data = {
        "tool_name": "WebFetch",
        "error": "Connection refused",
    }
    is_paused = lambda sid: False
    get_panel = lambda d: None
    oneline = lambda s, n=0: s

    label, detail = format_event(
        data, "PostToolUseFailure", is_pane_paused=is_paused, get_panel=get_panel, oneline=oneline
    )
    assert label == "[bold red]TOOLFAIL[/]", f"unexpected label: {label!r}"
    assert "Connection refused" in detail, f"string error missing: {detail!r}"


def test_post_tool_use_failure_oneline_truncation():
    long_msg = (
        "This is a very long error message that definitely exceeds "
        "the eighty character limit and should be truncated"
    )
    data = {
        "tool_name": "Edit",
        "error": {"message": long_msg},
    }
    is_paused = lambda sid: False
    get_panel = lambda d: None

    def oneline(s, n=0):
        return s[:n] if n else s

    label, detail = format_event(
        data, "PostToolUseFailure", is_pane_paused=is_paused, get_panel=get_panel, oneline=oneline
    )
    # oneline is called with max_len=80, so detail should be <= len("Edit  -> ")+80
    arrow_len = len("  -> ")
    detail_msg = detail.split("  -> ", 1)[1] if "  -> " in detail else ""
    assert len(detail_msg) <= 80, (
        f"message not truncated to 80: got {len(detail_msg)} chars in {detail_msg!r}"
    )
    assert len(long_msg) > 80, "test setup: message must be longer than 80 chars"
