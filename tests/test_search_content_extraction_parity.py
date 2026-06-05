"""Content extraction behavior for the canonical services.search.content module."""

import pytest
import httpx

pytest.importorskip("bs4")

from services.search import content as service_content


class _FakeResponse:
    status_code = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b""

    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class _HttpErrorResponse:
    """A response that mirrors a real upstream 4xx/5xx: ``raise_for_status``
    raises :class:`httpx.HTTPStatusError` like httpx does in production."""

    status_code = 404
    headers = {"Content-Type": "text/plain"}
    content = b"Page not found"
    reason_phrase = "Page not found"
    text = "Page not found"

    def raise_for_status(self):
        request = httpx.Request("GET", "https://example.test/missing")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError(
            f"{self.status_code} {self.reason_phrase}",
            request=request,
            response=response,
        )


@pytest.mark.parametrize("module", [service_content])
def test_content_fetcher_extracts_og_image_and_body_fallback(module, tmp_path, monkeypatch):
    html = """
    <html>
      <head>
        <title>Example</title>
        <meta property="og:image" content="https://example.com/cover.jpg">
      </head>
      <body>
        <nav>Navigation text should not win</nav>
        <div class="content">Tiny</div>
        <main>
          <p>This is the substantive body text that should be retained.</p>
          <p>It is much longer than the tiny class-matched wrapper.</p>
        </main>
        <script>window.secret = "not content";</script>
      </body>
    </html>
    """

    monkeypatch.setattr(module, "CONTENT_CACHE_DIR", tmp_path)
    module.content_cache_index.clear()
    monkeypatch.setattr(module, "_get_public_url", lambda url, headers, timeout: _FakeResponse(html))

    result = module.fetch_webpage_content("https://example.com/parity-test")

    assert result["og_image"] == "https://example.com/cover.jpg"
    assert "substantive body text" in result["content"]
    assert "much longer than the tiny" in result["content"]
    assert "window.secret" not in result["content"]


@pytest.mark.parametrize("module", [service_content])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
def test_content_fetcher_returns_empty_result_on_http_error(module, tmp_path, monkeypatch, status):
    """Regression: pasting a URL that 4xx/5xx (e.g. an API base URL that
    only serves ``/v1/messages`` and ``/v1/models``, like MiniMax's
    Anthropic-style endpoint) must NOT raise. A bad link in a chat
    message would otherwise 500 the whole request. The function returns
    ``success=False`` and the caller's ``if result.get('success')``
    gate skips injecting it into the context preface.
    """
    err = _HttpErrorResponse()
    err.status_code = status
    err.reason_phrase = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                         404: "Page not found", 500: "Internal Server Error",
                         502: "Bad Gateway", 503: "Service Unavailable"}[status]
    monkeypatch.setattr(module, "CONTENT_CACHE_DIR", tmp_path)
    module.content_cache_index.clear()
    monkeypatch.setattr(module, "_get_public_url", lambda url, headers, timeout: err)

    result = module.fetch_webpage_content("https://api.minimax.io/anthropic")

    assert result["success"] is False
    assert result["content"] == ""
    assert f"HTTP {status}" in result["error"]
