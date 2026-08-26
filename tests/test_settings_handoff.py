"""Tests for the session hand-off settings fields added to settings.py."""

from claude_monitor.settings import (
    FIELD_DEFS,
    HANDOFF_TRANSPORTS,
    Settings,
    load_settings,
    save_settings,
)

HANDOFF_FIELDS = [
    "handoff_enabled",
    "handoff_capture_on_session_end",
    "handoff_capture_idle_mins",
    "handoff_llm_enabled",
    "handoff_llm_transport",
    "handoff_model",
    "handoff_llm_timeout_secs",
    "handoff_inject_on_start",
    "handoff_inject_max_age_hours",
    "handoff_markdown_enabled",
    "handoff_retain_per_project",
]


class TestHandoffDefaults:
    def test_defaults(self):
        s = Settings()
        assert s.handoff_enabled is False
        assert s.handoff_capture_on_session_end is True
        assert s.handoff_capture_idle_mins == 0
        assert s.handoff_llm_enabled is False
        assert s.handoff_llm_transport == "minimax"
        assert s.handoff_model == ""
        assert s.handoff_llm_timeout_secs == 30
        assert s.handoff_inject_on_start is False
        assert s.handoff_inject_max_age_hours == 72
        assert s.handoff_markdown_enabled is True
        assert s.handoff_retain_per_project == 5


class TestHandoffClamps:
    def test_capture_idle_mins_low(self):
        assert Settings(handoff_capture_idle_mins=-5).handoff_capture_idle_mins == 0

    def test_capture_idle_mins_high(self):
        assert Settings(handoff_capture_idle_mins=9999).handoff_capture_idle_mins == 1440

    def test_capture_idle_mins_bounds(self):
        assert Settings(handoff_capture_idle_mins=0).handoff_capture_idle_mins == 0
        assert Settings(handoff_capture_idle_mins=1440).handoff_capture_idle_mins == 1440

    def test_llm_timeout_secs_low(self):
        assert Settings(handoff_llm_timeout_secs=0).handoff_llm_timeout_secs == 5

    def test_llm_timeout_secs_high(self):
        assert Settings(handoff_llm_timeout_secs=99999).handoff_llm_timeout_secs == 300

    def test_llm_timeout_secs_bounds(self):
        assert Settings(handoff_llm_timeout_secs=5).handoff_llm_timeout_secs == 5
        assert Settings(handoff_llm_timeout_secs=300).handoff_llm_timeout_secs == 300

    def test_inject_max_age_hours_low(self):
        assert Settings(handoff_inject_max_age_hours=0).handoff_inject_max_age_hours == 1

    def test_inject_max_age_hours_high(self):
        assert Settings(handoff_inject_max_age_hours=100000).handoff_inject_max_age_hours == 8760

    def test_inject_max_age_hours_bounds(self):
        assert Settings(handoff_inject_max_age_hours=1).handoff_inject_max_age_hours == 1
        assert Settings(handoff_inject_max_age_hours=8760).handoff_inject_max_age_hours == 8760

    def test_retain_per_project_low(self):
        assert Settings(handoff_retain_per_project=0).handoff_retain_per_project == 1

    def test_retain_per_project_high(self):
        assert Settings(handoff_retain_per_project=500).handoff_retain_per_project == 100

    def test_retain_per_project_bounds(self):
        assert Settings(handoff_retain_per_project=1).handoff_retain_per_project == 1
        assert Settings(handoff_retain_per_project=100).handoff_retain_per_project == 100


class TestHandoffTransportValidation:
    def test_invalid_transport_falls_back(self):
        assert Settings(handoff_llm_transport="invalid").handoff_llm_transport == "minimax"

    def test_valid_transports_accepted(self):
        for transport in HANDOFF_TRANSPORTS:
            assert Settings(handoff_llm_transport=transport).handoff_llm_transport == transport

    def test_empty_string_falls_back(self):
        assert Settings(handoff_llm_transport="").handoff_llm_transport == "minimax"


class TestHandoffPersistence:
    def test_round_trip(self, isolated_state):
        s = Settings(
            handoff_enabled=True,
            handoff_capture_on_session_end=False,
            handoff_capture_idle_mins=15,
            handoff_llm_enabled=True,
            handoff_llm_transport="openai",
            handoff_model="gpt-4o-mini",
            handoff_llm_timeout_secs=45,
            handoff_inject_on_start=True,
            handoff_inject_max_age_hours=24,
            handoff_markdown_enabled=False,
            handoff_retain_per_project=10,
        )
        save_settings(s)
        loaded = load_settings()

        assert loaded.handoff_enabled is True
        assert loaded.handoff_capture_on_session_end is False
        assert loaded.handoff_capture_idle_mins == 15
        assert loaded.handoff_llm_enabled is True
        assert loaded.handoff_llm_transport == "openai"
        assert loaded.handoff_model == "gpt-4o-mini"
        assert loaded.handoff_llm_timeout_secs == 45
        assert loaded.handoff_inject_on_start is True
        assert loaded.handoff_inject_max_age_hours == 24
        assert loaded.handoff_markdown_enabled is False
        assert loaded.handoff_retain_per_project == 10

    def test_round_trip_defaults(self, isolated_state):
        save_settings(Settings())
        loaded = load_settings()
        for field in HANDOFF_FIELDS:
            assert getattr(loaded, field) == getattr(Settings(), field)


class TestHandoffFieldDefs:
    def test_every_handoff_field_has_a_field_def(self):
        defined_names = {fd["name"] for fd in FIELD_DEFS}
        for field in HANDOFF_FIELDS:
            assert field in defined_names, f"{field} missing a FIELD_DEFS entry"

    def test_field_def_names_are_real_dataclass_fields(self):
        dataclass_fields = set(Settings.__dataclass_fields__)
        for fd in FIELD_DEFS:
            assert fd["name"] in dataclass_fields, f"{fd['name']} is not a Settings field"

    def test_handoff_llm_transport_uses_select_widget(self):
        fd = next(fd for fd in FIELD_DEFS if fd["name"] == "handoff_llm_transport")
        assert fd["widget_type"] == "select"
        values = {v for _, v in fd["options"]}
        assert values == set(HANDOFF_TRANSPORTS)

    def test_handoff_model_uses_input_widget(self):
        fd = next(fd for fd in FIELD_DEFS if fd["name"] == "handoff_model")
        assert fd["widget_type"] == "input"
        assert fd["placeholder"] == "leave blank for provider default"

    def test_integer_fields_use_integer_input(self):
        int_fields = {
            "handoff_capture_idle_mins",
            "handoff_llm_timeout_secs",
            "handoff_inject_max_age_hours",
            "handoff_retain_per_project",
        }
        for fd in FIELD_DEFS:
            if fd["name"] in int_fields:
                assert fd["widget_type"] == "input"
                assert fd.get("input_type") == "integer"

    def test_boolean_fields_use_switch_widget(self):
        bool_fields = {
            "handoff_enabled",
            "handoff_capture_on_session_end",
            "handoff_llm_enabled",
            "handoff_inject_on_start",
            "handoff_markdown_enabled",
        }
        for fd in FIELD_DEFS:
            if fd["name"] in bool_fields:
                assert fd["widget_type"] == "switch"

    def test_handoff_llm_enabled_warns_about_third_party_data(self):
        fd = next(fd for fd in FIELD_DEFS if fd["name"] == "handoff_llm_enabled")
        desc = (fd.get("description") or "").lower()
        assert "third-party" in desc or "third party" in desc

    def test_handoff_capture_idle_mins_notes_zero_disables(self):
        fd = next(fd for fd in FIELD_DEFS if fd["name"] == "handoff_capture_idle_mins")
        desc = (fd.get("description") or "").lower()
        assert "0" in desc and "disable" in desc

    def test_handoff_inject_on_start_notes_new_session_behavior(self):
        fd = next(fd for fd in FIELD_DEFS if fd["name"] == "handoff_inject_on_start")
        desc = (fd.get("description") or "").lower()
        assert "new session" in desc
