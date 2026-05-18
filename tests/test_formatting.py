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

    label, detail = format_event(data, "SubagentStop", is_pane_paused=is_paused, get_panel=get_panel, oneline=oneline)
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

    label, detail = format_event(data, "SubagentStop", is_pane_paused=is_paused, get_panel=get_panel, oneline=oneline)
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

    label, detail = format_event(data, "SubagentStop", is_pane_paused=is_paused, get_panel=get_panel, oneline=oneline)
    # Message should be truncated to 100 chars + "  -> " prefix (7 chars) = 107 extra chars
    # detail ends with "  -> " + 100 x's
    assert detail.endswith("  -> " + ("x" * 100)), f"truncation failed: {detail[-20:]!r}"