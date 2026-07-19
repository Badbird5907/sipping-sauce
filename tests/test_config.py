import pytest

from partyline_llm.config import ConfigurationError, Settings


def base_env(monkeypatch) -> None:
    monkeypatch.setenv("SIP_SERVER", "10.13.37.10")
    monkeypatch.setenv("SIP_USERNAME", "199")
    monkeypatch.setenv("SIP_PASSWORD", "secret")
    monkeypatch.setenv("SIP_LOCAL_IP", "10.13.37.11")
    for name in (
        "REALTIME_PROVIDER",
        "XAI_API_KEY",
        "OPENAI_API_KEY",
        "XAI_REALTIME_MODEL",
        "OPENAI_REALTIME_MODEL",
        "XAI_VOICE",
        "OPENAI_VOICE",
        "RECORD_CALLS",
        "RECORDINGS_DIR",
        "WEBUI_ENABLED",
        "WEBUI_HOST",
        "WEBUI_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_xai_is_the_default_realtime_provider(monkeypatch) -> None:
    base_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "xai-key")

    settings = Settings.from_env(None)

    assert settings.realtime_provider == "xai"
    assert settings.realtime_model == "grok-voice-latest"
    assert settings.realtime_voice == "eve"
    assert settings.realtime_vad_threshold == 0.85
    assert settings.realtime_vad_prefix_padding_ms == 333


def test_openai_can_still_be_selected(monkeypatch) -> None:
    base_env(monkeypatch)
    monkeypatch.setenv("REALTIME_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    settings = Settings.from_env(None)

    assert settings.realtime_provider == "openai"
    assert settings.realtime_model == "gpt-realtime-2.1"
    assert settings.realtime_voice == "marin"


def test_selected_provider_requires_its_own_key(monkeypatch) -> None:
    base_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    with pytest.raises(ConfigurationError, match="XAI_API_KEY"):
        Settings.from_env(None)


def test_recording_and_dashboard_settings(monkeypatch) -> None:
    base_env(monkeypatch)
    monkeypatch.setenv("XAI_API_KEY", "xai-key")
    monkeypatch.setenv("RECORD_CALLS", "false")
    monkeypatch.setenv("RECORDINGS_DIR", "/tmp/booth-recordings")
    monkeypatch.setenv("WEBUI_HOST", "127.0.0.1")
    monkeypatch.setenv("WEBUI_PORT", "9090")

    settings = Settings.from_env(None)

    assert not settings.record_calls
    assert settings.recordings_dir == "/tmp/booth-recordings"
    assert settings.webui_host == "127.0.0.1"
    assert settings.webui_port == 9090
