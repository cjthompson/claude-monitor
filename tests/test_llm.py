"""Tests for claude_monitor.llm — no real network calls are made."""

import io
import json
import subprocess
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from claude_monitor import llm


def _fake_response(payload: dict, status: int = 200):
    body = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


def _happy_payload(content="Hello there"):
    return {"choices": [{"message": {"content": content}}]}


# --- request shape -----------------------------------------------------


@patch("urllib.request.urlopen")
def test_minimax_request_shape(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "secret-key-123")
    mock_urlopen.return_value = _fake_response(_happy_payload())

    result = llm.complete("hi there", system="be nice", transport="minimax")

    assert result == "Hello there"
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.minimax.io/v1/chat/completions"
    assert req.get_header("Authorization") == "Bearer secret-key-123"
    assert req.get_header("Content-type") == "application/json"

    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "MiniMax-M3"
    assert body["messages"] == [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "hi there"},
    ]
    assert body["max_tokens"] == 800
    assert body["thinking"] == {"type": "disabled"}


@patch("urllib.request.urlopen")
def test_openai_request_shape_no_system(mock_urlopen, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
    mock_urlopen.return_value = _fake_response(_happy_payload())

    llm.complete("just a prompt", transport="openai")

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://api.openai.com/v1/chat/completions"
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "gpt-5-mini"
    assert body["messages"] == [{"role": "user", "content": "just a prompt"}]
    assert "thinking" not in body


# --- model defaulting ----------------------------------------------------


@patch("urllib.request.urlopen")
def test_model_override(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    mock_urlopen.return_value = _fake_response(_happy_payload())

    llm.complete("hi", transport="minimax", model="custom-model")

    req = mock_urlopen.call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "custom-model"


@patch("urllib.request.urlopen")
def test_max_tokens_passed_through(mock_urlopen, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    mock_urlopen.return_value = _fake_response(_happy_payload())

    llm.complete("hi", transport="openai", max_tokens=42)

    req = mock_urlopen.call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    assert body["max_tokens"] == 42


# --- missing API key -------------------------------------------------------


def test_missing_api_key_raises_and_hides_no_key(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("hi", transport="minimax")

    assert "MINIMAX_API_KEY" in str(excinfo.value)


def test_missing_api_key_empty_string(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")

    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("hi", transport="openai")

    assert "OPENAI_API_KEY" in str(excinfo.value)


@patch("urllib.request.urlopen")
def test_api_key_never_leaked_in_error_message(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "super-secret-value")
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="https://api.minimax.io/v1/chat/completions",
        code=500,
        msg="Server Error",
        hdrs=None,
        fp=io.BytesIO(b"internal error"),
    )

    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("hi", transport="minimax")

    assert "super-secret-value" not in str(excinfo.value)


# --- HTTP errors ------------------------------------------------------------


@patch("urllib.request.urlopen")
def test_http_401_raises_llm_error(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="https://api.minimax.io/v1/chat/completions",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"error": "bad key"}'),
    )

    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("hi", transport="minimax")

    assert "401" in str(excinfo.value)


@patch("urllib.request.urlopen")
def test_http_500_raises_llm_error(mock_urlopen, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    mock_urlopen.side_effect = urllib.error.HTTPError(
        url="https://api.openai.com/v1/chat/completions",
        code=500,
        msg="Server Error",
        hdrs=None,
        fp=io.BytesIO(b"oops"),
    )

    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("hi", transport="openai")

    assert "500" in str(excinfo.value)


# --- MiniMax base_resp error -------------------------------------------------


@patch("urllib.request.urlopen")
def test_minimax_base_resp_error(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    payload = {
        "base_resp": {"status_code": 1004, "status_msg": "invalid api key"},
    }
    mock_urlopen.return_value = _fake_response(payload)

    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("hi", transport="minimax")

    assert "invalid api key" in str(excinfo.value)


@patch("urllib.request.urlopen")
def test_minimax_base_resp_zero_is_ok(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    payload = {
        "base_resp": {"status_code": 0, "status_msg": "success"},
        **_happy_payload("fine"),
    }
    mock_urlopen.return_value = _fake_response(payload)

    result = llm.complete("hi", transport="minimax")
    assert result == "fine"


# --- malformed responses -----------------------------------------------------


@patch("urllib.request.urlopen")
def test_malformed_json_raises(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"not json{{"
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    mock_urlopen.return_value = mock_resp

    with pytest.raises(llm.LLMError):
        llm.complete("hi", transport="minimax")


@patch("urllib.request.urlopen")
def test_unexpected_shape_raises(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    mock_urlopen.return_value = _fake_response({"choices": []})

    with pytest.raises(llm.LLMError):
        llm.complete("hi", transport="minimax")


@patch("urllib.request.urlopen")
def test_missing_choices_key_raises(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    mock_urlopen.return_value = _fake_response({"unexpected": "shape"})

    with pytest.raises(llm.LLMError):
        llm.complete("hi", transport="minimax")


# --- happy path / content shapes ---------------------------------------------


@patch("urllib.request.urlopen")
def test_happy_path_string_content(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    mock_urlopen.return_value = _fake_response(_happy_payload("plain text reply"))

    result = llm.complete("hi", transport="minimax")
    assert result == "plain text reply"


@patch("urllib.request.urlopen")
def test_happy_path_list_content(mock_urlopen, monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "part one "},
                        {"type": "text", "text": "part two"},
                    ]
                }
            }
        ]
    }
    mock_urlopen.return_value = _fake_response(payload)

    result = llm.complete("hi", transport="minimax")
    assert result == "part one part two"


# --- unknown transport --------------------------------------------------------


def test_unknown_transport_raises():
    with pytest.raises(llm.LLMError):
        llm.complete("hi", transport="not-a-real-provider")


# --- available() --------------------------------------------------------------


def test_available_minimax_env_set(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    assert llm.available("minimax") is True


def test_available_minimax_env_unset(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert llm.available("minimax") is False


def test_available_openai_env_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert llm.available("openai") is True


def test_available_openai_env_unset(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert llm.available("openai") is False


def test_available_unknown_transport():
    assert llm.available("nope") is False


@patch("shutil.which")
def test_available_claude_cli_present(mock_which):
    mock_which.return_value = "/usr/local/bin/claude"
    assert llm.available("claude_cli") is True


@patch("shutil.which")
def test_available_claude_cli_absent(mock_which):
    mock_which.return_value = None
    assert llm.available("claude_cli") is False


# --- claude_cli transport -----------------------------------------------------


@patch("subprocess.run")
def test_claude_cli_success(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=json.dumps({"result": "cli reply"}), stderr=""
    )

    result = llm.complete("hi", transport="claude_cli", system="sys prompt")

    assert result == "cli reply"
    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[0] == "claude"
    assert "--model" in cmd
    assert kwargs["input"] == "sys prompt\n\nhi"
    assert kwargs["timeout"] == 30


@patch("subprocess.run")
def test_claude_cli_falls_back_to_raw_stdout(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout="not json output", stderr=""
    )

    result = llm.complete("hi", transport="claude_cli")
    assert result == "not json output"


@patch("subprocess.run")
def test_claude_cli_nonzero_exit_raises(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=1, stdout="", stderr="boom"
    )

    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete("hi", transport="claude_cli")

    assert "1" in str(excinfo.value)


@patch("subprocess.run")
def test_claude_cli_timeout_raises(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["claude"], timeout=30)

    with pytest.raises(llm.LLMError):
        llm.complete("hi", transport="claude_cli", timeout=30)


@patch("subprocess.run")
def test_claude_cli_model_default(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=json.dumps({"result": "ok"}), stderr=""
    )

    llm.complete("hi", transport="claude_cli")

    cmd = mock_run.call_args[0][0]
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "haiku"


@patch("subprocess.run")
def test_claude_cli_model_override(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=["claude"], returncode=0, stdout=json.dumps({"result": "ok"}), stderr=""
    )

    llm.complete("hi", transport="claude_cli", model="sonnet")

    cmd = mock_run.call_args[0][0]
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "sonnet"
