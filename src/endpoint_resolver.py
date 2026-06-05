# src/endpoint_resolver.py
"""Unified endpoint resolution for all backend services.

Consolidates the 4+ copies of normalize_base / resolve_endpoint logic into one place.
"""

import json
import logging
import socket
import subprocess
from typing import Any, Optional, Tuple, Dict
from urllib.parse import urlparse, urlunparse

from core.database import SessionLocal, ModelEndpoint
from src.llm_core import _detect_provider, _host_match, _is_anthropic_compatible_url

logger = logging.getLogger(__name__)

# Model-name substrings that are NOT chat/generation models. When an endpoint
# has no explicit model configured we pick the first CHAT model from its list —
# never an embedding/tts/etc. (an OpenAI-style endpoint often lists
# `text-embedding-ada-002` first, which silently broke email-summarize and
# other resolve_endpoint callers with "Cannot reach model").
_NON_CHAT_MODEL = (
    "text-embedding", "embedding", "tts-", "whisper", "dall-e",
    "moderation", "rerank", "reranker", "clip", "stable-diffusion",
)


def _first_chat_model(models) -> Optional[str]:
    """First model that isn't an embedding/tts/etc.; falls back to models[0]."""
    for m in (models or []):
        if not any(p in str(m).lower() for p in _NON_CHAT_MODEL):
            return m
    return (models[0] if models else None)


def _endpoint_cached_models(ep) -> list:
    """Return cached model ids from the current or legacy endpoint field."""
    raw = getattr(ep, "cached_models", None) or getattr(ep, "models", None)
    if not raw:
        return []
    try:
        models = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return models if isinstance(models, list) else []


def _endpoint_hidden_models(ep) -> set:
    """Model ids the admin disabled on this endpoint (the UI's hidden list)."""
    raw = getattr(ep, "hidden_models", None)
    if not raw:
        return set()
    try:
        hidden = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return set()
    return set(hidden) if isinstance(hidden, list) else set()


def _endpoint_enabled_models(ep) -> list:
    """Cached models minus the ones disabled on the endpoint, order preserved.

    The auto-pick fallback must never select a model the user disabled — a
    Groq endpoint can list 16 models with only 1 enabled, and picking the
    raw first one resolves to a model that 400s ("requires terms acceptance").
    """
    hidden = _endpoint_hidden_models(ep)
    return [m for m in _endpoint_cached_models(ep) if m not in hidden]


# Cache for Tailscale hostname → IP resolution
_tailscale_cache: Dict[str, Optional[str]] = {}


def _resolve_tailscale_host(hostname: str) -> Optional[str]:
    """Try to resolve a hostname via 'tailscale status' if DNS fails."""
    if hostname in _tailscale_cache:
        return _tailscale_cache[hostname]

    # First check if normal DNS works
    try:
        socket.getaddrinfo(hostname, None, socket.AF_INET)
        _tailscale_cache[hostname] = None  # DNS works, no override needed
        return None
    except socket.gaierror:
        pass

    # DNS failed — try tailscale
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            import json as _json
            data = _json.loads(result.stdout)
            peers = data.get("Peer", {})
            for _id, peer in peers.items():
                peer_name = (peer.get("HostName") or "").lower()
                dns_name = (peer.get("DNSName") or "").split(".")[0].lower()
                if peer_name == hostname.lower() or dns_name == hostname.lower():
                    addrs = peer.get("TailscaleIPs", [])
                    if addrs:
                        ip = addrs[0]
                        logger.info(f"Resolved '{hostname}' via Tailscale → {ip}")
                        _tailscale_cache[hostname] = ip
                        return ip
    except Exception as e:
        logger.debug(f"Tailscale resolution failed for '{hostname}': {e}")

    _tailscale_cache[hostname] = None
    return None


def resolve_url(url: str) -> str:
    """If a URL's hostname can't be resolved via DNS, try Tailscale."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return url
    ip = _resolve_tailscale_host(hostname)
    if ip:
        # Replace hostname with IP in the URL
        netloc = ip
        if parsed.port:
            netloc = f"{ip}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))
    return url


def normalize_base(url: str) -> str:
    """Strip known API path suffixes from a base URL."""
    url = (url or "").strip().rstrip("/")
    for suffix in ["/models", "/chat/completions", "/completions", "/v1/messages"]:
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    for suffix in ["/chat", "/tags", "/generate"]:
        if url.endswith("/api" + suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


def _anthropic_api_root(base: str) -> str:
    """Return the API root for an Anthropic or Anthropic-compatible base URL.

    For ``https://api.anthropic.com/v1`` the trailing ``/v1`` is stripped so
    that ``build_chat_url`` can append ``/v1/messages``. For Anthropic-style
    proxies mounted on a non-``anthropic.com`` host (e.g. MiniMax Token Plan
    at ``https://api.minimax.io/anthropic``) the existing path prefix is
    preserved so the same builder produces
    ``https://api.minimax.io/anthropic/v1/messages``.

    Users who paste the full URL with the trailing ``/v1`` on either kind
    of host land at the same place: the suffix is stripped uniformly so
    we never end up with ``/v1/v1/messages``. A bare ``/v1`` on a host that
    isn't Anthropic-style is left untouched — the function is only called
    from Anthropic branches, but defending against misuse keeps the
    helper self-contained.
    """
    base = (base or "").strip().rstrip("/")
    if not _is_anthropic_compatible_url(base):
        return base
    if base.endswith("/v1"):
        return base[:-3].rstrip("/")
    return base


def _ollama_api_root(base: str) -> str:
    """Return the native Ollama API root, adding /api for ollama.com hosts."""
    base = (base or "").strip().rstrip("/")
    parsed = urlparse(base)
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/api"):
        return base
    if _host_match(base, "ollama.com"):
        root = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://ollama.com"
        return root.rstrip("/") + "/api"
    return base


def build_chat_url(base: str) -> str:
    """Return the correct chat endpoint URL for a given base."""
    base = resolve_url(base)
    provider = _detect_provider(base)
    if provider == "anthropic":
        return _anthropic_api_root(base) + "/v1/messages"
    if provider == "ollama":
        return _ollama_api_root(base) + "/chat"
    return base + "/chat/completions"


def build_models_url(base: str) -> str:
    """Return the provider-specific model-list endpoint URL for a base."""
    base = resolve_url(base)
    provider = _detect_provider(base)
    if provider == "anthropic":
        return _anthropic_api_root(base) + "/v1/models"
    if provider == "ollama":
        return _ollama_api_root(base) + "/tags"
    return base + "/models"


def build_headers(api_key: Optional[str], base: str) -> Dict[str, str]:
    """Build auth headers for an endpoint."""
    provider = _detect_provider(base)
    headers: Dict[str, str] = {}
    if provider == "anthropic":
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        return headers
    if provider == "copilot":
        from src.copilot import copilot_headers
        return copilot_headers(api_key)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if provider == "openrouter":
        headers.setdefault("HTTP-Referer", "https://github.com/pewdiepie-archdaemon/odysseus")
        headers.setdefault("X-OpenRouter-Title", "Odysseus")
    return headers


def resolve_endpoint(
    setting_prefix: str,
    fallback_url: Optional[str] = None,
    fallback_model: Optional[str] = None,
    fallback_headers: Optional[Dict] = None,
    owner: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[Dict]]:
    """Resolve an endpoint/model from settings, with fallback.

    Args:
        setting_prefix: Settings key prefix, e.g. "research", "task", "utility", "default".
                       Reads ``{prefix}_endpoint_id`` and ``{prefix}_model`` from settings.
        fallback_url:    URL to use if settings are empty or endpoint missing.
        fallback_model:  Model to use if settings are empty.
        fallback_headers: Headers to use if using fallback.

    Returns:
        (endpoint_url, model, headers) — resolved or fallback values.
    """
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
    except Exception:
        return fallback_url, fallback_model, fallback_headers

    owner_str = owner or ""
    def _stg(key: str) -> str:
        return (get_user_setting(key, owner_str, settings.get(key, "")) or "").strip()

    ep_id = _stg(f"{setting_prefix}_endpoint_id")
    model = _stg(f"{setting_prefix}_model")

    # If the specific endpoint is not configured, but the caller provided a
    # valid fallback (e.g. the active session model), use that immediately.
    # This prevents background tasks from jumping to the global default_model
    # when the user is mid-conversation with a different model.
    if not ep_id and fallback_url and fallback_model:
        return fallback_url, fallback_model, fallback_headers

    # Unset Utility means "same as Default Chat Model".
    if setting_prefix == "utility" and not ep_id:
        ep_id = _stg("default_endpoint_id")
        model = _stg("default_model")

    # Fall back to utility model for task/research/auto-naming if not specifically configured.
    # If Utility itself is unset, the block above makes that resolve to Default Chat.
    if not ep_id and setting_prefix != "utility":
        ep_id = _stg("utility_endpoint_id")
        model = _stg("utility_model")
        if not ep_id:
            ep_id = _stg("default_endpoint_id")
            model = _stg("default_model")

    if not ep_id:
        return fallback_url, fallback_model, fallback_headers

    db = SessionLocal()
    try:
        ep = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == ep_id,
            ModelEndpoint.is_enabled == True,
        )
        if owner:
            from src.auth_helpers import owner_filter
            ep = owner_filter(ep, ModelEndpoint, owner).first()
        else:
            ep = ep.first()
        if not ep:
            return fallback_url, fallback_model, fallback_headers

        base = normalize_base(ep.base_url)
        chat_url = build_chat_url(base)
        headers = build_headers(ep.api_key, base)

        # Discard a configured model the user has since disabled on the
        # endpoint (e.g. a stale `default_model` left pointing at a now-hidden
        # model). Treat it as unset so the picker below selects a live one
        # instead of dispatching to a disabled model that 400s.
        if model and model in _endpoint_hidden_models(ep):
            model = ""
        # If no (usable) model specified, pick the first enabled chat model.
        if not model:
            model = _first_chat_model(_endpoint_enabled_models(ep)) or ""
        if not model and not fallback_model:
            logger.warning('[resolve_endpoint] no usable model (all models hidden or list empty)')

        return chat_url, model or fallback_model, headers
    except Exception as e:
        logger.debug(f"Could not resolve {setting_prefix} endpoint: {e}")
        return fallback_url, fallback_model, fallback_headers
    finally:
        db.close()


def resolve_endpoint_by_id(
    ep_id: str, model: Optional[str] = None, owner: Optional[str] = None
) -> Optional[Tuple[str, str, Dict]]:
    """Resolve a specific endpoint id (+ optional model) to (chat_url, model, headers).

    Returns None if the endpoint doesn't exist or is disabled. Used to turn
    a configured fallback entry ({endpoint_id, model}) into a dispatch target.
    """
    if not ep_id:
        return None
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == ep_id,
            ModelEndpoint.is_enabled == True,
        )
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        ep = q.first()
        if not ep:
            return None
        base = normalize_base(ep.base_url)
        chat_url = build_chat_url(base)
        headers = build_headers(ep.api_key, base)
        m = (model or "").strip()
        # Drop a model the user disabled on the endpoint, then pick the first
        # enabled chat model rather than a hidden one.
        if m and m in _endpoint_hidden_models(ep):
            m = ""
        if not m:
            m = _first_chat_model(_endpoint_enabled_models(ep)) or ""
        if not m:
            return None
        return chat_url, m, headers
    except Exception as e:
        logger.debug(f"Could not resolve endpoint {ep_id}: {e}")
        return None
    finally:
        db.close()


def resolve_chat_fallback_candidates(owner: Optional[str] = None) -> list:
    """Build the configured default-chat fallback chain as a list of
    (chat_url, model, headers) tuples, skipping any that can't resolve.

    The primary model is NOT included — callers prepend their session's
    current (url, model, headers) so per-session model overrides are honored.
    """
    return _resolve_fallback_candidates("default_model_fallbacks", owner=owner)


def resolve_utility_fallback_candidates(owner: Optional[str] = None) -> list:
    """Configured fallback chain for the Utility model (`utility_model_fallbacks`)."""
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        utility_ep = (get_user_setting("utility_endpoint_id", owner or "", settings.get("utility_endpoint_id", "")) or "").strip()
        if not utility_ep:
            return _resolve_fallback_candidates("default_model_fallbacks", owner=owner)
    except Exception:
        pass
    return _resolve_fallback_candidates("utility_model_fallbacks", owner=owner)


def resolve_vision_fallback_candidates(owner: Optional[str] = None) -> list:
    """Configured fallback chain for the Vision model (`vision_model_fallbacks`)."""
    return _resolve_fallback_candidates("vision_model_fallbacks", owner=owner)


def _resolve_fallback_candidates(setting_key: str, owner: Optional[str] = None) -> list:
    out = []
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        chain = get_user_setting(setting_key, owner or "", settings.get(setting_key) or []) or []
    except Exception:
        return out
    for entry in chain:
        if not isinstance(entry, dict):
            continue
        resolved = resolve_endpoint_by_id(entry.get("endpoint_id", ""), entry.get("model", ""), owner=owner)
        if resolved:
            out.append(resolved)
    return out


# ---------------------------------------------------------------------------
# Max-tokens resolution chain
# ---------------------------------------------------------------------------
#
# 4-tier precedence, highest to lowest. A value of ``None`` at any tier means
# "not configured for this tier, fall through". A value of ``0`` is an
# EXPLICIT, valid value meaning "no limit" (sent to the provider as 0). The
# provider decides what 0 means — Anthropic rejects it, OpenAI interprets it
# as the model default, MiniMax mirrors OpenAI. This is intentional: the
# user opted into "no limit" by setting 0.
#
#   1. session.max_tokens        — per-chat override (DB column)
#   2. endpoint.model_max_tokens[model]  — per-model override (JSON map)
#   3. endpoint.max_tokens       — per-endpoint default (DB column)
#   4. global user setting        — Settings → AI → Default max output tokens
#   5. provider default           — 4096 for Anthropic / Anthropic-compatible;
#                                   0 for everything else (let the model
#                                   decide its own cap)
#
# ``preset_max_tokens`` (from the active persona) is a tier-0 override that
# is applied by the caller BEFORE invoking this function — the preset is a
# chat-level setting just like the session, and conflating them keeps the
# precedence simple.

# Provider default for Anthropic-style APIs. Matches the Anthropic API's
# own default for the Messages endpoint, so even an unconfigured user
# gets a sane number of tokens on a fresh install.
_ANTHROPIC_PROVIDER_DEFAULT = 4096


def _parse_model_max_tokens(raw) -> Dict[str, int]:
    """Parse the ``model_max_tokens`` JSON column on ``ModelEndpoint``.

    Returns an empty dict on any failure (malformed JSON, non-dict, None).
    Always returns a fresh dict so callers can mutate freely.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return {}


def resolve_max_tokens(
    *,
    session: Optional[Any] = None,
    model: Optional[str] = None,
    endpoint: Optional[Any] = None,
    endpoint_base_url: Optional[str] = None,
    owner: Optional[str] = None,
) -> int:
    """Resolve the output cap for an LLM call.

    Walks the 4-tier chain (session → per-model → per-endpoint → global),
    returning the first explicitly-configured value (including ``0``). If
    nothing is configured anywhere, returns the provider default
    (``_ANTHROPIC_PROVIDER_DEFAULT`` for Anthropic-compatible URLs,
    ``0`` for everything else).

    Args:
        session: An object with a ``max_tokens`` attribute (the ``Session``
            DB row, the in-memory session, or anything that quacks like
            one). ``None`` or missing ``max_tokens`` is treated as "not
            configured at this tier".
        model: Model id used for the per-model override lookup. ``None``
            skips the per-model tier.
        endpoint: A ``ModelEndpoint`` row (or anything with ``max_tokens`` /
            ``model_max_tokens`` attributes). ``None`` skips the per-endpoint
            tiers.
        endpoint_base_url: Used to detect the Anthropic provider for the
            fallback default. If both ``endpoint`` and ``endpoint_base_url``
            are provided, ``endpoint.base_url`` wins; this argument is the
            convenience path for callers that already have the URL but not
            the row.
        owner: Username, used to read the global user setting.

    Returns:
        An ``int`` token cap. ``0`` means "no limit" (explicit user opt-in).
    """
    # Tier 1: per-chat (session.max_tokens)
    if session is not None:
        v = getattr(session, "max_tokens", None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass

    # Tier 2 + 3: per-model (endpoint.model_max_tokens[model]) then
    # per-endpoint default (endpoint.max_tokens)
    if endpoint is not None:
        model_overrides = _parse_model_max_tokens(getattr(endpoint, "model_max_tokens", None))
        if model and model in model_overrides:
            return int(model_overrides[model])
        v = getattr(endpoint, "max_tokens", None)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass

    # Tier 4: global user setting (Settings → AI → default_max_tokens)
    try:
        from src.settings import get_user_setting
        v = get_user_setting("default_max_tokens", owner or "")
        if v is not None and v != "":
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    except Exception:
        # Settings module may not be importable in every code path (e.g. some
        # background tasks); never let a config read fail the resolution.
        pass

    # Tier 5: provider default
    base = (endpoint_base_url
            or (getattr(endpoint, "base_url", None) if endpoint is not None else None)
            or "")
    if base and _is_anthropic_compatible_url(base):
        return _ANTHROPIC_PROVIDER_DEFAULT
    return 0


def resolve_max_tokens_with_preset(
    *,
    preset_max_tokens: Optional[int] = None,
    **kwargs,
) -> int:
    """Resolve the effective max_tokens for a chat call.

    Convenience wrapper around :func:`resolve_max_tokens` that adds a tier-0
    override: if the active persona / preset has ``max_tokens`` explicitly
    set (including 0 for "no limit"), that value wins. Otherwise the
    chain falls through to the 4-tier resolution.

    Args:
        preset_max_tokens: The persona's ``max_tokens`` field. ``None``
            means "no preset, use the chain". ``0`` is an EXPLICIT
            opt-in for "no limit". Positive values are used as-is.
        **kwargs: Forwarded to :func:`resolve_max_tokens`.
    """
    # Tier 0: the active persona/preset. The user's intentional chat-level
    # setting beats everything below it.
    if preset_max_tokens is not None:
        try:
            v = int(preset_max_tokens)
            if v >= 0:
                return v
        except (TypeError, ValueError):
            pass
    return resolve_max_tokens(**kwargs)
