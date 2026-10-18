/* First-party account UI. Session token stays in HttpOnly cookies, not JS/storage. */
(function () {
  'use strict';
  const rawFetch = window.fetch.bind(window);
  const state = { user: null, csrf: '', config: null, mode: 'signup', challenge: null, dragOk: false, dragValue: -1, dialog: null };
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
    const now = Date.now()/1000;
    // An unstarted trial has no deadline yet -- the clock starts on the first
    // run -- so it reads as its full window until then.
    const full = Number(u && u.trial_seconds) || 60;
    const seconds = !u ? 0 : u.trial_pending ? full : Math.max(0, Math.ceil(Math.max(u.trial_until, u.paid_until) - now));
    const clock = String(seconds).padStart(2,'0') + 's';
    const text = !u ? 'SIGN IN TO START' : u.lifetime ? 'FOREVER ACCESS' : u.paid_until > now ? 'MONTHLY ACCESS' : u.trial_pending ? `TRIAL READY · ${clock}` : seconds > 0 ? `TRIAL · ${clock}` : 'TRIAL ENDED · CHOOSE A PLAN';
    document.querySelectorAll('[data-access-clock]').forEach(el => { el.textContent = text; el.classList.toggle('expired', !!u && !u.lifetime && !u.trial_pending && seconds <= 0); });
  }
  
  async function refresh() {
    try { const d = await json('/auth/status'); state.user = d.user; state.csrf = d.csrf; }
    catch { state.user = null; state.csrf = ''; }
    notify();
    return state.user;
  }

  // Human check: drag the chip onto the target, then type the distorted word.
  // Submit stays disabled until both steps are done, and the server accepts one attempt per challenge.
  function updateSubmit() {
    const word = document.getElementById('vxWord');
    const submit = document.querySelector('.auth-submit');
    if (submit) submit.disabled = !(state.challenge && state.dragOk && word && word.value.trim().length >= 6);
  }

  async function setupSecurityChallenge() {
    const box = document.getElementById('authCaptcha');
    if (!box) return;
    const mode = state.mode;
    state.challenge = null; state.dragOk = false; state.dragValue = -1;
    updateSubmit();
    box.innerHTML = '<p class="auth-legal">Loading verification…</p>';
    try {
      const ch = await json('/auth/challenge?action=' + encodeURIComponent(mode));
      // Ignore a late response: the user may have switched between signup and login meanwhile.
      if (!state.dialog || !state.dialog.open || state.mode !== mode || !document.getElementById('authCaptcha')) return;
      state.challenge = ch;
      box.innerHTML = `
        <div class="vx-check">
          <p class="vx-step">1 / 2 · Drag the chip onto the target</p>
          <div class="vx-track" id="vxTrack">
            <div class="vx-zone" style="left:calc((100% - 44px) * ${Number(ch.target)} / 100)"></div>
            <div class="vx-knob" id="vxKnob" role="slider" tabindex="0" aria-label="Drag the chip onto the target" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">&#x27A4;</div>
          </div>
          <p class="vx-step">2 / 2 · Type the word you see</p>
          <img class="vx-word" src="${esc(ch.word_image)}" alt="Distorted verification word" draggable="false">
          <input id="vxWord" autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength="16" placeholder="type the word" aria-label="Type the word you see">
          <p id="vxStatus" class="vx-status" role="status">Drag the chip onto the target.</p>
        </div>`;
      wireChallenge(box, ch);
    } catch (err) {
      box.innerHTML = `<div class="vx-status">Verification could not load: ${esc(err.message)}</div>`;
    }
  }

  function wireChallenge(box, ch) {
    const track = box.querySelector('#vxTrack'), knob = box.querySelector('#vxKnob');
    const status = box.querySelector('#vxStatus'), word = box.querySelector('#vxWord');
    let dragging = false, value = 0;
    const travel = () => Math.max(1, track.clientWidth - knob.offsetWidth);
    const place = px => {
      const x = Math.min(Math.max(px, 0), travel());
      knob.style.transform = `translateX(${x}px)`;
      value = Math.round(x / travel() * 100);
      knob.setAttribute('aria-valuenow', String(value));
    };
    const pxFromEvent = e => e.clientX - track.getBoundingClientRect().left - knob.offsetWidth / 2;

    knob.addEventListener('pointerdown', e => {
      dragging = true; state.dragOk = false; updateSubmit();
      knob.setPointerCapture(e.pointerId); knob.classList.add('dragging');
      place(pxFromEvent(e)); e.preventDefault();
    });
    knob.addEventListener('pointermove', e => { if (dragging) place(pxFromEvent(e)); });
    const release = () => {
      if (!dragging) return;
      dragging = false; knob.classList.remove('dragging');
      const ok = Math.abs(value - ch.target) <= ch.tolerance;
      state.dragOk = ok; state.dragValue = value;
      if (ok) {
        place(travel() * ch.target / 100);
        status.textContent = 'On target. Now type the word.';
        word.focus();
      } else {
        place(0);
        status.textContent = 'Not on target. Drag the chip again.';
      }
      updateSubmit();
    };
    knob.addEventListener('pointerup', release);
    knob.addEventListener('pointercancel', release);
    knob.addEventListener('keydown', e => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      place(value * travel() / 100 + (e.key === 'ArrowRight' ? 2 : -2) * travel() / 100);
      state.dragOk = false; state.dragValue = value; updateSubmit();
      status.textContent = 'Use the arrow keys, then press Enter to confirm.';
    });
    knob.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      dragging = true; release();
    });
    word.oninput = updateSubmit;
  }

  function form() {
    const signup = state.mode === 'signup';
    state.challenge = null; state.dragOk = false; state.dragValue = -1;
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
      <p class="auth-legal">${signup ? 'Each new account starts with a 60-second trial of full-speed sweeps.' : 'Passwords hashed with Argon2id. Every login needs the same human check.'}</p>`;
    
    state.dialog.querySelector('.modal-close').onclick = () => state.dialog.close();
    state.dialog.querySelector('[data-mode]').onclick = e => { state.mode = e.target.dataset.mode; form(); };
    state.dialog.querySelector('form').onsubmit = async e => {
      e.preventDefault();
      const button = e.target.querySelector('.auth-submit'), error = document.getElementById('authError');
      error.textContent = '';
      button.disabled = true;
      button.textContent = signup ? 'Creating identity…' : 'Authenticating…';
      
      try {
        // Fail closed: the drag and the typed word must both be done, and only the solved challenge is sent.
        const word = document.getElementById('vxWord');
        if (!state.challenge || !state.dragOk || !word) throw new Error('Finish the verification first.');

        const fields = new FormData(e.target);
        const data = await json('/auth/' + state.mode, {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            username: fields.get('username'),
            password: fields.get('password'),
            website: fields.get('website'),
            challenge_id: state.challenge.id,
            drag: state.dragValue,
            word: word.value.trim()
          })
        });
        state.user = data.user; state.csrf = data.csrf;
        state.dialog.close(); notify();
        location.href = state.user.access ? '/console' : '/buy';
      } catch (err) {
        error.textContent = err.message;
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
