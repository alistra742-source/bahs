/* First-party account UI. Session token stays in HttpOnly cookies, not JS/storage. */
(function () {
  'use strict';
  const rawFetch = window.fetch.bind(window);
  const state = { user: null, csrf: '', config: null, mode: 'signup', widget: null, token: '', dialog: null, challengeData: null };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  
  async function json(path, options = {}) {
    const r = await fetch(path, options);
    const data = await r.json();
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Please check the form and try again.');
    return data;
  }
  
  // All old page-specific API clients also get the CSRF header.
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
      script.onerror = () => reject(new Error('Turnstile could not load.'));
      document.head.appendChild(script);
    });
    return captchaScript;
  }
  
  async function setupSecurityChallenge() {
    const box = document.getElementById('authCaptcha');
    if (!box) return;
    
    // If Turnstile is configured on backend with live keys
    if (state.config?.turnstile && state.config?.site_key) {
      try {
        await loadCaptcha();
        if (!state.dialog.open) return;
        state.widget = turnstile.render('#authCaptcha', {
          sitekey: state.config.site_key, action: state.mode, theme: 'dark', size: 'flexible',
          callback: token => { state.token = token; const b = document.querySelector('.auth-submit'); if (b) b.disabled = false; },
          'expired-callback': () => { state.token = ''; },
          'error-callback': () => { state.token = ''; fallbackChallenge(); }
        });
        return;
      } catch (e) {
        // Fallback to integrated tactical challenge
      }
    }
    
    fallbackChallenge();
  }

  async function fallbackChallenge() {
    const box = document.getElementById('authCaptcha');
    if (!box) return;
    try {
      const ch = await json('/auth/challenge?action=' + encodeURIComponent(state.mode));
      state.challengeData = ch;
      state.token = '';
      box.innerHTML = `
        <div class="vortex-captcha-box" style="background:#0e0e13;border:1px solid #2d2d38;border-radius:4px;padding:12px;margin:10px 0;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="font:10px var(--mono);color:#c97a45;letter-spacing:0.1em;">TACTICAL BOT DETECTION // 3-PHASE</span>
            <span style="font:9px var(--mono);color:#7a7a85;">CLICK MATCHING GLYPHS</span>
          </div>
          <div style="font:11px var(--mono);color:#d0d0d8;margin-bottom:10px;">Select glyphs: <b style="color:#c97a45;">${ch.required_glyphs.join(' ')}</b></div>
          <div class="vortex-glyphs" style="display:flex;gap:8px;margin-bottom:10px;">
            ${ch.glyphs.map((g, idx) => `<button type="button" class="glyph-btn" data-glyph="${g}" style="flex:1;padding:8px 0;background:#16161f;border:1px solid #333340;color:#e8e8ea;font-size:16px;cursor:pointer;border-radius:3px;">${g}</button>`).join('')}
          </div>
          <div style="margin-top:6px;">
            <div style="font:9px var(--mono);color:#888894;margin-bottom:4px;display:flex;justify-content:space-between;">
              <span>CALIBRATION FREQUENCY:</span> <span id="freqVal">0%</span>
            </div>
            <input type="range" id="freqSlider" min="0" max="100" value="0" style="width:100%;accent-color:#c97a45;cursor:pointer;">
          </div>
          <div id="captchaStatus" style="font:9px var(--mono);color:#888894;margin-top:8px;text-align:right;">[ CHALLENGE UNRESOLVED ]</div>
        </div>
      `;
      
      const selectedGlyphs = new Set();
      const slider = box.querySelector('#freqSlider');
      const status = box.querySelector('#captchaStatus');
      const freqVal = box.querySelector('#freqVal');
      
      box.querySelectorAll('.glyph-btn').forEach(btn => {
        btn.onclick = () => {
          const g = btn.dataset.glyph;
          if (selectedGlyphs.has(g)) {
            selectedGlyphs.delete(g);
            btn.style.borderColor = '#333340';
            btn.style.background = '#16161f';
          } else {
            selectedGlyphs.add(g);
            btn.style.borderColor = '#c97a45';
            btn.style.background = '#281c14';
          }
          checkResolution();
        };
      });
      
      slider.oninput = () => {
        freqVal.textContent = slider.value + '%';
        checkResolution();
      };
      
      function checkResolution() {
        const hasAllGlyphs = ch.required_glyphs.every(req => selectedGlyphs.has(req));
        const glyphsOk = hasAllGlyphs && selectedGlyphs.size === ch.required_glyphs.length;
        const sliderOk = parseInt(slider.value, 10) >= ch.target;
        
        if (glyphsOk && sliderOk) {
          state.token = ch.challenge;
          status.textContent = '✓ VERIFIED HUMAN OPERATOR';
          status.style.color = '#4a7c59';
          document.getElementById('authError').textContent = '';
          const submitBtn = document.querySelector('.auth-submit');
          if (submitBtn) submitBtn.disabled = false;
        } else {
          state.token = '';
          status.textContent = !glyphsOk ? '[ GLYPH MISMATCH ]' : '[ ALIGN FREQUENCY >= ' + ch.target + '% ]';
          status.style.color = '#c97a45';
        }
      }
    } catch(err) {
      box.innerHTML = `<div style="font:10px var(--mono);color:#c18a80;">Challenge sync error: ${esc(err.message)}</div>`;
    }
  }

  function form() {
    const signup = state.mode === 'signup';
    if (state.widget !== null && window.turnstile) turnstile.remove(state.widget);
    state.widget = null; state.token = '';
    state.dialog.innerHTML = `
      <button type="button" class="modal-close" aria-label="Close account form">×</button>
      <div class="modal-mark">VORTEX<span> // 01</span></div><p class="eyebrow">${signup ? 'IDENTITY & SCAN INITIALIZATION' : 'ACCESS GATEWAY'}</p>
      <h2>${signup ? 'Claim your edge.' : 'Welcome back.'}</h2>
      <p class="auth-sub">${signup ? 'Create an account. 60-second trial across all 16 platforms.' : 'Log in to your workspace. Your runs and hits are ready.'}</p>
      <form id="authForm">
        <label>Username (3-20 characters)
          <input name="username" autocomplete="username" minlength="3" maxlength="20" pattern="[a-zA-Z0-9_]+" required placeholder="handle_3_to_20">
        </label>
        <label>Password (3-20 characters)
          <input name="password" type="password" autocomplete="${signup ? 'new-password' : 'current-password'}" minlength="3" maxlength="20" required placeholder="3-20 characters">
        </label>
        <label class="honeypot" aria-hidden="true">Website<input name="website" tabindex="-1" autocomplete="off"></label>
        <div id="authCaptcha"></div>
        <p id="authError" role="alert" class="form-error"></p>
        <button class="btn primary big auth-submit" disabled>${signup ? 'Create account →' : 'Log in →'}</button>
      </form>
      <p class="auth-switch">${signup ? 'Already have an account? <button type="button" data-mode="login">Log in</button>' : 'New here? <button type="button" data-mode="signup">Create account</button>'}</p>
      <p class="auth-legal">${signup ? 'One trial per eligible IP/device. 60 seconds of full speed sweeps.' : 'Multi-tier anti-bot detection active. Passwords hashed with Argon2id.'}</p>`;
    
    state.dialog.querySelector('.modal-close').onclick = () => state.dialog.close();
    state.dialog.querySelector('[data-mode]').onclick = e => { state.mode = e.target.dataset.mode; form(); };
    state.dialog.querySelector('form').onsubmit = async e => {
      e.preventDefault();
      const button = e.target.querySelector('.auth-submit'), error = document.getElementById('authError');
      error.textContent = '';
      button.disabled = true;
      button.textContent = signup ? 'Creating identity…' : 'Authenticating…';
      
      try {
        // Fail closed: the security check must be solved before anything is sent.
        if (!state.token) throw new Error('Complete the security check first.');

        const fields = new FormData(e.target);
        const data = await json('/auth/' + state.mode, {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            username: fields.get('username'),
            password: fields.get('password'),
            website: fields.get('website'),
            captcha_token: state.token
          })
        });
        state.user = data.user; state.csrf = data.csrf;
        state.dialog.close(); notify();
        location.href = state.user.access ? '/console' : '/buy';
      } catch (err) {
        error.textContent = err.message;
        state.token = '';
        button.disabled = false;
        button.textContent = signup ? 'Create account →' : 'Log in →';
        setupSecurityChallenge();
      }
    };
    setupSecurityChallenge();
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
    try { state.config = await json('/auth/config'); } catch { state.config = {ready:true}; }
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
