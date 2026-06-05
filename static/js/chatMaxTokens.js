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
  if (!_currentSessionId) return;
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

function _openPopover() {
  if (!_currentSessionId) return;
  _closePopover();
  const btn = _$('chat-maxtokens-btn');
  if (!btn) return;
  const pop = document.createElement('div');
  pop.id = _POPOVER_ID;
  pop.className = 'chat-maxtokens-popover';
  pop.style.cssText = 'position:absolute;z-index:1000;background:var(--bg);border:1px solid color-mix(in srgb, var(--fg) 18%, transparent);border-radius:8px;padding:12px;box-shadow:0 8px 24px rgba(0,0,0,0.18);min-width:240px;font-size:12px;';
  pop.innerHTML = `
    <div style="font-weight:600;margin-bottom:6px;">Max output tokens for this chat</div>
    <div style="opacity:0.6;font-size:11px;margin-bottom:8px;">0 = no limit (provider decides). Leave blank to inherit the per-model / per-endpoint / global default.</div>
    <div style="display:flex;gap:6px;align-items:center;">
      <input type="number" id="chat-maxtokens-input" min="0" max="131072" step="256" placeholder="auto (inherit)" style="flex:1;padding:5px 8px;background:var(--bg);color:var(--fg);border:1px solid color-mix(in srgb, var(--fg) 18%, transparent);border-radius:4px;font-size:12px;">
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
  `;
  // Position next to the badge button.
  const rect = btn.getBoundingClientRect();
  pop.style.top = (rect.bottom + 6) + 'px';
  pop.style.right = (window.innerWidth - rect.right) + 'px';
  document.body.appendChild(pop);
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
