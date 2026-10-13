/* First-party account UI. Session token stays in HttpOnly cookies, not JS/storage. */
(function () {
  'use strict';
  const rawFetch = window.fetch.bind(window);
  const state = { user: null, csrf: '', config: null, mode: 'signup', widget: null, token: '', dialog: null };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  async function json(path, options = {}) {
    const r = await fetch(path, options);
    const data = await r.json();
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Please check the form and try again.');
    return data;
  }
  // All old page-specific API clients also get the CSRF header. Never send it
  // to cross-origin URLs, including a CAPTCHA/payment provider.
  window.fetch = (resource, options = {}) => {
    const url = new URL(resource instanceof Request ? resource.url : String(resource), location.href);
    const method = (options.method || (resource instanceof Request ? resource.method : 'GET')).toUpperCase();
    if (url.origin === location.origin && !['GET','HEAD','OPTIONS'].includes(method) && state.csrf) {
      const headers = new Headers(options.headers || (resource instanceof Request ? resource.headers : undefined));
      headers.set('X-CSRF-Token', state.csrf);
      options = {...options, headers};
    }
    return rawFetch(resource, options);
  };
  function notify() {
    document.querySelectorAll('[data-account-label]').forEach(el => el.textContent = state.user ? state.user.username : 'Log in');
    document.querySelectorAll('[data-auth-only]').forEach(el => el.hidden = !state.user);
    window.dispatchEvent(new CustomEvent('accountchange', {detail: state.user}));
    updateCountdown();
  }
  function updateCountdown() {
    const u = state.user;
    const seconds = u ? Math.max(0, Math.ceil(Math.max(u.trial_until, u.paid_until) - Date.now()/1000)) : 0;
    const text = !u ? 'SIGN IN TO START' : u.lifetime ? 'FOREVER ACCESS' : u.paid_until > Date.now()/1000 ? 'MONTHLY ACCESS' : seconds > 0 ? `TRIAL · ${String(seconds).padStart(2,'0')}s` : 'TRIAL ENDED · CHOOSE A PLAN';
    document.querySelectorAll('[data-access-clock]').forEach(el => { el.textContent = text; el.classList.toggle('expired', !!u && !u.lifetime && seconds <= 0); });
  }
  async function refresh() {
    try { const d = await json('/auth/status'); state.user = d.user; state.csrf = d.csrf; }
    catch { state.user = null; state.csrf = ''; }
    notify();
    return state.user;
  }
  let captchaScript;
  function loadCaptcha() {
    if (window.turnstile) return Promise.resolve();
    if (!captchaScript) captchaScript = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      script.onload = resolve;
      script.onerror = () => reject(new Error('Security check could not load. Please retry.'));
      document.head.appendChild(script);
    });
    return captchaScript;
  }
  async function mountCaptcha() {
    if (!state.config?.ready) return;
    try {
      await loadCaptcha();
      if (!state.dialog.open) return;
      state.widget = turnstile.render('#authCaptcha', {
        sitekey: state.config.site_key, action: state.mode, theme: 'dark', size: 'flexible',
        callback: token => { state.token = token; },
        'expired-callback': () => { state.token = ''; },
        'error-callback': () => { state.token = ''; document.getElementById('authError').textContent = 'Security check failed. Please retry.'; }
      });
    } catch (e) { document.getElementById('authError').textContent = e.message; }
  }
  function form() {
    const signup = state.mode === 'signup';
    if (state.widget !== null && window.turnstile) turnstile.remove(state.widget);
    state.widget = null; state.token = '';
    state.dialog.innerHTML = `
      <button type="button" class="modal-close" aria-label="Close account form">×</button>
      <div class="modal-mark">b<span>↗</span></div><p class="eyebrow">${signup ? 'YOUR NEXT HANDLE STARTS HERE' : 'BACK TO THE SIGNAL'}</p>
      <h2>${signup ? 'Claim your edge.' : 'Welcome back.'}</h2>
      <p class="auth-sub">${signup ? 'Create an account. One minute. All sixteen platforms.' : 'Log in to your workspace. Your runs are waiting.'}</p>
      <form id="authForm">
        <label>Username<input name="username" autocomplete="username" minlength="3" maxlength="24" pattern="[a-zA-Z0-9_]+" required placeholder="your_handle"></label>
        <label>Password<input name="password" type="password" autocomplete="${signup ? 'new-password' : 'current-password'}" minlength="12" maxlength="128" required placeholder="At least 12 characters"></label>
        <label class="honeypot" aria-hidden="true">Website<input name="website" tabindex="-1" autocomplete="off"></label>
        <div id="authCaptcha"></div>
        ${!state.config?.ready ? '<div class="setup-warning">Account setup is pending. CAPTCHA and server credentials must be configured before accounts can be created.</div>' : ''}
        <p id="authError" role="alert" class="form-error"></p>
        <button class="btn primary big auth-submit" ${!state.config?.ready ? 'disabled' : ''}>${signup ? 'Create account →' : 'Log in →'}</button>
      </form>
      <p class="auth-switch">${signup ? 'Already have an account? <button type="button" data-mode="login">Log in</button>' : 'New here? <button type="button" data-mode="signup">Create account</button>'}</p>
      <p class="auth-legal">${signup ? 'One trial per eligible IP/device. Shared networks may be ineligible. Changing both cannot be conclusively detected.' : 'Protected by a server-verified security challenge. No password or session token is saved in browser storage.'}</p>`;
    state.dialog.querySelector('.modal-close').onclick = () => state.dialog.close();
    state.dialog.querySelector('[data-mode]').onclick = e => { state.mode = e.target.dataset.mode; form(); };
    state.dialog.querySelector('form').onsubmit = async e => {
      e.preventDefault();
      const button = e.target.querySelector('.auth-submit'), error = document.getElementById('authError');
      if (!state.token) { error.textContent = 'Complete the security check first.'; return; }
      button.disabled = true; error.textContent = '';
      try {
        const fields = new FormData(e.target);
        const data = await json('/auth/' + state.mode, {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({username: fields.get('username'), password: fields.get('password'), website: fields.get('website'), captcha_token: state.token})});
        state.user = data.user; state.csrf = data.csrf;
        state.dialog.close(); notify();
        location.href = state.user.access ? '/console' : '/buy';
      } catch (err) {
        error.textContent = err.message; state.token = '';
        if (window.turnstile && state.widget !== null) turnstile.reset(state.widget);
      } finally { button.disabled = !state.config?.ready; }
    };
    mountCaptcha();
  }
  async function open(mode = 'signup') {
    await ready;
    if (!state.dialog) {
      state.dialog = document.createElement('dialog'); state.dialog.className = 'auth-dialog';
      document.body.appendChild(state.dialog);
      state.dialog.addEventListener('click', e => { if (e.target === state.dialog) {
        const r = state.dialog.getBoundingClientRect();
        if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) state.dialog.close();
      }});
    }
    state.mode = mode;
    if (!state.dialog.open) state.dialog.showModal();
    form();
  }
  const ready = (async () => {
    try { state.config = await json('/auth/config'); } catch { state.config = {ready:false}; }
    await refresh();
  })();
  document.addEventListener('click', async e => {
    const trigger = e.target.closest('[data-auth]');
    if (trigger) { e.preventDefault(); if (state.user) location.href = '/console'; else open(trigger.dataset.auth); }
    const logout = e.target.closest('[data-logout]');
    if (logout) { e.preventDefault(); try { await json('/auth/logout',{method:'POST'}); state.user=null; state.csrf=''; notify(); location.href='/'; } catch(err) { logout.textContent=err.message; } }
  });
  setInterval(updateCountdown, 1000);
  setInterval(() => { if (state.user && !document.hidden) refresh(); }, 15000);
  window.Auth = {ready, state, open, refresh, json, esc};
})();
