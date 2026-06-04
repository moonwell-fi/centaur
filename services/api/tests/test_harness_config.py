from __future__ import annotations


def test_default_harness_defaults_to_codex(monkeypatch):
    from api.harness_config import default_harness

    monkeypatch.delenv("CENTAUR_DEFAULT_HARNESS", raising=False)

    assert default_harness() == "codex"


def test_default_harness_supports_aliases(monkeypatch):
    from api.harness_config import default_harness

    monkeypatch.setenv("CENTAUR_DEFAULT_HARNESS", "claude")
    assert default_harness() == "claude-code"

    monkeypatch.setenv("CENTAUR_DEFAULT_HARNESS", "pi")
    assert default_harness() == "pi-mono"


def test_default_harness_ignores_unknown_values(monkeypatch):
    from api.harness_config import default_harness

    monkeypatch.setenv("CENTAUR_DEFAULT_HARNESS", "unknown")

    assert default_harness() == "codex"


def test_enabled_harnesses_defaults_to_default_harness(monkeypatch):
    from api.harness_config import enabled_harnesses

    monkeypatch.delenv("CENTAUR_DEFAULT_HARNESS", raising=False)
    monkeypatch.delenv("CENTAUR_ENABLED_HARNESSES", raising=False)
    assert enabled_harnesses() == frozenset({"codex"})

    monkeypatch.setenv("CENTAUR_DEFAULT_HARNESS", "claude")
    assert enabled_harnesses() == frozenset({"claude-code"})


def test_enabled_harnesses_adds_explicit_list(monkeypatch):
    from api.harness_config import enabled_harnesses

    monkeypatch.setenv("CENTAUR_DEFAULT_HARNESS", "claude")
    monkeypatch.setenv("CENTAUR_ENABLED_HARNESSES", "codex")
    assert enabled_harnesses() == frozenset({"claude-code", "codex"})


def test_enabled_harnesses_normalizes_aliases_and_separators(monkeypatch):
    from api.harness_config import enabled_harnesses

    monkeypatch.setenv("CENTAUR_DEFAULT_HARNESS", "claude")
    # Mixed comma/space separators; aliases normalize (claude->claude-code,
    # pi->pi-mono) so the set matches the _HARNESS_SECRETS engine keys.
    monkeypatch.setenv("CENTAUR_ENABLED_HARNESSES", "codex,  pi   claude")
    assert enabled_harnesses() == frozenset({"claude-code", "codex", "pi-mono"})


def test_enabled_harnesses_passes_through_unknown_tokens(monkeypatch):
    from api.harness_config import enabled_harnesses

    monkeypatch.delenv("CENTAUR_DEFAULT_HARNESS", raising=False)
    monkeypatch.setenv("CENTAUR_ENABLED_HARNESSES", "mystery")
    # Unknown engines stay verbatim (they match no _HARNESS_SECRETS entry, so
    # they're inert) rather than being coerced to the default the way
    # CENTAUR_DEFAULT_HARNESS is.
    assert enabled_harnesses() == frozenset({"codex", "mystery"})


def test_enabled_harnesses_ignores_blank_entries(monkeypatch):
    from api.harness_config import enabled_harnesses

    monkeypatch.setenv("CENTAUR_DEFAULT_HARNESS", "claude")
    monkeypatch.setenv("CENTAUR_ENABLED_HARNESSES", "  , ,  ")
    assert enabled_harnesses() == frozenset({"claude-code"})
