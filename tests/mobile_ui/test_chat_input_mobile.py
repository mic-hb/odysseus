"""Mobile UI regression tests for the chat input bar.

These tests load the running app at a mobile viewport and assert on
the visibility / geometry of the chat input controls. The bugs
captured here were reported by a user on iPhone 14 portrait (390x844
@2x) in Chrome on Android at the same nominal viewport:

1. The Agent/Chat mode toggle was hidden because the chat input bar
   is narrower than 340px on portrait, which the CSS deliberately
   collapsed (line 2244 of static/style.css) to "sacrifice chrome
   before the typing area" — but the toggle IS a primary control,
   not chrome. Users couldn't switch between Agent and Chat modes
   on mobile at all.

2. The per-chat max-tokens popover overflowed the right edge of
   the screen on narrow viewports because the JS positioned it
   with `right: (innerWidth - rect.right) + 'px'`, which works on
   desktop (where the button is well inside the viewport) but goes
   off-screen on mobile (where the button is near the right edge
   of a 390px viewport).
"""
import pytest


def _ensure_chat_visible(mobile_page, base_url):
    """Navigate to the main page and force the chat container into a
    visible state regardless of what the user had open in the live DB.

    Real users land on the app in three states: (a) the welcome
    screen, (b) an existing chat, or (c) an existing chat with a
    document panel open (``body.doc-view`` hides the chat behind
    the doc). For these tests we want the chat visible, so we strip
    ``doc-view`` / ``welcome-active`` and pick the most recent
    chat session if there is one.
    """
    mobile_page.goto(base_url + "/", wait_until="domcontentloaded")
    # Wait for the splash loader to fade out (it's set to ~5s in
    # index.html and would otherwise intercept pointer events).
    try:
        mobile_page.wait_for_selector("#app-loader", state="detached", timeout=10_000)
    except Exception:
        pass
    # Let the page finish bootstrapping (model picker, session load, etc.)
    mobile_page.wait_for_timeout(1500)
    mobile_page.evaluate("""() => {
        // Force the chat into "active chat" state. ``doc-view`` on body
        // hides the chat behind a doc panel — strip it for the test.
        document.body.classList.remove('doc-view', 'notes-view', 'gallery-view');
        const cc = document.getElementById('chat-container');
        if (cc) cc.classList.remove('welcome-active');
    }""")
    # Pick the first session from the sidebar so the chat bar is in
    # its "active chat" geometry rather than the welcome layout.
    sessions = mobile_page.evaluate("""async () => {
        try {
            const r = await fetch('/api/sessions', { credentials: 'same-origin' });
            if (!r.ok) return [];
            return await r.json();
        } catch (e) { return []; }
    }""")
    if sessions and isinstance(sessions, list) and len(sessions) > 0:
        first = sessions[0].get('id')
        if first:
            try:
                mobile_page.evaluate(f"window.selectSession && window.selectSession('{first}')")
            except Exception:
                pass
    mobile_page.wait_for_timeout(500)


def test_agent_chat_toggle_visible_on_portrait(mobile_page, base_url):
    """The Agent/Chat toggle must be visible on iPhone 14 portrait.

    Before the fix: the .chat-input-bar container query hid the toggle
    when the bar was <= 340px. On a 390px viewport the bar is ~362px
    (after page padding), right at the threshold; on a 375px iPhone
    (iPhone 12/13 mini) or with a chat sidebar partially open the
    toggle is hidden entirely.
    """
    _ensure_chat_visible(mobile_page, base_url)
    mobile_page.wait_for_selector(".mode-toggle", state="attached", timeout=5_000)

    toggle = mobile_page.locator(".mode-toggle")
    # The toggle is in the DOM (we waited for state="attached") — now
    # the question is whether it's actually painted on the viewport.
    assert toggle.is_visible(), (
        "Agent/Chat mode toggle is hidden on iPhone portrait. "
        "BBox: " + str(toggle.bounding_box())
    )

    # Both buttons inside the toggle should be present and tappable.
    agent_btn = mobile_page.locator("#mode-agent-btn")
    chat_btn = mobile_page.locator("#mode-chat-btn")
    assert agent_btn.is_visible(), "Agent button missing on mobile"
    assert chat_btn.is_visible(), "Chat button missing on mobile"
    assert agent_btn.bounding_box()["width"] > 0
    assert chat_btn.bounding_box()["width"] > 0


def test_agent_chat_toggle_visible_on_landscape(mobile_landscape_page, base_url):
    """Toggle should also be visible on landscape (more horizontal room)."""
    _ensure_chat_visible(mobile_landscape_page, base_url)
    mobile_landscape_page.wait_for_selector(".mode-toggle", state="attached", timeout=5_000)
    assert mobile_landscape_page.locator(".mode-toggle").is_visible()


def test_max_tokens_popover_stays_in_viewport(mobile_page, base_url):
    """The per-chat max-tokens popover must stay within the viewport.

    Before the fix: the popover was positioned with
    `right: (innerWidth - rect.right) + 'px'`, which on a 390px
    viewport (where the button is right at the edge) produced a
    negative or zero offset, pushing the popover off-screen to the
    right.

    The fix: anchor the popover to the button with a CSS-friendly
    absolute positioning relative to a positioned wrapper, or clamp
    its right edge so it never extends past ``innerWidth - 8px``.
    """
    _ensure_chat_visible(mobile_page, base_url)
    mobile_page.wait_for_selector("#chat-maxtokens-btn", state="attached", timeout=5_000)

    # The badge is in the DOM. Whether it's visible depends on the
    # current chat state — if it's hidden by some other code path,
    # we still want to know (that's a real bug too).
    btn = mobile_page.locator("#chat-maxtokens-btn")
    assert btn.is_visible(), "Max-tokens badge missing on mobile"
    btn.click()

    # The popover should appear and fit within the viewport.
    mobile_page.wait_for_selector("#chat-maxtokens-popover", timeout=2_000)
    popover = mobile_page.locator("#chat-maxtokens-popover")
    box = popover.bounding_box()
    viewport_w = mobile_page.viewport_size["width"]
    viewport_h = mobile_page.viewport_size["height"]

    assert box is not None, "Popover has no bounding box"
    assert box["x"] >= 0, f"Popover left edge is off-screen: x={box['x']}"
    assert box["x"] + box["width"] <= viewport_w + 1, (
        f"Popover right edge ({box['x'] + box['width']}) exceeds viewport width ({viewport_w})"
    )
    assert box["y"] >= 0, f"Popover top edge is off-screen: y={box['y']}"
    assert box["y"] + box["height"] <= viewport_h + 1, (
        f"Popover bottom edge ({box['y'] + box['height']}) exceeds viewport height ({viewport_h})"
    )


def test_max_tokens_popover_stays_in_viewport_on_desktop(desktop_page, base_url):
    """Desktop control: the mobile popover fix must not regress desktop.

    On a 1280px viewport the popover should anchor to the button's
    right edge (the default position) and stay well within the screen
    (the button is in the chat input bar, nowhere near the edge).
    """
    _ensure_chat_visible(desktop_page, base_url)
    btn = desktop_page.locator("#chat-maxtokens-btn")
    assert btn.is_visible(), "Max-tokens badge missing on desktop"
    btn.click()
    desktop_page.wait_for_selector("#chat-maxtokens-popover", timeout=2_000)
    popover = desktop_page.locator("#chat-maxtokens-popover")
    box = popover.bounding_box()
    viewport_w = desktop_page.viewport_size["width"]
    assert box is not None
    assert box["x"] >= 0
    assert box["x"] + box["width"] <= viewport_w + 1
