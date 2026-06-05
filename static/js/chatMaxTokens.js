// Per-chat max-tokens badge.
//
// Shows the current effective output cap next to the model picker in the
// chat input. Click → opens a small popover to override for this session
// (persists via PATCH /api/session/{id}/max_tokens). The badge colour
// changes when the user has explicitly set an override (distinct from
// inheriting the per-model / per-endpoint / global default).
//
// The effective cap is computed on the server in
// ``src.endpoint_resolver.resolve_max_tokens``. The client can't fully
// reproduce the 4-tier chain (no access to the global setting from
// arbitrary code paths), so the badge label is "best effort" — it shows
// either the explicit per-chat value (if any) or falls back to "auto".

// Same-origin as the page — no base path prefix needed (e.g. when the
// app is reverse-proxied at ``/odysseus``). Matches the convention used
// by the other self-contained modules (calendar.js, emailInbox.js, …).
const API_BASE = window.location.origin;

const _POPOVER_ID = 'chat-maxtokens-popover';
let _currentSessionId = null;
let _currentModelId = null;
let _currentEndpointBaseUrl = null;

function _$(id) { return document.getElementById(id); }

function _formatCap(n) {
  if (n == null) return 'auto';
  const v = Number(n);
  if (!Number.isFinite(v)) return 'auto';
  if (v === 0) return 'no limit';
  if (v >= 1024) {
    const k = v / 1024;
    if (Number.isInteger(k)) return `${k}K`;
    return `${k.toFixed(1)}K`;
  }
  return String(v);
}

function _setBadgeFromSession(sess) {
  const btn = _$('chat-maxtokens-btn');
  const label = _$('chat-maxtokens-label');
  if (!btn || !label) return;
  const v = sess && Object.prototype.hasOwnProperty.call(sess, 'max_tokens')
    ? sess.max_tokens : null;
  if (v == null) {
    label.textContent = 'cap: auto';
    btn.classList.remove('is-overridden');
    btn.title = 'Per-chat max output tokens (currently inheriting from per-model → per-endpoint → global default)';
  } else {
    label.textContent = `cap: ${_formatCap(v)}`;
    btn.classList.add('is-overridden');
    btn.title = `Per-chat max output tokens (overridden to ${v === 0 ? 'no limit' : v + ' tokens'})`;
  }
}

function _closePopover() {
  const p = _$(_POPOVER_ID);
  if (p) p.remove();
  document.removeEventListener('click', _onDocClick, true);
  document.removeEventListener('keydown', _onKey, true);
}

function _onDocClick(e) {
  const p = _$(_POPOVER_ID);
  if (!p) { _closePopover(); return; }
  if (p.contains(e.target)) return;
  if (e.target.closest('#chat-maxtokens-btn')) return;
  _closePopover();
}

function _onKey(e) {
  if (e.key === 'Escape') _closePopover();
}

async function _save(value) {
  if (!_currentSessionId) {
    // No active session (welcome screen, or the model picker hasn't
    // reported a session yet). Surface a friendly error in the popover
    // status line instead of silently failing on a 404.
    const msg = document.querySelector(`#${_POPOVER_ID} .chat-maxtokens-msg`);
    if (msg) {
      msg.textContent = 'Open or start a chat first to set a per-chat cap.';
      msg.style.color = 'var(--red)';
    }
    return;
  }
  try {
    const r = await fetch(`${API_BASE}/api/session/${_currentSessionId}/max_tokens`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ max_tokens: value }),
    });
    if (!r.ok) {
      const t = await r.text();
      console.warn('max_tokens save failed', t);
      return;
    }
    // Update badge to reflect the new value.
    _setBadgeFromSession({ max_tokens: value });
    _closePopover();
  } catch (e) {
    console.warn('max_tokens save failed', e);
  }
}

async function _openPopover() {
  // The session ID is only needed when SAVING — it's not required to
  // open the popover. Letting the popover open even before the
  // model-picker has reported the active session (which is the case
  // on first paint and on the welcome screen) means the badge works
  // as soon as the page is interactive. If the user saves before a
  // session exists, ``_save`` falls back to a friendlier "open a chat
  // first" message instead of firing a 404 to /api/session/null/...
  _closePopover();
  const btn = _$('chat-maxtokens-btn');
  if (!btn) return;
  const pop = document.createElement('div');
  pop.id = _POPOVER_ID;
  pop.className = 'chat-maxtokens-popover';
  // Position via fixed (so it scrolls with the page correctly on mobile
  // keyboards) and a 6px gutter. We measure after insert and clamp so
  // the popover never spills off either edge of the viewport — earlier
  // this used `right: (innerWidth - rect.right)`, which on a 390px phone
  // placed the popover's right edge at the badge's right edge and then
  // let the left edge overflow the screen (e.g. x = -136 in tests).
  pop.style.cssText = 'position:fixed;z-index:1000;background:var(--bg);border:1px solid color-mix(in srgb, var(--fg) 18%, transparent);border-radius:8px;padding:12px;box-shadow:0 8px 24px rgba(0,0,0,0.18);min-width:220px;max-width:calc(100vw - 16px);box-sizing:border-box;font-size:12px;';
  pop.innerHTML = `
    <div style="font-weight:600;margin-bottom:6px;">Max output tokens for this chat</div>
    <div style="opacity:0.6;font-size:11px;margin-bottom:8px;">0 = no limit (provider decides). Leave blank to inherit the per-model / per-endpoint / global default.</div>
    <div style="display:flex;gap:6px;align-items:center;">
      <input type="number" id="chat-maxtokens-input" min="0" max="131072" step="256" placeholder="auto (inherit)" style="flex:1;min-width:0;padding:5px 8px;background:var(--bg);color:var(--fg);border:1px solid color-mix(in srgb, var(--fg) 18%, transparent);border-radius:4px;font-size:12px;">
      <button type="button" id="chat-maxtokens-save" class="admin-btn-sm">Save</button>
    </div>
    <div style="display:flex;gap:4px;margin-top:8px;flex-wrap:wrap;">
      <button type="button" class="admin-btn-sm" data-mt-quick="0">No limit</button>
      <button type="button" class="admin-btn-sm" data-mt-quick="4096">4K</button>
      <button type="button" class="admin-btn-sm" data-mt-quick="8192">8K</button>
      <button type="button" class="admin-btn-sm" data-mt-quick="16384">16K</button>
      <button type="button" class="admin-btn-sm" data-mt-quick="32768">32K</button>
      <button type="button" class="admin-btn-sm" data-mt-quick="65536">64K</button>
      <button type="button" class="admin-btn-sm" data-mt-quick="131072">128K</button>
      <button type="button" class="admin-btn-sm" data-mt-quick="">Inherit</button>
    </div>
    <div class="chat-maxtokens-msg" style="font-size:11px;margin-top:6px;min-height:1em;opacity:0.7;"></div>
  `;
  document.body.appendChild(pop);

  // Position the popover relative to the button. Anchor: right edge
  // of the popover aligns with the right edge of the button by default.
  // Then clamp: if the popover would overflow the left edge of the
  // viewport (the common mobile case — 240px popover on a 390px screen
  // anchored to a button near the right edge), pin the popover's LEFT
  // edge at 8px instead. Also clamp vertically: if the popover would
  // overflow the bottom (mobile keyboard up, tiny phones), flip it
  // above the button.
  const rect = btn.getBoundingClientRect();
  const popRect = pop.getBoundingClientRect();
  const margin = 6;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const pad = 8;

  // Desired right-edge x of the popover (same as button's right edge).
  let popRightX = rect.right;
  // If anchoring at that right edge pushes the left edge off-screen,
  // clamp the left edge to ``pad`` and let the right edge overflow
  // by the same amount (CSS will still render it on-screen because
  // we set ``left`` explicitly below).
  if (popRightX - popRect.width < pad) {
    // Switch to left-anchored positioning. The popover's left edge
    // sits at ``pad`` from the viewport's left edge.
    pop.style.left = pad + 'px';
    pop.style.right = 'auto';
  } else {
    pop.style.right = (vw - popRightX) + 'px';
    pop.style.left = 'auto';
  }

  // Vertical: try below the button, flip above if it overflows.
  let top = rect.bottom + margin;
  if (top + popRect.height > vh) {
    top = Math.max(pad, rect.top - margin - popRect.height);
  }
  pop.style.top = top + 'px';
  setTimeout(() => {
    document.addEventListener('click', _onDocClick, true);
    document.addEventListener('keydown', _onKey, true);
  }, 0);

  const input = pop.querySelector('#chat-maxtokens-input');
  const save = pop.querySelector('#chat-maxtokens-save');
  // Pre-fill with current value if known.
  try {
    const r = await fetch(`${API_BASE}/api/sessions`, { credentials: 'same-origin' });
    if (r.ok) {
      const all = (await r.json()) || [];
      const sess = all.find(s => s.id === _currentSessionId);
      if (sess && sess.max_tokens != null) input.value = String(sess.max_tokens);
    }
  } catch (_) {}
  input.focus();

  const _doSave = () => {
    const raw = (input.value || '').trim();
    if (raw === '') { _save(null); return; }
    const n = parseInt(raw, 10);
    if (!Number.isFinite(n) || n < 0) { input.focus(); return; }
    _save(n);
  };
  save.addEventListener('click', _doSave);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); _doSave(); } });
  pop.querySelectorAll('[data-mt-quick]').forEach(b => {
    b.addEventListener('click', () => {
      const v = b.dataset.mtQuick;
      _save(v === '' ? null : parseInt(v, 10));
    });
  });
}

export function initChatMaxTokens() {
  const btn = _$('chat-maxtokens-btn');
  if (!btn) return;
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _openPopover();
  });
  // Initial label: hidden until we know the session.
  _setBadgeFromSession(null);
}

// Re-render the badge label / colour when the session changes. Called by
// the chat module after a new session is created or an existing one is
// opened.
export function updateChatMaxTokensBadge(session) {
  _currentSessionId = session?.id || null;
  _currentModelId = session?.model || null;
  _currentEndpointBaseUrl = session?.endpoint_url || null;
  _setBadgeFromSession(session);
}
