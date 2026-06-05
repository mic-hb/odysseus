"""Shared fixtures for the mobile-UI Playwright tests.

Connects to a running Odysseus instance over HTTP. The base URL is read
from the ``ODYSSEUS_BASE_URL`` env var (default ``http://127.0.0.1:7000``)
so the same tests work against a local dev server, a docker-compose
stack, or a remote staging environment.

Two viewports are provided:

- ``mobile_page``        — iPhone 14 portrait, 390x844 @2x. Use this
                          for the mobile-bug tests.
- ``mobile_landscape``   — iPhone 14 landscape, 844x390 @2x. Use this
                          to verify behaviour at the same physical device
                          rotated.

Both contexts share a single browser instance per test (pytest-playwright
already does this) and authenticate against the dev admin user if the
``ODYSSEUS_ADMIN_USER`` / ``ODYSSEUS_ADMIN_PASSWORD`` env vars are set;
otherwise the fixtures expect the server to be unauthenticated (single-user
mode) and skip the login form.

Chrome / Chromium dependency note: on Ubuntu 24.04 the prebuilt
chromium-1223 binary needs ``libasound.so.2`` which isn't installed
on minimal dev hosts. We extract it from the ``libasound2t64`` apt
package into ``/tmp/playwright-libs`` on first run and prepend that
to ``LD_LIBRARY_PATH`` so the browser can launch. The .deb is fetched
via ``apt-get download`` (no sudo needed) and unpacked with
``ar`` + ``tar --use-compress-program=unzstd``; the result is cached
in ``/tmp`` and reused on subsequent runs.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import socket
import ctypes
import pytest


def _ensure_chromium_libs() -> None:
    """Provide libasound.so.2 to the bundled chromium binary if missing.

    The Playwright-managed chromium 1223 links against ``libasound.so.2``;
    if the system doesn't have it (common on slim dev images), the browser
    fails to start with exit code 127. We side-load the library from the
    matching Ubuntu package without needing root.
    """
    # Already loadable? Nothing to do.
    try:
        ctypes.CDLL("libasound.so.2")
        return
    except OSError:
        pass

    lib_dir = "/tmp/playwright-libs/usr/lib/x86_64-linux-gnu"
    if os.path.exists(os.path.join(lib_dir, "libasound.so.2")):
        os.environ["LD_LIBRARY_PATH"] = (
            lib_dir + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
        )
        return

    # No lib, no cache. Fetch + extract the apt package.
    workdir = tempfile.mkdtemp(prefix="odysseus-playwright-")
    try:
        deb = os.path.join(workdir, "libasound2t64.deb")
        subprocess.run(
            ["apt-get", "download", "libasound2t64"],
            cwd=workdir, check=True, capture_output=True,
        )
        extracted = os.path.join(workdir, "extracted")
        os.makedirs(extracted)
        subprocess.run(["ar", "x", os.path.basename(deb)], cwd=extracted, check=True, capture_output=True)
        subprocess.run(
            ["tar", "--use-compress-program=unzstd", "-xf", "data.tar.zst"],
            cwd=extracted, check=True, capture_output=True,
        )
        os.makedirs(lib_dir, exist_ok=True)
        for f in os.listdir(os.path.join(extracted, "usr/lib/x86_64-linux-gnu")):
            shutil.copy2(
                os.path.join(extracted, "usr/lib/x86_64-linux-gnu", f),
                os.path.join(lib_dir, f),
            )
        os.environ["LD_LIBRARY_PATH"] = (
            lib_dir + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


_ensure_chromium_libs()


def _base_url() -> str:
    return os.environ.get("ODYSSEUS_BASE_URL", "http://127.0.0.1:7000").rstrip("/")


def _admin_user() -> str | None:
    return os.environ.get("ODYSSEUS_ADMIN_USER") or None


def _admin_password() -> str | None:
    return os.environ.get("ODYSSEUS_ADMIN_PASSWORD") or None


def _is_alive(base: str, timeout_s: float = 5.0) -> bool:
    """Return True if the Odysseus instance is up and serving.

    Used as a friendly skip when the user runs the mobile tests without a
    live server — pytest reports ``SKIPPED`` rather than the confusing
    "connection refused" stack trace the test would otherwise hit.
    """
    import urllib.request
    import urllib.error
    import socket
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/auth/status", timeout=1) as r:
                return 200 <= r.status < 500  # server up; auth state doesn't matter
        except (urllib.error.URLError, socket.timeout, ConnectionRefusedError, OSError):
            time.sleep(0.25)
    return False


def pytest_collection_modifyitems(config, items):
    """Auto-skip the mobile tests when no Odysseus instance is reachable.

    Saves the user from a hard "connection refused" failure when they
    run the suite without booting the app. The skip message points at
    ``ODYSSEUS_BASE_URL`` so the fix is one env var away.
    """
    base = _base_url()
    if _is_alive(base):
        return
    skip = pytest.mark.skip(
        reason=(
            f"Odysseus is not reachable at {base}. Boot the app "
            "(docker compose up -d, or python app.py) and re-run, "
            "or set ODYSSEUS_BASE_URL to a running instance."
        )
    )
    for item in items:
        if "mobile_ui" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def base_url() -> str:
    return _base_url()


@pytest.fixture(scope="session")
def browser():
    """Session-scoped browser launched with the right env for libasound.

    We don't use the ``browser`` fixture from pytest-playwright because
    it launches with a clean env (it scrubs ``LD_LIBRARY_PATH`` for
    security) and the bundled chromium-1223 needs ``libasound.so.2``
    on hosts that don't have it installed system-wide. Passing the
    full env explicitly via ``launch(env=...)`` is the supported way
    to side-load the library.
    """
    from playwright.sync_api import sync_playwright
    lib_dir = "/tmp/playwright-libs/usr/lib/x86_64-linux-gnu"
    env = dict(os.environ)
    if os.path.exists(lib_dir):
        env["LD_LIBRARY_PATH"] = lib_dir + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"], env=env)
        yield b
        b.close()


@pytest.fixture(scope="session")
def _authenticated_context(browser):
    """One-time login that all mobile tests reuse.

    Logs in as the admin user (if env vars are set) or relies on
    single-user mode (no auth). Returns the BrowserContext so the
    mobile fixtures inherit the auth cookie without re-logging in.
    """
    ctx = browser.new_context()
    user = _admin_user()
    password = _admin_password()
    if user and password:
        page = ctx.new_page()
        page.goto(_base_url() + "/login")
        page.fill('input[name="username"], input#username, input#email, input[name="user"]', user)
        page.fill('input[name="password"], input#password, input[type="password"]', password)
        page.click('button[type="submit"], button.login-btn, button#login-btn')
        # Wait for the redirect to the main page (URL no longer ends in
        # /login and the chat container is present).
        try:
            page.wait_for_url(lambda url: "/login" not in url, timeout=5000)
        except Exception:
            pass  # let the test surface a clearer error if auth truly failed
        page.close()
    yield ctx
    ctx.close()


@pytest.fixture
def mobile_page(_authenticated_context):
    """An iPhone-14-portrait page (390x844 @2x) ready to navigate.

    Yields a Playwright ``Page``. Tests can call ``page.goto(...)`` etc.
    directly; auth cookies are inherited from the session-level context.
    """
    page = _authenticated_context.new_page()
    page.set_viewport_size({"width": 390, "height": 844})
    yield page
    page.close()


@pytest.fixture
def mobile_landscape_page(_authenticated_context):
    """Same as ``mobile_page`` but rotated to landscape (844x390 @2x)."""
    page = _authenticated_context.new_page()
    page.set_viewport_size({"width": 844, "height": 390})
    yield page
    page.close()


@pytest.fixture
def desktop_page(_authenticated_context):
    """A standard desktop viewport (1280x800). Used as a control so a
    mobile-only fix doesn't accidentally regress the desktop layout."""
    page = _authenticated_context.new_page()
    page.set_viewport_size({"width": 1280, "height": 800})
    yield page
    page.close()
