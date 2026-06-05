"""Tests for the 4-tier max_tokens resolution chain.

The chain in :func:`src.endpoint_resolver.resolve_max_tokens` is:

  1. session.max_tokens               — per-chat override
  2. endpoint.model_max_tokens[model] — per-model override (JSON map)
  3. endpoint.max_tokens              — per-endpoint default
  4. global user setting              — ``Settings → AI → default_max_tokens``
  5. provider default                 — 4096 for Anthropic / Anthropic-compatible;
                                       0 for everything else

``0`` is a valid explicit value at every tier meaning "no limit"
(provider decides). ``None`` / missing means "not configured at this
tier, fall through". The function never raises — a malformed config
(e.g. JSON that's not a dict) is treated as "not configured" so a
bug in the admin UI can't 500 the whole chat.
"""
import json
from types import SimpleNamespace

import pytest

from src.endpoint_resolver import (
    resolve_max_tokens,
    resolve_max_tokens_with_preset,
)


def _ep(base_url: str, max_tokens=None, model_max_tokens=None) -> SimpleNamespace:
    """Tiny ModelEndpoint stand-in with just the fields the resolver reads."""
    return SimpleNamespace(
        base_url=base_url,
        max_tokens=max_tokens,
        model_max_tokens=model_max_tokens,
    )


def _sess(max_tokens) -> SimpleNamespace:
    return SimpleNamespace(max_tokens=max_tokens, model="x", endpoint_url="https://x")


# ---------------------------------------------------------------------------
# Tier 1: session override
# ---------------------------------------------------------------------------

class TestSessionOverride:
    def test_session_set_wins_over_everything(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 99999)
        ep = _ep("https://api.minimax.io/anthropic",
                 max_tokens=4096,
                 model_max_tokens=json.dumps({"x": 1234}))
        assert resolve_max_tokens(session=_sess(8192), model="x", endpoint=ep) == 8192

    def test_session_zero_is_explicit_no_limit(self, monkeypatch):
        # User explicitly opted out — must be sent as 0, not replaced by
        # any lower tier.
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 99999)
        ep = _ep("https://api.minimax.io/anthropic", max_tokens=4096)
        assert resolve_max_tokens(session=_sess(0), endpoint=ep) == 0

    def test_session_none_falls_through(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 0)
        ep = _ep("https://api.minimax.io/anthropic", max_tokens=4096)
        assert resolve_max_tokens(session=_sess(None), endpoint=ep) == 4096


# ---------------------------------------------------------------------------
# Tier 2: per-model override (JSON map on the endpoint)
# ---------------------------------------------------------------------------

class TestPerModelOverride:
    def test_per_model_map_wins_over_endpoint_default(self):
        ep = _ep(
            "https://api.minimax.io/anthropic",
            max_tokens=4096,
            model_max_tokens=json.dumps({"MiniMax-M3": 32000, "MiniMax-M2.7": 64000}),
        )
        assert resolve_max_tokens(model="MiniMax-M3", endpoint=ep) == 32000
        assert resolve_max_tokens(model="MiniMax-M2.7", endpoint=ep) == 64000

    def test_per_model_map_miss_falls_through_to_endpoint(self):
        ep = _ep(
            "https://api.minimax.io/anthropic",
            max_tokens=4096,
            model_max_tokens=json.dumps({"MiniMax-M3": 32000}),
        )
        assert resolve_max_tokens(model="MiniMax-M2.7", endpoint=ep) == 4096

    def test_per_model_map_parses_already_dict(self):
        # The route serialises to JSON, but the column can also hold a
        # raw dict if set in another code path.
        ep = _ep(
            "https://api.minimax.io/anthropic",
            model_max_tokens={"MiniMax-M3": 32000},
        )
        assert resolve_max_tokens(model="MiniMax-M3", endpoint=ep) == 32000

    def test_per_model_map_garbage_does_not_raise(self):
        ep = _ep(
            "https://api.minimax.io/anthropic",
            max_tokens=4096,
            model_max_tokens="not json",
        )
        # Falls through to endpoint default rather than 500ing the chat.
        assert resolve_max_tokens(model="MiniMax-M3", endpoint=ep) == 4096

    def test_per_model_map_filters_non_numeric_values(self, monkeypatch):
        # The global setting is also unset so a non-numeric per-model
        # value falls all the way through to the provider default (4096
        # for Anthropic-compatible URLs).
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: None)
        ep = _ep(
            "https://api.minimax.io/anthropic",
            model_max_tokens=json.dumps({"a": 1, "b": "no", "c": True, "d": 2}),
        )
        # ``True`` is technically an int subclass but is filtered to avoid
        # silent type coercion surprises. ``"no"`` is dropped.
        assert resolve_max_tokens(model="a", endpoint=ep) == 1
        assert resolve_max_tokens(model="b", endpoint=ep) == 4096  # provider default
        assert resolve_max_tokens(model="c", endpoint=ep) == 4096  # filtered
        assert resolve_max_tokens(model="d", endpoint=ep) == 2


# ---------------------------------------------------------------------------
# Tier 3: per-endpoint default
# ---------------------------------------------------------------------------

class TestEndpointDefault:
    def test_endpoint_default_wins_over_global(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 99999)
        ep = _ep("https://api.minimax.io/anthropic", max_tokens=4096)
        assert resolve_max_tokens(endpoint=ep) == 4096

    def test_endpoint_zero_is_explicit_no_limit(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 99999)
        ep = _ep("https://api.minimax.io/anthropic", max_tokens=0)
        assert resolve_max_tokens(endpoint=ep) == 0


# ---------------------------------------------------------------------------
# Tier 4: global user setting
# ---------------------------------------------------------------------------

class TestGlobalSetting:
    def test_global_used_when_no_endpoint_or_session(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 16384)
        assert resolve_max_tokens() == 16384

    def test_global_zero_is_explicit_no_limit(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 0)
        # 0 at the global tier is honored — but for Anthropic, the
        # provider default of 4096 still wins because we fall through to
        # the provider-default tier. To actually send 0 globally, the
        # user has to set it on the session or endpoint tier.
        assert resolve_max_tokens(endpoint_base_url="https://api.minimax.io/anthropic") == 0

    def test_global_with_anthropic_provider_default(self, monkeypatch):
        # Nothing set anywhere → Anthropic provider default of 4096 wins.
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: None)
        assert resolve_max_tokens(endpoint_base_url="https://api.anthropic.com") == 4096
        assert resolve_max_tokens(endpoint_base_url="https://api.minimax.io/anthropic") == 4096

    def test_global_with_openai_provider_default(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: None)
        # OpenAI / others have no implicit default — 0 is returned so the
        # provider's own behavior takes over.
        assert resolve_max_tokens(endpoint_base_url="https://api.openai.com/v1") == 0
        assert resolve_max_tokens(endpoint_base_url="https://openrouter.ai/api/v1") == 0

    def test_global_settings_import_failure_falls_through(self, monkeypatch):
        # A failing settings module must never break the chat.
        def _raise(*a, **k):
            raise RuntimeError("settings broken")
        monkeypatch.setattr("src.settings.get_user_setting", _raise)
        assert resolve_max_tokens(endpoint_base_url="https://api.minimax.io/anthropic") == 4096


# ---------------------------------------------------------------------------
# Tier 5: provider default
# ---------------------------------------------------------------------------

class TestProviderDefault:
    def test_anthropic_compatible_path_uses_4096(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: None)
        # MiniMax Anthropic-style URL — provider default of 4096 wins.
        assert resolve_max_tokens(endpoint_base_url="https://api.minimax.io/anthropic") == 4096
        # Native Anthropic too.
        assert resolve_max_tokens(endpoint_base_url="https://api.anthropic.com") == 4096

    def test_openai_uses_zero(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: None)
        assert resolve_max_tokens(endpoint_base_url="https://api.openai.com/v1") == 0

    def test_local_uses_zero(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: None)
        assert resolve_max_tokens(endpoint_base_url="http://localhost:11434/v1") == 0


# ---------------------------------------------------------------------------
# Tier 0: persona / preset override (the active persona is the highest tier)
# ---------------------------------------------------------------------------

class TestPresetOverride:
    def test_preset_wins_over_everything(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 99999)
        ep = _ep("https://api.minimax.io/anthropic",
                 max_tokens=4096,
                 model_max_tokens=json.dumps({"x": 1234}))
        assert resolve_max_tokens_with_preset(
            preset_max_tokens=512,
            session=_sess(8192),
            model="x",
            endpoint=ep,
        ) == 512

    def test_preset_zero_is_explicit_no_limit(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 99999)
        ep = _ep("https://api.minimax.io/anthropic", max_tokens=4096)
        assert resolve_max_tokens_with_preset(
            preset_max_tokens=0,
            session=_sess(8192),
            endpoint=ep,
        ) == 0

    def test_preset_none_falls_through(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 16384)
        assert resolve_max_tokens_with_preset(preset_max_tokens=None) == 16384

    def test_preset_negative_ignored_falls_through(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 16384)
        assert resolve_max_tokens_with_preset(preset_max_tokens=-5) == 16384


# ---------------------------------------------------------------------------
# Realistic MiniMax M3 scenario
# ---------------------------------------------------------------------------

class TestMiniMaxScenario:
    """End-to-end: MiniMax-M3 on api.minimax.io/anthropic with a per-model
    override of 32K. Verifies the 4-tier chain produces the expected
    value at every level."""

    BASE_URL = "https://api.minimax.io/anthropic"
    MODEL = "MiniMax-M3"
    EP_DEFAULT = 16384
    MODEL_OVERRIDE = 32000

    def _ep(self, **kw):
        return _ep(
            self.BASE_URL,
            max_tokens=kw.get("ep_default", self.EP_DEFAULT),
            model_max_tokens=json.dumps({self.MODEL: self.MODEL_OVERRIDE}) if kw.get("per_model", True) else None,
        )

    def test_per_model_wins_when_only_model_override_set(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: None)
        assert resolve_max_tokens(model=self.MODEL, endpoint=self._ep()) == self.MODEL_OVERRIDE

    def test_endpoint_default_used_when_model_not_in_map(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: None)
        ep = self._ep(per_model=False)
        assert resolve_max_tokens(model="SomeOtherModel", endpoint=ep) == self.EP_DEFAULT

    def test_global_used_when_endpoint_unconfigured(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: 65536)
        # No session, no endpoint, no per-model → global.
        assert resolve_max_tokens(endpoint_base_url=self.BASE_URL) == 65536

    def test_provider_default_only_when_nothing_set(self, monkeypatch):
        monkeypatch.setattr("src.settings.get_user_setting", lambda *a, **k: None)
        assert resolve_max_tokens(endpoint_base_url=self.BASE_URL) == 4096
