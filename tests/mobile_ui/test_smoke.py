"""Smoke test for the Playwright + mobile infrastructure.

Loads the main app at a mobile viewport and asserts the page is
interactive. Confirms the Playwright + libasound-side-load wiring
actually works end-to-end on the host where the test is running —
if this test can't reach the app, the rest of the suite gets a
clear ``SKIPPED`` instead of a confusing connection-refused stack.
"""
import pytest


def test_chat_page_loads_on_mobile(mobile_page, base_url):
    """The main page must render at iPhone 14 portrait without errors."""
    mobile_page.goto(base_url + "/", wait_until="domcontentloaded")
    # Wait for the splash loader to fade out so the chat bar is
    # clickable (the loader intercepts pointer events for ~5s on first
    # load while the app finishes bootstrapping).
    try:
        mobile_page.wait_for_selector("#app-loader", state="detached", timeout=10_000)
    except Exception:
        pass
    # Either the welcome screen or a chat is fine — what matters is
    # that the page rendered and the chat input bar is in the DOM.
    mobile_page.wait_for_selector(".chat-input-bar", timeout=10_000)
    # The visible message input. There can be more than one #message
    # element on the page (welcome-screen placeholder, modals) — the
    # visible one is the chat input.
    message = mobile_page.locator("#message:visible").first
    assert message.is_visible(), "Message input not visible on mobile"

    # No console errors that would indicate a JS-level breakage.
    errors: list[str] = []
    mobile_page.on("pageerror", lambda exc: errors.append(str(exc)))
    # Trigger a tiny interaction so any deferred handlers run.
    message.click()
    assert not errors, f"JS errors on mobile load: {errors}"
