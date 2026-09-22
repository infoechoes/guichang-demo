/* Login preferences only. Passwords and session tokens never enter web storage. */
(() => {
  'use strict';
  const key = 'gc.login.preferences.v1';
  let preferences = {};
  try { preferences = JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (_) {}
  const save = value => { preferences = value; try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {} };
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.origin !== location.origin || url.pathname !== '/api/login' || !init || typeof init.body !== 'string') return originalFetch(input, init);
    const data = JSON.parse(init.body);
    const rememberAccount = !!document.querySelector('#gc-remember-account')?.checked;
    const remember = !!document.querySelector('#gc-remember-session')?.checked;
    const response = await originalFetch(input, {...init, body: JSON.stringify({...data, remember})});
    if (response.ok) save({username: rememberAccount ? data.username : '', rememberAccount, remember});
    return response;
  };
  function mount() {
    const username = document.querySelector('input[autocomplete="username"]');
    const form = username?.closest('form');
    if (!form || form.querySelector('#gc-login-preferences')) return;
    const box = document.createElement('div');
    box.id = 'gc-login-preferences';
    box.style.cssText = 'display:grid;gap:8px;margin:12px 0;font-size:13px';
    box.innerHTML = '<label style="display:flex;align-items:center;gap:8px"><input id="gc-remember-account" type="checkbox" style="width:auto;margin:0">记住账号</label><label style="display:flex;align-items:center;gap:8px"><input id="gc-remember-session" type="checkbox" style="width:auto;margin:0">30 天内自动登录（个人电脑适用）</label>';
    form.insertBefore(box, form.querySelector('button[type="submit"]') || form.querySelector('button'));
    const account = box.querySelector('#gc-remember-account');
    const session = box.querySelector('#gc-remember-session');
    account.checked = preferences.rememberAccount !== false;
    session.checked = preferences.remember === true;
    account.addEventListener('change', () => { if (!account.checked) save({...preferences, username:'', rememberAccount:false}); });
    if (account.checked && typeof preferences.username === 'string' && preferences.username && !username.value) {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(username, preferences.username);
      username.dispatchEvent(new Event('input', {bubbles:true}));
    }
  }
  new MutationObserver(mount).observe(document.documentElement, {childList:true,subtree:true});
  document.addEventListener('DOMContentLoaded', mount);
})();
