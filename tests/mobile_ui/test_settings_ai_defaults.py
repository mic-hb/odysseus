"""Regression tests for two settings-panel bugs reported by a user
after adding a MiniMax Token Plan endpoint.

1. **Vision model list is empty.** The primary vision <select> in
   Settings → AI Defaults → Vision was populated exactly once
   (in ``initVisionSettings``) and never re-fetched, so adding a
   new endpoint AFTER the settings panel was first opened left the
   vision list showing only the "Auto-detect" placeholder. The
   chat list and the vision fallback widget both refresh on
   ``_registerAiEndpointRefresh``; the main vision select did
   not. Fix: extract the list population into ``_refreshVisionList``
   and hook it into the same refresh callback.

2. **Token Cap card "Could not load settings: API_BASE is not
   defined".** The new ``initOutputCapSettings`` function in
   static/js/settings.js used ``API_BASE`` in a fetch URL template
   literal but never declared it. The settings module is loaded
   directly by index.html (no ``init(apiBase)`` indirection like
   chat.js / document.js), so it can't read a runtime-injected
   value. Fix: declare ``const API_BASE = window.location.origin;``
   at module top — the same convention used by calendar.js,
   emailInbox.js, chatMaxTokens.js.
"""
import pytest


def _open_settings_ai_tab(mobile_page, base_url):
    """Open Settings and switch to the AI tab; wait for both the tab
    content and the chat input bar to be ready. Idempotent."""
    mobile_page.goto(base_url + "/", wait_until="domcontentloaded")
    try:
        mobile_page.wait_for_selector("#app-loader", state="detached", timeout=10_000)
    except Exception:
        pass
    mobile_page.wait_for_timeout(1500)
    mobile_page.evaluate("""() => {
        document.body.classList.remove('doc-view', 'notes-view', 'gallery-view');
    }""")
    # Open settings (the cog in the user bar).
    mobile_page.evaluate("""() => {
        const btn = document.getElementById('user-bar-settings')
                  || document.querySelector('.user-bar-btn');
        if (btn) btn.click();
    }""")
    mobile_page.wait_for_selector("#settings-modal", timeout=5_000)
    # Switch to the AI tab if it isn't already active.
    mobile_page.evaluate("""() => {
        const tab = document.querySelector('[data-settings-tab=\"ai\"]');
        if (tab && !tab.classList.contains('active')) tab.click();
    }""")
    mobile_page.wait_for_timeout(500)


def test_token_cap_card_loads_settings(mobile_page, base_url):
    """The Token Cap card must load settings without an "API_BASE is
    not defined" error.

    Before the fix, the new ``initOutputCapSettings`` function used
    ``API_BASE`` in a fetch URL template literal but never declared
    the constant (the settings module is loaded directly by
    index.html — no ``init(apiBase)`` indirection). The page
    rendered the input but the very first GET against
    ``/api/settings`` threw ``ReferenceError: API_BASE is not
    defined``, surfaced as the literal string "API_BASE is not
    defined" in the card's error slot.
    """
    _open_settings_ai_tab(mobile_page, base_url)
    mobile_page.wait_for_selector("#set-defaultMaxTokens", timeout=5_000)
    # Wait long enough for the fetch to have either succeeded or
    # surfaced its error. The bug was a synchronous ReferenceError on
    # the fetch call — it would surface within the next tick.
    mobile_page.wait_for_timeout(1500)
    msg = mobile_page.locator("#set-defaultMaxTokensMsg")
    text = (msg.text_content() or "").strip()
    assert "API_BASE" not in text, (
        f"Token Cap card surfaced a JS ReferenceError: {text!r}"
    )
    assert "Could not load settings" not in text, (
        f"Token Cap card failed to load settings: {text!r}"
    )


def test_vision_list_includes_models_from_new_endpoint(mobile_page, base_url):
    """The vision <select> must show MiniMax-M3 after the endpoint
    is added — even if the settings panel was already open and
    the endpoint was added later, WITHOUT reloading the page.

    This reproduces the user's exact flow: open settings first
    (with NO endpoints, so the vision list is empty), then add
    the endpoint via the admin panel, then ask the settings module
    to refresh (the admin panel emits a "settings changed" event
    that calls settingsModule.refreshAiModelEndpoints). The
    before-the-fix behavior is that the vision <select> shows
    only the "Auto-detect" placeholder because it was populated
    exactly once in initVisionSettings() and never re-fetched.
    """
    # Start from a clean slate so the test is reproducible.
    mobile_page.goto(base_url + "/", wait_until="domcontentloaded")
    mobile_page.wait_for_timeout(1000)
    _delete_minimax_if_exists(mobile_page, base_url)
    # 1) Open settings first — with no endpoints, the vision list
    #    has only the "Auto-detect" placeholder.
    _open_settings_ai_tab(mobile_page, base_url)
    mobile_page.wait_for_selector("#set-vlModelSelect", timeout=5_000)
    initial = mobile_page.evaluate("""() => {
        const sel = document.getElementById('set-vlModelSelect');
        return Array.from(sel.options).map(o => o.value);
    }""")
    assert "MiniMax-M3" not in initial, (
        f"Pre-condition failed: M3 should NOT be in the list yet. "
        f"Got: {initial}"
    )
    # 2) Add the endpoint — but stay on the settings page (no
    #    page reload). In a real session, the admin panel lives in
    #    a separate page or modal; the settings panel here stays
    #    open with its stale vision list.
    _ensure_minimax_endpoint(mobile_page, base_url)
    # 3) Trigger the refresh hook. In production this is fired by
    #    the admin "endpoint added" success path via the
    #    ``odysseus-integrations-changed`` window event; we call the
    #    exported helper directly to simulate it.
    mobile_page.evaluate("""() => {
        if (window.settingsModule && settingsModule.refreshAiModelEndpoints) {
            settingsModule.refreshAiModelEndpoints();
        }
    }""")
    mobile_page.wait_for_timeout(1000)
    # 4) M3 must now be in the vision list — WITHOUT a page reload.
    options = mobile_page.evaluate("""() => {
        const sel = document.getElementById('set-vlModelSelect');
        if (!sel) return null;
        return Array.from(sel.options).map(o => o.value);
    }""")
    assert options is not None, "set-vlModelSelect missing"
    assert "MiniMax-M3" in options, (
        f"MiniMax-M3 missing from vision list after endpoint add + "
        f"refresh. Options: {options}"
    )


def test_vision_list_picks_up_endpoint_on_settings_reopen(desktop_page, base_url):
    """A more realistic flow: add the endpoint, then open settings.
    M3 should appear in the Vision list immediately (no page reload
    required)."""
    # Navigate first so the in-page fetch can reach the same origin
    # (window.location.origin is null on about:blank).
    desktop_page.goto(base_url + "/", wait_until="domcontentloaded")
    desktop_page.wait_for_timeout(1000)
    # Start clean so the test is order-independent.
    _delete_minimax_if_exists(desktop_page, base_url)
    # Add the endpoint via the admin API (in-page fetch).
    _ensure_minimax_endpoint(desktop_page, base_url)
    # Now open settings and confirm M3 is in the vision list.
    _open_settings_ai_tab(desktop_page, base_url)
    desktop_page.wait_for_selector("#set-vlModelSelect", timeout=5_000)
    desktop_page.wait_for_timeout(1500)
    options = desktop_page.evaluate("""() => {
        const sel = document.getElementById('set-vlModelSelect');
        if (!sel) return null;
        return Array.from(sel.options).map(o => o.value);
    }""")
    assert options is not None
    assert "MiniMax-M3" in options, (
        f"MiniMax-M3 missing from vision list. Options: {options}"
    )


# --- helpers ---

def _delete_minimax_if_exists(mobile_page, base_url):
    """Delete the MiniMax endpoint if it exists, so the test starts
    from a clean slate. Idempotent — no-op if it doesn't exist."""
    base = base_url.rstrip("/")
    mobile_page.evaluate("""async ({base}) => {
        const list = await fetch(base + '/api/model-endpoints', { credentials: 'same-origin' });
        const data = await list.json();
        const existing = (data || []).find(ep => ep.base_url && ep.base_url.startsWith('https://api.minimax.io/'));
        if (existing) {
            await fetch(base + '/api/model-endpoints/' + existing.id, { method: 'DELETE', credentials: 'same-origin' });
        }
    }""", {"base": base})
    mobile_page.wait_for_timeout(300)


def _ensure_minimax_endpoint(mobile_page, base_url):
    """Add the MiniMax Token Plan endpoint with M3 pinned, via the
    admin API directly. Idempotent — if it already exists, the
    PATCH below is a no-op for pinned_models.

    The local test instance has admin/testpass credentials.
    """
    # The ``window.location.origin`` is ``null`` in some Playwright
    # contexts (notably the about:blank page used during auth), so
    # pass the base URL explicitly to the in-page script.
    base = base_url.rstrip("/")
    mobile_page.evaluate("""async ({base}) => {
        // 1) Check if the endpoint already exists.
        const list = await fetch(base + '/api/model-endpoints', { credentials: 'same-origin' });
        const data = await list.json();
        const existing = (data || []).find(ep => ep.base_url && ep.base_url.startsWith('https://api.minimax.io/'));
        if (existing) {
            await fetch(base + '/api/model-endpoints/' + existing.id, {
                method: 'PATCH',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pinned_models: ['MiniMax-M3'] }),
            });
            return existing.id;
        }
        // 2) Add it (multipart endpoint creation endpoint).
        const fd = new FormData();
        fd.append('name', 'MiniMax Token Plan');
        fd.append('base_url', 'https://api.minimax.io/anthropic');
        fd.append('api_key', 'fake-key-for-test');
        fd.append('model_type', 'llm');
        const create = await fetch(base + '/api/model-endpoints', { method: 'POST', credentials: 'same-origin', body: fd });
        const created = await create.json();
        // 3) Pin M3 so /api/models surfaces it without needing to
        //    probe the (unreachable in tests) /v1/models endpoint.
        if (created && created.id) {
            await fetch(base + '/api/model-endpoints/' + created.id, {
                method: 'PATCH',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pinned_models: ['MiniMax-M3'] }),
            });
        }
        return created && created.id;
    }""", {"base": base})
    mobile_page.wait_for_timeout(500)
