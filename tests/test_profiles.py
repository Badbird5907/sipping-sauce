import random

from partyline_llm.profiles import (
    PARTYLINE_PROFILE,
    RandomProfileCycle,
    SPOOKY_PROFILE,
    SPOOKY_PROFILES,
)


def test_spooky_profile_is_fictional_and_has_a_greeting() -> None:
    assert "fictional" in SPOOKY_PROFILE.instructions.lower()
    assert "666" in SPOOKY_PROFILE.instructions
    assert "comedian" in SPOOKY_PROFILE.instructions.lower()
    assert SPOOKY_PROFILE.voice == "cedar"
    assert SPOOKY_PROFILE.greeting


def test_666_profiles_are_unique_and_have_short_greetings() -> None:
    assert SPOOKY_PROFILES
    assert len({profile.name for profile in SPOOKY_PROFILES}) == len(
        SPOOKY_PROFILES
    )
    assert all("666" in profile.instructions for profile in SPOOKY_PROFILES)
    assert all(0 < len(profile.greeting) <= 80 for profile in SPOOKY_PROFILES)


def test_every_personality_has_comedy_and_ai_accusation_rules() -> None:
    profiles = (PARTYLINE_PROFILE, *SPOOKY_PROFILES)

    assert all(
        "first job is to be funny" in profile.instructions for profile in profiles
    )
    assert all(
        "Never sound like customer support" in profile.instructions
        for profile in profiles
    )
    assert all("vehement" in profile.instructions for profile in profiles)
    assert all(
        "Accuse the caller of being the AI" in profile.instructions
        for profile in profiles
    )


def test_random_profile_cycle_uses_every_personality_before_repeating() -> None:
    cycle = RandomProfileCycle(SPOOKY_PROFILES, rng=random.Random(666))

    selected = [cycle() for _ in range(len(SPOOKY_PROFILES) * 3)]

    for start in range(0, len(selected), len(SPOOKY_PROFILES)):
        assert set(selected[start : start + len(SPOOKY_PROFILES)]) == set(
            SPOOKY_PROFILES
        )
    if len(SPOOKY_PROFILES) > 1:
        assert all(left is not right for left, right in zip(selected, selected[1:]))
    else:
        assert selected == [SPOOKY_PROFILE] * 3


def test_spooky_profile_ignores_generic_partyline_prompt(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_INSTRUCTIONS", "You are on a party line.")
    monkeypatch.setenv("OPENAI_GREETING", "Hello, the AI is on the party line.")

    profile = SPOOKY_PROFILE.configured(
        "SPOOKY", provider="openai", include_generic=False
    )

    assert "stand-up comedian" in profile.instructions
    assert "worst nightmare" in profile.greeting
    assert profile.voice == "cedar"


def test_spooky_specific_override_still_wins(monkeypatch) -> None:
    monkeypatch.setenv("SPOOKY_OPENAI_GREETING", "The custom darkness answers.")
    monkeypatch.setenv("SPOOKY_OPENAI_VOICE", "marin")

    profile = SPOOKY_PROFILE.configured(
        "SPOOKY", provider="openai", include_generic=False
    )

    assert profile.greeting == "The custom darkness answers."
    assert profile.voice == "marin"


def test_spooky_profile_ignores_generic_voice(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_VOICE", "marin")

    profile = SPOOKY_PROFILE.configured(
        "SPOOKY", provider="openai", include_generic=False
    )

    assert profile.voice == "cedar"


def test_xai_profile_uses_xai_voice_override(monkeypatch) -> None:
    monkeypatch.setenv("SPOOKY_XAI_VOICE", "eve")

    profile = SPOOKY_PROFILE.configured(
        "SPOOKY", provider="xai", include_generic=False
    )

    assert profile.voice_for("xai") == "eve"
    assert profile.voice_for("openai") == "cedar"
