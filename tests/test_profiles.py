from partyline_llm.profiles import SPOOKY_PROFILE


def test_spooky_profile_is_fictional_and_has_a_greeting() -> None:
    assert "fictional" in SPOOKY_PROFILE.instructions.lower()
    assert "666" in SPOOKY_PROFILE.instructions
    assert "BOMBACLATT" in SPOOKY_PROFILE.instructions
    assert "BOMBACLATT" in SPOOKY_PROFILE.greeting
    assert SPOOKY_PROFILE.voice == "cedar"
    assert SPOOKY_PROFILE.greeting


def test_spooky_profile_ignores_generic_partyline_prompt(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_INSTRUCTIONS", "You are on a party line.")
    monkeypatch.setenv("OPENAI_GREETING", "Hello, the AI is on the party line.")

    profile = SPOOKY_PROFILE.configured("SPOOKY", include_generic=False)

    assert "Duppy Devil" in profile.instructions
    assert "BOMBACLATT" in profile.greeting
    assert profile.voice == "cedar"


def test_spooky_specific_override_still_wins(monkeypatch) -> None:
    monkeypatch.setenv("SPOOKY_OPENAI_GREETING", "The custom darkness answers.")
    monkeypatch.setenv("SPOOKY_OPENAI_VOICE", "marin")

    profile = SPOOKY_PROFILE.configured("SPOOKY", include_generic=False)

    assert profile.greeting == "The custom darkness answers."
    assert profile.voice == "marin"


def test_spooky_profile_ignores_generic_voice(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_VOICE", "marin")

    profile = SPOOKY_PROFILE.configured("SPOOKY", include_generic=False)

    assert profile.voice == "cedar"
