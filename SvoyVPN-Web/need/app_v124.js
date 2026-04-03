

// GLOBAL LOGOUT FOR WEBSITE
window.WEB_LOGOUT = function() {
    console.log('[Logout] WEB_LOGOUT called');
    localStorage.removeItem('svoyvpn_web_jwt');
    sessionStorage.clear();
    // Use location.origin + pathname to ensure clear reload
    window.location.href = window.location.origin + window.location.pathname; 
};

// === MODERN SVOYVPN AUTH SYSTEM (Mobile-level) ===
document.addEventListener('DOMContentLoaded', () => {

    const API = (window.AppConfig ? window.AppConfig.apiBaseURL : 'https://xdoublegroup.online') + '/api';
    let pollingInterval = null;
    let currentNonce = null;
    let _pendingRegEmail = '';

    // Elements
    const el = {
        tabTg: document.getElementById('tabTg'),
        tabEmail: document.getElementById('tabEmail'),
        panelTg: document.getElementById('panelTg'),
        panelEmail: document.getElementById('panelEmail'),
        
        subTabLogin: document.getElementById('subTabLogin'),
        subTabRegister: document.getElementById('subTabRegister'),
        
        loginForm: document.getElementById('webLoginForm'),
        registerForm: document.getElementById('webRegisterForm'),
        resetForm: document.getElementById('webResetForm'),
        
        btnTgLogin: document.getElementById('btnTgLogin'),
        pollingBox: document.getElementById('pollingBox'),
        tgError: document.getElementById('tgError'),
        
        loginEmail: document.getElementById('loginUsr'),
        loginPass: document.getElementById('loginKey'),
        loginError: document.getElementById('loginError'),
        btnLoginSubmit: document.getElementById('btnLoginSubmit'),
        btnShowReset: document.getElementById('btnShowReset'),
        
        regEmail: document.getElementById('regUsr'),
        regPass: document.getElementById('regKey1'),
        regPass2: document.getElementById('regKey2'),
        regOtp: document.getElementById('regOtp'),
        regError: document.getElementById('registerError'),
        regSuccess: document.getElementById('registerSuccess'),
        regStep1: document.getElementById('regStep1'),
        regStep2: document.getElementById('regStep2'),
        btnRegSendOtp: document.getElementById('btnRegSendOtp'),
        btnRegSubmit: document.getElementById('btnRegSubmit'),
        btnRegBack: document.getElementById('btnRegBack'),
        
        resetEmail: document.getElementById('resetEmail'),
        resetOtp: document.getElementById('resetOtp'),
        resetNewPass: document.getElementById('resetNewPass'),
        resetError: document.getElementById('resetError'),
        resetSuccess: document.getElementById('resetSuccess'),
        resetStep1: document.getElementById('resetStep1'),
        resetStep2: document.getElementById('resetStep2'),
        btnResetSendOtp: document.getElementById('btnResetSendOtp'),
        btnResetSubmit: document.getElementById('btnResetSubmit'),
        btnResetBack: document.getElementById('btnResetBack')
    };

    if (!el.tabTg) return;

    // Initial redirect if web and no token
    const _tgCheck = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    const isMobileAppCheck = (!!window.webkit && !!window.webkit.messageHandlers && !!window.webkit.messageHandlers.iOSBridge) || !!window.AndroidBridge;
    if (!isMobileAppCheck && !_tgCheck && !localStorage.getItem('svoyvpn_web_jwt')) {
        const authScreen = document.getElementById('screenAuth');
        if (authScreen) authScreen.classList.add('active');
    }

    // --- Helpers ---
    function haptic(style) {
        if (window.haptic) window.haptic(style);
        // On web we mostly do nothing or could use navigator.vibrate
    }

    function showError(element, msg) {
        if (!element) return;
        element.textContent = msg;
        element.classList.add('visible');
    }

    function hideError(element) {
        if (element) element.classList.remove('visible');
    }

    function notifySuccess(token) {
        localStorage.setItem('svoyvpn_web_jwt', token);
        location.reload();
    }

    // --- Tab Switching ---
    function switchTab(tab) {
        el.tabTg.classList.remove('active');
        el.tabEmail.classList.remove('active');
        el.panelTg.classList.remove('active');
        el.panelEmail.classList.remove('active');
        haptic('light');
        if (tab === 'tg') {
            el.tabTg.classList.add('active');
            el.panelTg.classList.add('active');
        } else {
            el.tabEmail.classList.add('active');
            el.panelEmail.classList.add('active');
        }
    }
    el.tabTg.onclick = () => switchTab('tg');
    el.tabEmail.onclick = () => switchTab('email');

    function switchEmailTab(t) {
        el.subTabLogin.classList.remove('active');
        el.subTabRegister.classList.remove('active');
        el.loginForm.style.display = 'none';
        el.registerForm.style.display = 'none';
        el.resetForm.style.display = 'none';
        haptic('light');
        if (t === 'login') {
            el.subTabLogin.classList.add('active');
            el.loginForm.style.display = 'block';
            el.loginEmail.value = '';
            el.loginPass.value = '';
        } else if (t === 'register') {
            el.subTabRegister.classList.add('active');
            el.registerForm.style.display = 'block';
            el.regEmail.value = '';
            el.regPass.value = '';
            el.regPass2.value = '';
        } else if (t === 'reset') {
            el.resetForm.style.display = 'block';
            el.resetEmail.value = '';
        }
    }
    el.subTabLogin.onclick = () => switchEmailTab('login');
    el.subTabRegister.onclick = () => switchEmailTab('register');
    el.btnShowReset.onclick = () => switchEmailTab('reset');
    el.btnResetBack.onclick = () => switchEmailTab('login');

    // --- Telegram Logic ---
    el.btnTgLogin.onclick = async () => {
        hideError(el.tgError);
        el.btnTgLogin.disabled = true;
        try {
            const r = await fetch(API + '/auth/tg-init', { method: 'POST' });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const data = await r.json();
            currentNonce = data.nonce;
            
            // Open bot in new tab/window for Web
            window.open(data.botUrl, '_blank');
            
            el.pollingBox.classList.add('visible');
            startPolling(data.nonce);
        } catch(e) {
            showError(el.tgError, 'Ошибка подключения: ' + e.message);
            el.btnTgLogin.disabled = false;
        }
    };

    function startPolling(nonce) {
        if (pollingInterval) clearInterval(pollingInterval);
        pollingInterval = setInterval(async () => {
            try {
                const r = await fetch(API + '/auth/tg-poll?nonce=' + nonce);
                const data = await r.json();
                if (data.status === 'ok' && data.token) {
                    clearInterval(pollingInterval);
                    el.pollingBox.classList.remove('visible');
                    notifySuccess(data.token);
                } else if (data.status === 'expired') {
                    clearInterval(pollingInterval);
                    el.pollingBox.classList.remove('visible');
                    el.btnTgLogin.disabled = false;
                    showError(el.tgError, 'Время ожидания истекло. Попробуйте ещё раз.');
                }
            } catch(e) { /* retry */ }
        }, 2000);
    }

    // --- Email Flow ---
    el.btnLoginSubmit.onclick = async () => {
        hideError(el.loginError);
        const email = el.loginEmail.value.trim();
        const pass = el.loginPass.value;
        if (!email || !pass) return showError(el.loginError, 'Заполните все поля');
        
        el.btnLoginSubmit.disabled = true;
        try {
            const r = await fetch(API + '/auth/login', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({email, password: pass})
            });
            const data = await r.json();
            if (!r.ok) return showError(el.loginError, data.error || data.detail || 'Ошибка входа');
            notifySuccess(data.token);
        } catch(e) { showError(el.loginError, 'Ошибка сети'); }
        el.btnLoginSubmit.disabled = false;
    };

    el.btnRegSendOtp.onclick = async () => {
        hideError(el.regError);
        const email = el.regEmail.value.trim();
        const pass = el.regPass.value;
        const pass2 = el.regPass2.value;
        if (!email || !pass) return showError(el.regError, 'Заполните все поля');
        if (pass !== pass2) return showError(el.regError, 'Пароли не совпадают');
        if (pass.length < 6) return showError(el.regError, 'Пароль минимум 6 символов');
        
        el.btnRegSendOtp.disabled = true;
        try {
            const r = await fetch(API + '/auth/email-otp', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({email, password: pass})
            });
            const data = await r.json();
            if (!r.ok) { showError(el.regError, data.error || data.detail || 'Ошибка'); el.btnRegSendOtp.disabled=false; return; }
            _pendingRegEmail = email;
            el.regStep1.style.display = 'none';
            el.regStep2.style.display = 'block';
            el.regSuccess.textContent = 'Код отправлен на ' + email;
            el.regSuccess.classList.add('visible');
        } catch(e) { showError(el.regError, 'Ошибка сети'); el.btnRegSendOtp.disabled=false; }
    };

    el.btnRegSubmit.onclick = async () => {
        hideError(el.regError);
        el.regSuccess.classList.remove('visible');
        const email = _pendingRegEmail || el.regEmail.value.trim();
        const otp = el.regOtp.value.trim();
        if (!otp) return showError(el.regError, 'Введите код');
        
        el.btnRegSubmit.disabled = true;
        try {
            const r = await fetch(API + '/auth/register', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({email, otp})
            });
            const data = await r.json();
            if (!r.ok) { showError(el.regError, data.error || data.detail || 'Ошибка'); el.btnRegSubmit.disabled=false; return; }
            if (data.token) notifySuccess(data.token);
        } catch(e) { showError(el.regError, 'Ошибка сети'); el.btnRegSubmit.disabled=false; }
    };
    el.btnRegBack.onclick = () => {
        el.regStep1.style.display = 'block';
        el.regStep2.style.display = 'none';
        hideError(el.regError);
        el.regSuccess.classList.remove('visible');
        el.btnRegSendOtp.disabled = false;
    };

    el.btnResetSendOtp.onclick = async () => {
        hideError(el.resetError);
        const email = el.resetEmail.value.trim();
        if (!email) return showError(el.resetError, 'Введите email');
        
        el.btnResetSendOtp.disabled = true;
        try {
            const r = await fetch(API + '/auth/reset-otp', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({email})
            });
            const data = await r.json();
            if (!r.ok) { showError(el.resetError, data.error || data.detail || 'Ошибка'); el.btnResetSendOtp.disabled=false; return; }
            el.resetStep1.style.display = 'none';
            el.resetStep2.style.display = 'block';
            el.resetSuccess.textContent = 'Код сброса отправлен';
            el.resetSuccess.classList.add('visible');
        } catch(e) { showError(el.resetError, 'Ошибка сети'); el.btnResetSendOtp.disabled=false; }
    };

    el.btnResetSubmit.onclick = async () => {
        hideError(el.resetError);
        const email = el.resetEmail.value.trim();
        const otp = el.resetOtp.value.trim();
        const pass = el.resetNewPass.value;
        if (!otp || pass.length < 6) return showError(el.resetError, 'Заполните поля корректно');
        
        el.btnResetSubmit.disabled = true;
        try {
            const r = await fetch(API + '/auth/reset-password', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({email, otp, password: pass})
            });
            const data = await r.json();
            if (!r.ok) { showError(el.resetError, data.error || data.detail || 'Ошибка'); el.btnResetSubmit.disabled=false; return; }
            el.resetSuccess.textContent = 'Пароль изменён! Входим...';
            el.resetSuccess.classList.add('visible');
            if (data.token) {
                setTimeout(() => notifySuccess(data.token), 1000);
            } else {
                setTimeout(() => switchEmailTab('login'), 2000);
            }
        } catch(e) { showError(el.resetError, 'Ошибка сети'); el.btnResetSubmit.disabled=false; }
    };

});

// --- SVOYVPN WEB ENGINE CONFIG ---
window.AppConfig = {
    apiBaseURL: 'https://xdoublegroup.online',
    isWeb: true,
    /** Если API не прислал refLink, собираем deep link сами */
    referralBotUsername: 'SvoyVPN_bot',
};

(function() {
    const _originalFetch = window.fetch;
    window.fetch = async (...args) => {
        let [resource, config] = args;
        let url = resource.toString();
        config = config || {};

        if (url.includes('/api/') || url.includes('/miniapp/')) {
            if (!url.startsWith('http')) {
                 const base = window.AppConfig.apiBaseURL;
                 url = base + (url.startsWith('/') ? url : '/' + url);
            }
        }

        const token = localStorage.getItem('svoyvpn_web_jwt') || window.__androidJwt;
        const skipWebJwt = config && config.skipWebJwt;
        
        if (token && !skipWebJwt) {
            config.headers = config.headers || {};
            const bearer = 'Bearer ' + token;
            if (config.headers instanceof Headers) {
                config.headers.set('Authorization', bearer);
            } else {
                config.headers['Authorization'] = bearer;
            }
        }
        return _originalFetch(url, config);
    };
})();

/* ═══════════════════════════════════════════
   SvoyVPN Miniapp — App Logic
   ═══════════════════════════════════════════ */
(function () {
  'use strict';

  /* ── Android WebView bridge ── */
  const urlParams = new URLSearchParams(window.location.search);
  const ANDROID_JWT = window.__androidJwt || urlParams.get('jwt') || null;
  const IS_ANDROID = !!ANDROID_JWT;
  const IS_IOS = !!window.webkit && !!window.webkit.messageHandlers && !!window.webkit.messageHandlers.iOSBridge;
  const IS_MOBILE_APP = IS_ANDROID || IS_IOS;
  const URL_THEME = urlParams.get('theme');

  let _tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const tg = _tg;

  window.onerror = function (msg, url, line, col, error) {
    const errDiv = document.createElement('div');
    errDiv.style.cssText = 'position:fixed;top:0;left:0;width:100%;background:red;color:white;z-index:9999;padding:10px;font-size:12px;line-height:1.2;';
    errDiv.innerHTML = `<b>JS Error:</b> ${msg}<br>Line: ${line}:${col}`;
    document.body.appendChild(errDiv);
    return false;
  };

  /* ── State ── */
  const S = {
    user: null,
    subscription: null,
    referral: null,
    /** true только если последний успешный loadUser шёл через svoyvpn_web_jwt (не Telegram / не Android JWT). */
    authViaWebJwt: false,
    tariffs: [],
    paymentMethods: [],
    servers: [],
    selectedTariff: null,
    selectedPM: null,
  };

  /** Единый формат рефералки только из ответа /api/user (camelCase / snake_case). */
  function normalizeReferralFromApi(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const code = raw.referralCode || raw.referral_code;
    if (!code) return null;
    let refLink = String(raw.refLink || raw.ref_link || '').trim();
    if (!refLink) {
      const bot =
        (window.AppConfig && window.AppConfig.referralBotUsername) || 'SvoyVPN_bot';
      refLink = 'https://t.me/' + bot + '?start=ref_' + encodeURIComponent(String(code));
    }
    return {
      referralCode: String(code),
      referralCount: Number(raw.referralCount ?? raw.referral_count ?? 0) || 0,
      inviterBonusDays: Number(raw.inviterBonusDays ?? raw.inviter_bonus_days ?? 5) || 5,
      invitedBonusDays: Number(raw.invitedBonusDays ?? raw.invited_bonus_days ?? 3) || 3,
      refLink: refLink,
    };
  }

  /* ═══════ Helpers ═══════ */
  const MW = ['месяц', 'месяца', 'месяцев'];
  function mw(n) {
    if (n % 10 === 1 && n % 100 !== 11) return MW[0];
    if ([2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100)) return MW[1];
    return MW[2];
  }

  const DW = ['день', 'дня', 'дней'];
  function dw(n) {
    if (n % 10 === 1 && n % 100 !== 11) return DW[0];
    if ([2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100)) return DW[1];
    return DW[2];
  }

  const fmtPrice = (p) =>
    Number(p).toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 0 });

  function fmtDate(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
  }

  const FLAGS_LONG = {
    '🇳🇱': ['netherland', 'nederland', 'нидерланды', 'голландия', 'amsterdam', 'амстердам'],
    '🇩🇪': ['germany', 'deutchland', 'германия', 'frankfurt', 'франкфурт'],
    '🇫🇷': ['france', 'франция', 'paris', 'париж'],
    '🇺🇸': ['usa', 'сша', 'america', 'united states', 'new york', 'нью йорк'],
    '🇬🇧': ['united kingdom', 'англия', 'london', 'лондон'],
    '🇵🇱': ['poland', 'польша', 'warsaw', 'варшава'],
    '🇹🇷': ['turkey', 'турция', 'istanbul', 'стамбул'],
    '🇰🇿': ['kazakhstan', 'казахстан', 'astana', 'almaty', 'астана', 'алматы'],
    '🇷🇺': ['russia', 'россия', 'moscow', 'москва'],
    '🇫🇮': ['finland', 'финляндия', 'helsinki', 'хельсинки'],
    '🇸🇪': ['sweden', 'швеция', 'stockholm', 'стокгольм'],
    '🇦🇹': ['austria', 'австрия', 'vienna', 'вена'],
    '🇨🇦': ['canada', 'канада'],
    '🇯🇵': ['japan', 'япония'],
    '🇸🇬': ['singapore', 'сингапур'],
    '🇦🇪': ['uae', 'оаэ', 'dubai', 'дубай']
  };

  const FLAGS_SHORT = {
    '🇳🇱': ['nl'], '🇩🇪': ['de'], '🇫🇷': ['fr'], '🇺🇸': ['us'], '🇬🇧': ['uk', 'gb'],
    '🇵🇱': ['pl'], '🇹🇷': ['tr'], '🇰🇿': ['kz'], '🇷🇺': ['ru'], '🇫🇮': ['fi'],
    '🇸🇪': ['se'], '🇦🇹': ['at'], '🇨🇦': ['ca'], '🇯🇵': ['jp'], '🇸🇬': ['sg'], '🇦🇪': ['ae']
  };

  function getFlag(name) {
    // Автоматическое подставление флагов отключено по просьбе
    return '🌍';
  }

  function haptic(style) {
    try {
      if (tg && tg.HapticFeedback && tg.isVersionAtLeast && tg.isVersionAtLeast('6.1')) {
        tg.HapticFeedback.impactOccurred(style);
      } else if (window.haptic && typeof window.haptic === 'function') {
        window.haptic(style);
      } else if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.iOSBridge) {
        window.webkit.messageHandlers.iOSBridge.postMessage({ action: 'haptic', style: style });
      } else if (window.AndroidBridge && window.AndroidBridge.vibrate) {
        window.AndroidBridge.vibrate(style);
      }
    } catch (_) { }
  }

  /* ═══════ Theme ═══════ */
  function forceHeaderColor() {
    if (!tg) return;
    const scheme = tg.colorScheme || 'dark';
    const bgColor = scheme === 'dark' ? '#18222d' : '#ffffff';
    const secBgColor = scheme === 'dark' ? '#21303f' : '#f7f9fb';
    if (tg.isVersionAtLeast && tg.isVersionAtLeast('6.1')) {
      try { tg.setHeaderColor(bgColor); } catch (_) { }
      try { tg.setBackgroundColor(bgColor); } catch (_) { }
    }
    if (tg.isVersionAtLeast && tg.isVersionAtLeast('7.10')) {
      try { tg.setBottomBarColor(secBgColor); } catch (_) { }
    }
  }

  function applyTheme() {
    const scheme = URL_THEME || (tg && tg.colorScheme) ||
      (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', scheme);
    document.body.setAttribute('data-theme', scheme);

    const bgColor = scheme === 'dark' ? '#18222d' : '#ffffff';
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', bgColor);

    /* Apply immediately */
    forceHeaderColor();
    /* Re-apply after Telegram's internal handler finishes (race condition fix) */
    setTimeout(forceHeaderColor, 0);
    setTimeout(forceHeaderColor, 50);
    setTimeout(forceHeaderColor, 200);
  }

  /* ═══════ Sprite loader ═══════ */
  async function loadSprite() {
    try {
      const res = await fetch('./need/assets/sprite.svg');
      if (!res.ok) return;
      const text = await res.text();
      const host = document.getElementById('spriteHost');
      if (host) host.innerHTML = text;
    } catch (_) {
      // Sprite failed to load — icons will be empty but app works
    }
  }

  /* ═══════ Navigation ═══════ */
  function showScreen(id) {
    document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));

    const screen = document.getElementById(id);
    if (screen) screen.classList.add('active');

    // Tab highlight
    const tab = document.querySelector(`.tab[data-screen="${id}"]`);
    if (tab) tab.classList.add('active');

    haptic('light');
  }
  window.showScreen = showScreen;

  /* ═══════ Modals ═══════ */
  window.showModal = function (id) {
    const modal = document.getElementById(id);
    if (modal) {
      modal.classList.add('active');
      haptic('light');
    }
  };

  window.hideModal = function (id) {
    if (id === 'modalLinkTg' && window._linkTgPoll) {
      clearInterval(window._linkTgPoll);
      window._linkTgPoll = null;
    }
    const modal = document.getElementById(id);
    if (modal) {
      modal.classList.remove('active');
      haptic('light');
    }
  };

  /* ═══════ Toast ═══════ */
  let toastTimer;
  function showToast(msg, ms) {
    const el = document.getElementById('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), ms || 2000);
  }

  /* ═══════ Clipboard ═══════ */
  function copyText(text, btnEl) {
    if (!text) { showToast('Нечего копировать'); return; }
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    haptic('light');
    showToast('Скопировано ✓');
    if (btnEl) {
      const orig = btnEl.innerHTML;
      const w = btnEl.offsetWidth;
      const h = btnEl.offsetHeight;
      btnEl.style.width = w + 'px';
      btnEl.style.height = h + 'px';
      btnEl.style.display = 'flex';
      btnEl.style.alignItems = 'center';
      btnEl.style.justifyContent = 'center';
      btnEl.innerHTML = '✓';
      setTimeout(() => {
        btnEl.innerHTML = orig;
        btnEl.style.width = '';
        btnEl.style.height = '';
      }, 1500);
    }
  }

  /* ═══════ API ═══════ */
  async function api(url, opts) {
    try {
      const r = await fetch(url, opts);
      const d = await r.json();
      if (!r.ok) {
        showToast('API Error: ' + (d.error || r.status), 5000);
        return null;
      }
      return d;
    } catch (e) {
      showToast('Fetch Error: ' + e.message, 5000);
      return null;
    }
  }

  /* ═══════ Render: Tariffs ═══════ */
  function renderTariffs() {
    const w = document.getElementById('tariffsWrap');
    if (!w) return;
    w.innerHTML = '';

    const isStars = S.selectedPM && S.selectedPM.id === 'stars';
    const starIcon = `<svg viewBox="0 0 24 24" style="width:1em;height:1em;vertical-align:-0.15em;fill:var(--accent_text_color, #3aa8fc)"><path d="M12 2.3l2.4 7.4 7.6.6-5.8 4.7 1.8 7.3-6-4.3-6 4.3 1.8-7.3-5.8-4.7 7.6-.6z" stroke="var(--accent_text_color, #3aa8fc)" stroke-width="2" stroke-linejoin="round"/></svg>`;
    const currency = isStars ? starIcon : '₽';

    const allHaveDiscount = S.tariffs.length > 0 && S.tariffs.every(t => t.oldPrice || t.isRenew);
    let mostExpensiveId = null;
    let maxPct = 0;
    if (S.tariffs.length > 0) {
      mostExpensiveId = S.tariffs.reduce((prev, curr) => {
        const p1 = isStars && curr.priceStars ? curr.priceStars : curr.price;
        const p2 = isStars && prev.priceStars ? prev.priceStars : prev.price;
        return p1 > p2 ? curr : prev;
      }).id;
      S.tariffs.forEach(t => {
        let price = isStars && t.priceStars ? t.priceStars : t.price;
        let oldPrice = isStars && t.oldPriceStars ? t.oldPriceStars : t.oldPrice;
        if (oldPrice && oldPrice > price) {
          const pct = Math.round((1 - price / oldPrice) * 100);
          if (pct > maxPct) maxPct = pct;
        }
      });
    }

    const titleEl = document.querySelector('#planModalHeader');
    if (titleEl) {
      if (allHaveDiscount && maxPct > 0) {
        titleEl.innerHTML = `<p class="title-s" style="margin:0; flex-shrink:0;">Выберите тариф</p><span style="display:inline-flex; align-items:center; justify-content:center; font-size:9.5px; font-weight:700; color:#fff; background:var(--accent_text_color, #3aa8fc); padding:1px 8px; border-radius:12px; text-transform:uppercase; white-space:nowrap;">Скидки до -${maxPct}%</span>`;
      } else {
        titleEl.innerHTML = `<p class="title-s" style="margin:0;">Выберите тариф</p>`;
      }
    }

    S.tariffs.forEach((t) => {
      const sel = S.selectedTariff && S.selectedTariff.id === t.id;
      const el = document.createElement('div');
      el.className = 'tariff-card' + (sel ? ' selected' : '');

      let price = isStars && t.priceStars ? t.priceStars : t.price;
      let oldPrice = isStars && t.oldPriceStars ? t.oldPriceStars : t.oldPrice;
      let pricePerMonth = isStars && t.pricePerMonthStars ? t.pricePerMonthStars : t.pricePerMonth;

      let badgeHtml = '';
      const hasDiscount = t.oldPrice || t.isRenew;
      if (hasDiscount) {
        let pct = 0;
        if (oldPrice && oldPrice > price) {
          pct = Math.round((1 - price / oldPrice) * 100);
        }
        badgeHtml = `<div class="card-ribbon">${pct > 0 ? 'SALE -' + pct + '%' : 'SALE'}</div>`;
      } else if (t.id === mostExpensiveId) {
        badgeHtml = '<div class="card-ribbon" style="padding:4px 34px;">Popular</div>';
      } else if (t.popular) {
        badgeHtml = '<div class="card-ribbon">Хит</div>';
      }

      el.innerHTML = badgeHtml +
        `<p class="months">${t.months} ${mw(t.months)}</p>` +
        `<p class="price" style="margin-top:6px;">${fmtPrice(price)} ${currency} ` +
        (oldPrice ? `<span class="old-price">${fmtPrice(oldPrice)} ${currency}</span>` : '') +
        `</p>` +
        `<p class="per-month">${fmtPrice(pricePerMonth)} ${currency}/мес</p>`;

      el.addEventListener('click', () => {
        S.selectedTariff = t;
        renderTariffs();
        updateTotal();
        haptic('light');
      });
      w.appendChild(el);
    });
  }

  /* ═══════ Render: Payment Methods ═══════ */
  function renderPM() {
    const w = document.getElementById('pmWrap');
    if (!w) return;
    w.innerHTML = '';
    S.paymentMethods.forEach((m) => {
      const sel = S.selectedPM && S.selectedPM.id === m.id;
      const el = document.createElement('div');
      el.className = 'pm-item' + (sel ? ' selected' : '');

      let svgIcon = m.icon || '💳';
      const n = m.name ? m.name.toLowerCase() : '';
      if (m.id === 'stars' || n.includes('star')) {
        svgIcon = `<svg viewBox="0 0 24 24" class="icon-star"><path d="M12 2.3l2.4 7.4 7.6.6-5.8 4.7 1.8 7.3-6-4.3-6 4.3 1.8-7.3-5.8-4.7 7.6-.6z" stroke="currentColor" stroke-width="2" stroke-linejoin="round" class="star-shape"/><circle class="sparkle sp-1" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-2" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-3" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-4" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-5" cx="12" cy="12" r="1.5"/></svg>`;
      } else if (m.id === 'cryptopay' || n.includes('crypto')) {
        svgIcon = `<svg viewBox="0 0 77 42" class="icon-crypto" style="width:32px;height:18px;display:block;">
          <path d="M2.72194715,0 L26.6266393,0 C28.4220313,0 30.0735687,0.988569903 30.9307924,2.57636085 L52.2150034,42 L23.7342299,42 C21.9388379,42 20.2873006,41.0114301 19.4300769,39.4236391 L0.330751009,4.04694928 C-0.386869546,2.71773798 0.101959028,1.05466888 1.42258019,0.332380469 C1.82138733,0.114260557 2.26806337,0 2.72194715,0 Z" fill="#25A3F2"/>
          <path d="M73.643684,0 C74.0975678,0 74.5442438,0.114260557 74.943051,0.332380469 C76.2236533,1.03278135 76.7221109,2.61780981 76.0968053,3.92522764 L76.0348801,4.04694928 L56.9355543,39.4236391 C56.1059829,40.960211 54.5325175,41.9355978 52.8046779,41.996927 L52.6314012,42 L24.5945392,42 L23.7342299,42 L45.4348388,2.57636085 C46.2644101,1.03978897 47.8378756,0.0644022425 49.5657151,0.00307299695 L49.7389918,0 L73.643684,0 Z" fill="#25A3F2" fill-opacity="0.85"/>
        </svg>`;
      } else if (m.id === 'yookassa' || n.includes('юkassa') || n.includes('юкасса') || n.includes('юк') || n.includes('yoo') || n.includes('yuk') || n.includes('карт') || n.includes('card')) {
        svgIcon = `<svg viewBox="0 0 24 24" class="icon-card"><rect x="2" y="5" width="20" height="14" rx="2" ry="2" fill="none" class="card-outline"></rect><line x1="2" y1="10" x2="22" y2="10" class="card-line"></line></svg>`;
      }

      el.innerHTML =
        `<span class="pm-icon flex-center">${svgIcon}</span>` +
        `<span class="pm-name">${m.name}</span>`;
      el.addEventListener('click', () => {
        S.selectedPM = m;
        renderPM();
        renderTariffs();
        updateTotal();
        haptic('light');
      });
      w.appendChild(el);
    });
  }

  /* ═══════ Render: Servers ═══════ */
  function maskIp(ip) {
    if (!ip) return '•••';
    const parts = ip.split('.');
    if (parts.length === 4) return parts[0] + '.' + parts[1] + '.*.*';
    return ip;
  }

  function pingClass(ms) {
    if (ms < 100) return 'good';
    if (ms < 250) return 'medium';
    return 'bad';
  }

  function renderPingBadge(container, ms) {
    if (ms === null || ms === undefined) {
      container.innerHTML =
        '<span class="server-card__ping server-card__ping--loading">' +
        '<span class="server-card__ping-dot"></span>' +
        '<span class="server-card__ping-text">Проверка…</span></span>';
      return;
    }
    if (ms === -1) {
      container.innerHTML =
        '<span class="server-card__ping">' +
        '<span class="server-card__ping-dot server-card__ping-dot--bad"></span>' +
        '<span class="server-card__ping-text server-card__ping-text--bad">Недоступен</span></span>';
      return;
    }
    const cls = pingClass(ms);
    container.innerHTML =
      '<span class="server-card__ping">' +
      '<span class="server-card__ping-dot server-card__ping-dot--' + cls + '"></span>' +
      '<span class="server-card__ping-text server-card__ping-text--' + cls + '">' + ms + ' мс</span></span>';
  }

  async function measurePing(serverId) {
    try {
      const t0 = performance.now();
      const r = await fetch('https://xdoublegroup.online/api/ping?id=' + serverId, { cache: 'no-store' });
      const t1 = performance.now();
      if (!r.ok) return -1;
      const d = await r.json();
      if (d && typeof d.ping === 'number') return d.ping;
      return Math.round(t1 - t0);
    } catch (_) {
      return -1;
    }
  }

  let serverPage = 0;

  function createServerCard(s) {
    const el = document.createElement('div');
    el.className = 'server-card';
    el.setAttribute('data-server-id', s.id);
    el.innerHTML =
      '<div class="server-card__header">' +
      '<span class="server-card__flag">' + (s.emoji || getFlag(s.name)) + '</span>' +
      '<span class="server-card__name">' + s.name + '</span>' +
      '</div>' +
      '<span class="server-card__ip">' + maskIp(s.ip) + '</span>' +
      '<div class="server-card__ping-wrap"></div>';

    const pingWrap = el.querySelector('.server-card__ping-wrap');
    renderPingBadge(pingWrap, null);
    measurePing(s.id).then(function (ms) { renderPingBadge(pingWrap, ms); });

    el.addEventListener('click', function () {
      haptic('light');
      renderPingBadge(pingWrap, null);
      measurePing(s.id).then(function (ms) { renderPingBadge(pingWrap, ms); });
    });

    return el;
  }

  function renderServers() {
    const w = document.getElementById('serversWrap');
    if (!w) return;
    
    // Render desktop map if applicable
    const mapWrap = document.getElementById('serverMapWrap');
    if (mapWrap && window.innerWidth >= 1024) {
      initAndRenderMap();
    }

    if (!S.servers.length) {
      w.innerHTML = '<div class="server-card server-card--loading text-muted body">Нет серверов</div>';
      // Remove old nav if exists
      var oldNav = document.getElementById('serverNav');
      if (oldNav) oldNav.remove();
      return;
    }

    const pageSize = (S.user && S.user.trialAvailable && !(S.subscription && S.subscription.isActive)) ? 2 : 4;
    var totalPages = Math.ceil(S.servers.length / pageSize);
    if (serverPage >= totalPages) serverPage = totalPages - 1;
    if (serverPage < 0) serverPage = 0;

    var start = serverPage * pageSize;
    var pageServers = S.servers.slice(start, start + pageSize);

    w.innerHTML = '';
    pageServers.forEach(function (s) {
      w.appendChild(createServerCard(s));
    });

    // Navigation bar
    var oldNav = document.getElementById('serverNav');
    if (oldNav) oldNav.remove();

    if (totalPages > 1) {
      var nav = document.createElement('div');
      nav.id = 'serverNav';
      nav.className = 'server-nav';

      var btnPrev = document.createElement('button');
      btnPrev.className = 'server-nav__btn';
      btnPrev.textContent = '‹';
      btnPrev.disabled = serverPage === 0;
      btnPrev.addEventListener('click', function () {
        if (serverPage > 0) {
          serverPage--;
          haptic('light');
          renderServers();
        }
      });

      var indicator = document.createElement('span');
      indicator.className = 'server-nav__indicator';
      indicator.textContent = (serverPage + 1) + ' / ' + totalPages;

      var btnNext = document.createElement('button');
      btnNext.className = 'server-nav__btn';
      btnNext.textContent = '›';
      btnNext.disabled = serverPage >= totalPages - 1;
      btnNext.addEventListener('click', function () {
        if (serverPage < totalPages - 1) {
          serverPage++;
          haptic('light');
          renderServers();
        }
      });

      nav.appendChild(btnPrev);
      nav.appendChild(indicator);
      nav.appendChild(btnNext);

      var placeholder = document.getElementById('serverNavPlaceholder');
      if (placeholder) {
        placeholder.innerHTML = '';
        placeholder.appendChild(nav);
      } else {
        w.parentNode.insertBefore(nav, w.nextSibling);
      }
    } else {
      var placeholder = document.getElementById('serverNavPlaceholder');
      if (placeholder) placeholder.innerHTML = '';
    }
  }

  // Auto-refresh pings every 60 seconds (only visible cards)
  let pingInterval;
  function startPingRefresh() {
    if (pingInterval) clearInterval(pingInterval);
    pingInterval = setInterval(function () {
      if (!S.servers.length) return;
      
      // Refresh map pings if map is visible
      const mapWrap = document.getElementById('serverMapWrap');
      if (mapWrap && window.innerWidth >= 1024) {
        document.querySelectorAll('.map-pin').forEach(pin => {
           const idsRaw = pin.getAttribute('data-server-ids');
           if (idsRaw) {
             const ids = idsRaw.split(',').filter(Boolean);
             const pingEl = pin.querySelector('.map-pin-ping');
             if (pingEl && ids.length) {
               measureGroupPing(ids).then(ms => {
                  let dotClass = 'ping-dead';
                  let text = 'N/A';
                  if (ms > 0) {
                    text = ms + 'ms';
                    if (ms < 80) dotClass = 'ping-fast';
                    else if (ms < 150) dotClass = 'ping-med';
                    else dotClass = 'ping-slow';
                  }
                  pingEl.innerHTML = `<span class="ping-dot ${dotClass}"></span>${text}`;
               });
             }
           }
        });
      }

      const pageSize = (S.user && S.user.trialAvailable && !(S.subscription && S.subscription.isActive)) ? 2 : 4;
      var start = serverPage * pageSize;
      var pageServers = S.servers.slice(start, start + pageSize);
      document.querySelectorAll('.server-card[data-server-id]').forEach(function (card, i) {
        if (i >= pageServers.length) return;
        var pingWrap = card.querySelector('.server-card__ping-wrap');
        if (pingWrap) {
          measurePing(pageServers[i].id).then(function (ms) { renderPingBadge(pingWrap, ms); });
        }
      });
    }, 60000);
  }

  /* ═══════ MAP LOGIC ═══════ */
  let mapInitialized = false;
  const MAP_VB_ORIG = { x: 30.767, y: 241.591, w: 784.077, h: 458.627 };
  
  function initAndRenderMap() {
    const container = document.getElementById('mapContainer');
    if (!container) return;
    
    if (!mapInitialized) {
      fetch('need/world-map.svg')
        .then(res => res.text())
        .then(svg => {
          container.innerHTML = svg;
          /* viewBox из файла — полная карта; не подменять узким фокусом */
          mapInitialized = true;
          placePinsOnMap();
        });
    } else {
      placePinsOnMap();
    }
  }

  function hashStr(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h += (h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24);
    }
    return Math.abs(h >>> 0);
  }

  function jitterFromId(id, salt) {
    const h = hashStr(String(id) + ':' + salt);
    return (h % 10000) / 10000; // 0..1 deterministic
  }

  function isFreeServer(s) {
    const em = String((s && s.emoji) || '');
    const nm = String((s && s.name) || '').toLowerCase();
    return em.includes('🆓') || nm.includes('🆓') || nm.includes('[free]') || nm.includes(' free ');
  }

  function extractCountryCodeFromText(text) {
    if (!text) return null;
    // Find first pair of regional indicator symbols anywhere in string.
    const cps = Array.from(String(text));
    for (let i = 0; i < cps.length - 1; i++) {
      const a = cps[i].codePointAt(0);
      const b = cps[i + 1].codePointAt(0);
      if (a >= 0x1F1E6 && a <= 0x1F1FF && b >= 0x1F1E6 && b <= 0x1F1FF) {
        return String.fromCharCode(a - 0x1F1E6 + 97) + String.fromCharCode(b - 0x1F1E6 + 97);
      }
    }
    return null;
  }

  function resolvePinCollisions(pins) {
    const MIN_DIST = 4.4; // percent of map width
    const ITER = 70;
    for (let k = 0; k < ITER; k++) {
      let moved = false;
      for (let i = 0; i < pins.length; i++) {
        for (let j = i + 1; j < pins.length; j++) {
          const a = pins[i];
          const b = pins[j];
          let dx = b.px - a.px;
          let dy = b.py - a.py;
          const d = Math.sqrt(dx * dx + dy * dy) || 0.0001;
          if (d < MIN_DIST) {
            moved = true;
            const push = (MIN_DIST - d) * 0.52;
            dx /= d;
            dy /= d;
            a.px -= dx * push;
            a.py -= dy * push;
            b.px += dx * push;
            b.py += dy * push;
          }
        }
      }
      // Keep labels within map bounds.
      for (const p of pins) {
        p.px = Math.max(5, Math.min(95, p.px));
        p.py = Math.max(10, Math.min(94, p.py));
      }
      if (!moved) break;
    }
  }

  function measureGroupPing(ids) {
    const tasks = ids.map((id) => measurePing(id).catch(() => -1));
    return Promise.all(tasks).then((values) => {
      const ok = values.filter((v) => typeof v === 'number' && v > 0);
      if (!ok.length) return -1;
      return Math.round(ok.reduce((a, b) => a + b, 0) / ok.length);
    });
  }

  function placePinsOnMap() {
    const container = document.getElementById('mapContainer');
    container.querySelectorAll('.map-pin').forEach(p => p.remove());
    
    const svg = container.querySelector('svg');
    if (!svg) return;

    // To safely get bounding boxes even if the screen is currently hidden
    const svgClone = svg.cloneNode(true);
    svgClone.style.position = 'absolute';
    svgClone.style.visibility = 'hidden';
    svgClone.style.display = 'block';
    document.body.appendChild(svgClone);
    
    const bboxes = {};
    
    const pinModels = [];
    S.servers.forEach(s => {
      let code = 'ru'; 
      const emoji = s.emoji || getFlag(s.name);
      const freeServer = isFreeServer(s);
      
      if (freeServer) {
         code = 'ru';
      } else {
         const fromEmoji = extractCountryCodeFromText(s.emoji || '');
         const fromName = extractCountryCodeFromText(s.name || '');
         code = fromEmoji || fromName || code;
      }
      
      if (!bboxes[code]) {
         let target = svgClone.querySelector(`#${code}`);
         if (!target) target = svgClone.querySelector('#ru'); 
         if (target) {
           bboxes[code] = target.getBBox();
         }
      }
      
      const bbox = bboxes[code] || {x: 520, y: 280, width: 200, height: 100}; // Fallback for RU roughly
      let cx = bbox.x + bbox.width / 2;
      let cy = bbox.y + bbox.height / 2;
      
      if (freeServer) {
         // Free servers are distributed deterministically across Russia.
         const rx = jitterFromId(s.id, 'x');
         const ry = jitterFromId(s.id, 'y');
         cx = bbox.x + (0.2 + rx * 0.55) * bbox.width;
         cy = bbox.y + (0.2 + ry * 0.55) * bbox.height;
      } else if (code === 'ru') {
         cx = bbox.x + 0.15 * bbox.width;
         cy = bbox.y + 0.5 * bbox.height;
      } else if (code === 'us') {
         cy = bbox.y + 0.6 * bbox.height;
      } else if (code === 'fr') {
         cx = bbox.x + 0.5 * bbox.width;
         cy = bbox.y + 0.2 * bbox.height;
      }
      
      let px = ((cx - MAP_VB_ORIG.x) / MAP_VB_ORIG.w) * 100;
      let py = ((cy - MAP_VB_ORIG.y) / MAP_VB_ORIG.h) * 100;

      // Dense Europe area: add tiny deterministic spread before global collision solve.
      if (px > 44 && px < 66 && py > 22 && py < 47) {
        px += (jitterFromId(s.id, 'eu-x') - 0.5) * 2.9;
        py += (jitterFromId(s.id, 'eu-y') - 0.5) * 2.2;
      }

      pinModels.push({ s, emoji, px, py, freeServer, code });
    });

    // Merge servers by same location so duplicates become one pin with counter.
    const locationMap = {};
    pinModels.forEach((p) => {
      // 🆓 серверы не схлопываем — у каждого свой узел и своя точка в РФ.
      const key = p.freeServer ? 'free-' + p.s.id : (p.code || 'ru');
      if (!locationMap[key]) {
        locationMap[key] = {
          key,
          code: p.code || 'ru',
          freeServer: p.freeServer,
          emoji: p.freeServer ? '🆓' : p.emoji,
          pxSum: 0,
          pySum: 0,
          count: 0,
          servers: []
        };
      }
      const g = locationMap[key];
      g.pxSum += p.px;
      g.pySum += p.py;
      g.count += 1;
      g.servers.push(p.s);
      if (!g.freeServer && g.emoji === '🌍' && p.emoji && p.emoji !== '🌍') g.emoji = p.emoji;
    });

    const groupedPins = Object.values(locationMap).map((g) => ({
      ...g,
      px: g.pxSum / g.count,
      py: g.pySum / g.count
    }));

    // Slight deterministic spread for dense Europe area.
    groupedPins.forEach((p) => {
      if (p.px > 40 && p.px < 69 && p.py > 16 && p.py < 52) {
        p.px += (jitterFromId(p.key, 'grp-eu-x') - 0.5) * 1.8;
        p.py += (jitterFromId(p.key, 'grp-eu-y') - 0.5) * 1.4;
      }
    });

    resolvePinCollisions(groupedPins);

    groupedPins.forEach(({ key, emoji, px, py, freeServer, servers, count }) => {
      const pin = document.createElement('div');
      pin.className = 'map-pin';
      if (freeServer) pin.classList.add('map-pin--free');
      pin.setAttribute('data-pin-key', key);
      pin.setAttribute('data-server-ids', servers.map((x) => x.id).join(','));
      pin.style.left = px + '%';
      pin.style.top = py + '%';
      
      pin.innerHTML = `
        <div class="map-pin-inner">
          <div class="map-pin-flag">${freeServer ? '🆓' : emoji}</div>
          ${count > 1 ? `<div class="map-pin-count">×${count}</div>` : ''}
          <div class="map-pin-ping"><span class="ping-dot ping-dead"></span>...</div>
        </div>
      `;
      
      const pingEl = pin.querySelector('.map-pin-ping');
      const ids = servers.map((x) => x.id);
      measureGroupPing(ids).then(ms => {
         let dotClass = 'ping-dead';
         let text = 'N/A';
         if (ms > 0) {
           text = ms + 'ms';
           if (ms < 80) dotClass = 'ping-fast';
           else if (ms < 150) dotClass = 'ping-med';
           else dotClass = 'ping-slow';
         }
         pingEl.innerHTML = `<span class="ping-dot ${dotClass}"></span>${text}`;
      });
      
      pin.addEventListener('click', () => {
         haptic('light');
         pingEl.innerHTML = `<span class="ping-dot ping-dead"></span>...`;
         measureGroupPing(ids).then(ms => {
           let dotClass = 'ping-dead';
           let text = 'N/A';
           if (ms > 0) {
             text = ms + 'ms';
             if (ms < 80) dotClass = 'ping-fast';
             else if (ms < 150) dotClass = 'ping-med';
             else dotClass = 'ping-slow';
           }
           pingEl.innerHTML = `<span class="ping-dot ${dotClass}"></span>${text}`;
         });
      });
      
      container.appendChild(pin);
    });
    
    document.body.removeChild(svgClone);
  }

  /* ═══════ Render: Total ═══════ */
  function updateTotal() {
    const el = document.getElementById('totalPrice');
    const btn = document.getElementById('btnPay');
    if (S.selectedTariff) {
      const isStars = S.selectedPM && S.selectedPM.id === 'stars';
      const starIcon = `<svg viewBox="0 0 24 24" style="width:1em;height:1em;vertical-align:-0.15em;fill:var(--accent_text_color, #3aa8fc)"><path d="M12 2.3l2.4 7.4 7.6.6-5.8 4.7 1.8 7.3-6-4.3-6 4.3 1.8-7.3-5.8-4.7 7.6-.6z" stroke="var(--accent_text_color, #3aa8fc)" stroke-width="2" stroke-linejoin="round"/></svg>`;
      const currency = isStars ? starIcon : '₽';
      const price = isStars && S.selectedTariff.priceStars ? S.selectedTariff.priceStars : S.selectedTariff.price;

      el.innerHTML = fmtPrice(price) + ' ' + currency;
      btn.disabled = !S.selectedPM;
    } else {
      el.textContent = '—';
      btn.disabled = true;
    }
  }

  function escapeHtml(s) {
    if (s == null || s === '') return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function renderProfileAuthLinks() {
    const box = document.getElementById('profileAuthLinkBox');
    if (!box) return;
    if (!S.user) {
      box.style.display = 'none';
      return;
    }
    const needE = !!S.user.needLinkEmail;
    const needTg = !!S.user.needLinkTelegram;
    const masked = S.user.linkedEmailMasked;
    if (!needE && !needTg) {
      if (masked) {
        box.style.display = 'block';
        box.innerHTML =
          '<div class="card profile-auth-card"><p class="body text-muted profile-auth-card__text">Привязанная почта: <strong>' +
          escapeHtml(masked) +
          '</strong></p></div>';
      } else {
        box.style.display = 'none';
      }
      return;
    }
    box.style.display = 'block';
    let inner = '<div class="card profile-auth-card">';
    if (needE && tg && tg.initData) {
      inner +=
        '<p class="subtitle" style="margin:0 0 6px;">Привяжите почту</p>' +
        '<p class="body text-muted" style="margin:0 0 12px;font-size:13px;line-height:1.4;">Один и тот же код придёт в Telegram и на email — можно входить с сайта и не потерять доступ.</p>' +
        '<button type="button" class="btn-secondary" style="width:100%" onclick="window.openLinkEmailModal()">Привязать почту</button>';
    } else if (needE) {
      inner +=
        '<p class="body text-muted" style="margin:0;font-size:13px;line-height:1.4;">Откройте это приложение из Telegram (мини-приложение), чтобы привязать почту.</p>';
    }
    if (needTg && S.authViaWebJwt) {
      inner +=
        '<p class="subtitle" style="margin:12px 0 6px;">Привяжите Telegram</p>' +
        '<p class="body text-muted" style="margin:0 0 12px;font-size:13px;line-height:1.4;">Откройте бота по ссылке и нажмите Start — подписка перенесётся на ваш Telegram.</p>' +
        '<button type="button" class="btn-primary" style="width:100%" onclick="window.startLinkTelegramFlow()">Открыть бота</button>';
    }
    inner += '</div>';
    box.innerHTML = inner;
  }

  window.startLinkTelegramFlow = async function () {
    const jwt = localStorage.getItem('svoyvpn_web_jwt');
    if (!jwt) {
      showToast('Сначала войдите по почте');
      return;
    }
    const base = window.AppConfig.apiBaseURL || '';
    const r = await fetch(base + '/api/auth/link-telegram/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + jwt },
      body: '{}',
    });
    let d = {};
    try {
      d = await r.json();
    } catch (_) {}
    if (!r.ok) {
      showToast(d.error || 'Не удалось начать привязку');
      return;
    }
    if (!d.botUrl || !d.nonce) return;
    showModal('modalLinkTg');
    const a = document.getElementById('linkTgOpenBtn');
    if (a) a.href = d.botUrl;
    if (window._linkTgPoll) clearInterval(window._linkTgPoll);
    const nonce = d.nonce;
    window._linkTgPoll = setInterval(async () => {
      try {
        const pr = await fetch(
          base + '/api/auth/link-telegram/poll?nonce=' + encodeURIComponent(nonce),
          { headers: { Authorization: 'Bearer ' + jwt } }
        );
        const pj = await pr.json();
        if (pj.status === 'ok' && pj.token) {
          clearInterval(window._linkTgPoll);
          window._linkTgPoll = null;
          localStorage.setItem('svoyvpn_web_jwt', pj.token);
          hideModal('modalLinkTg');
          showToast('Готово! Вход теперь через Telegram');
          await loadUser(true);
        } else if (pj.status === 'expired') {
          clearInterval(window._linkTgPoll);
          window._linkTgPoll = null;
          hideModal('modalLinkTg');
          showToast('Время ожидания истекло');
        }
      } catch (_) {}
    }, 2000);
  };

  /* ═══════ Render: User & Subscription ═══════ */
  function renderUser() {
    const avatar = document.getElementById('avatar');
    if (S.user) {
      const name = [S.user.firstName, S.user.lastName].filter(Boolean).join(' ') || 'U';
      const pName = document.getElementById('profileName');
      if (pName) pName.textContent = name;

      // Avatar: try photo from API, then from tg.initDataUnsafe, then letter fallback
      const photoUrl = S.user.photoUrl || S.user.photo_url ||
        (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.photo_url);
      if (photoUrl && avatar) {
        avatar.innerHTML = '';
        avatar.style.overflow = 'hidden';
        const img = document.createElement('img');
        img.src = photoUrl;
        img.alt = name;
        img.style.cssText = 'width:100%;height:100%;object-fit:cover;border-radius:50%;';
        img.onerror = function () {
          avatar.innerHTML = name.charAt(0).toUpperCase();
          avatar.style.overflow = '';
        };
        avatar.appendChild(img);
      } else if (avatar) {
        avatar.textContent = name.charAt(0).toUpperCase();
      }
    }

    // Subscription
    const sub = S.subscription;
    const vpnStatus = document.getElementById('vpnStatus');
    const pStatus = document.getElementById('profileStatus');
    const pBadge = document.getElementById('profileBadge');
    const subBlockBox = document.getElementById('subBlockBox');

    let daysLeft = 0;
    if (sub && sub.isActive && sub.endDate) {
      const end = new Date(sub.endDate);
      const now = new Date();
      const diff = end.getTime() - now.getTime();
      daysLeft = Math.ceil(diff / (1000 * 3600 * 24));
    }

    if (pBadge) {
      const pBadgeText = document.getElementById('profileBadgeText');
      if (sub && sub.isActive) {
        pBadge.style.display = 'inline-flex';
        if (pBadgeText) {
          if (daysLeft > 0) {
            pBadgeText.textContent = `Активна на ${daysLeft} ${dw(daysLeft)}`;
          } else {
            pBadgeText.textContent = `Активна до ${fmtDate(sub.endDate)}`;
          }
        }
        pBadge.onclick = (e) => {
          e.stopPropagation();
          haptic('light');
          pBadge.classList.toggle('expanded');
        };
        // Reset if click anywhere else
        document.addEventListener('click', () => pBadge.classList.remove('expanded'), { once: false });
      } else {
        pBadge.style.display = 'none';
      }
    }

    // Support links update
    if (S.user && S.user.supportLink) {
      const obLink = document.getElementById('obSupportLink');
      const obHandle = document.getElementById('obSupportHandle');
      if (obLink) obLink.href = S.user.supportLink;
      if (obHandle) {
        // Extract handle like @SvoyVPN_support from https://t.me/SvoyVPN_support
        let handle = S.user.supportLink.split('t.me/')[1] || S.user.supportLink;
        if (!handle.startsWith('@') && handle.includes('_')) handle = '@' + handle;
        obHandle.textContent = handle;
      }
    }

    if (subBlockBox) {
      if (sub && sub.isActive) {
        if (vpnStatus) vpnStatus.textContent = 'Подписка активна';
        if (pStatus) {
          pStatus.textContent = '';
        }

        const endHuman = fmtDate(sub.endDate);
        const daysMeta =
          daysLeft > 0
            ? `Осталось ${daysLeft} ${dw(daysLeft)} полной свободы.`
            : 'Срок окончания указан ниже — подключайтесь в любой момент.';
        const warnSoon = daysLeft > 0 && daysLeft <= 7;
        const heroMod = warnSoon ? ' sub-status-hero--warn' : '';

        let statusHtml = `
            <div class="card sub-status-hero sub-status-hero--active${heroMod}" role="status">
              <div class="sub-status-hero__ring" aria-hidden="true">
                <svg class="sub-status-hero__check" viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                  <polyline points="22 4 12 14.01 9 11.01"/>
                </svg>
              </div>
              <p class="sub-status-hero__title">Подписка активна</p>
              <p class="sub-status-hero__date">Действует до ${endHuman}</p>
              <p class="sub-status-hero__meta">${daysMeta}</p>
              ${
                warnSoon
                  ? '<p class="sub-status-hero__urgent">Скоро окончание — продлите подписку, чтобы оставться в сети!</p>'
                  : ''
              }
            </div>
            <div class="gap-12"></div>
        `;

        statusHtml += `
          <button class="btn-primary" onclick="window.showModal('modalPlan')">Продлить</button>
        `;
        subBlockBox.innerHTML = statusHtml;

      } else {
        if (vpnStatus) vpnStatus.textContent = 'Быстрый и приватный VPN';
        if (pStatus) {
          pStatus.textContent = '';
        }

        if (S.user && S.user.trialAvailable) {
          subBlockBox.innerHTML = `
            <div class="card" style="padding:10px 16px; text-align:center; background: linear-gradient(135deg, rgba(58,168,252,0.1) 0%, rgba(58,168,252,0) 100%); border: 1px dashed var(--accent_text_color, #3aa8fc); border-radius: 12px;">
               <div style="margin-bottom: 4px;">
                 <svg class="gift-anim" viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="var(--accent_text_color, #3aa8fc)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                   <polyline points="20 12 20 22 4 22 4 12"></polyline>
                   <rect x="2" y="7" width="20" height="5"></rect>
                   <line x1="12" y1="22" x2="12" y2="7"></line>
                   <path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z"></path>
                   <path d="M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z"></path>
                 </svg>
                 <style>
                   @keyframes giftBounce {
                     0%, 100% { transform: scale(1) rotate(0deg); }
                     2% { transform: scale(1.1) rotate(-10deg); }
                     6% { transform: scale(1.1) rotate(10deg); }
                     10% { transform: scale(1.1) rotate(-10deg); }
                     14% { transform: scale(1.1) rotate(10deg); }
                     18% { transform: scale(1) rotate(0deg); }
                   }
                   .gift-anim {
                     display: inline-block;
                     animation: giftBounce 5s infinite;
                     transform-origin: bottom center;
                   }
                 </style>
               </div>
               <p class="subtitle" style="color:var(--accent_text_color, #3aa8fc); margin-bottom:4px; font-weight: 700;">Попробуй бесплатно!</p>
               <p class="body text-muted" style="margin-bottom:10px; font-size:12px; line-height: 1.3;">Доступно <b>${S.user.trialDays} дней</b> теста без привязки карты.</p>
               <button class="btn-primary" id="btnActivateTrial" style="min-height: 40px; font-size: 14px; padding: 8px;">Забрать ${S.user.trialDays} дней</button>
            </div>
            <div class="gap-12"></div>
            <button class="btn-secondary" style="width:100%; min-height: 48px;" onclick="window.showModal('modalPlan')">Выбрать тариф</button>
          `;

          document.getElementById('btnActivateTrial').addEventListener('click', async function () {
            this.disabled = true;
            this.textContent = 'Активация...';
            haptic('medium');

            if (!tg || !tg.initData) {
              showToast('Ошибка: Нет данных Telegram');
              this.disabled = false;
              return;
            }

            const d = await api('https://xdoublegroup.online/api/trial/activate', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ initData: tg.initData })
            });

            if (d && d.status === 'ok') {
              showSuccessOverlay('Период активирован!', 'Пробный период успешно зачислен. Теперь вы можете пользоваться VPN!');
              await loadUser(); // Reload user state
            } else {
              showToast('Ошибка активации: ' + (d ? d.error : 'Неизвестная ошибка'));
              this.disabled = false;
              this.textContent = `Забрать ${S.user.trialDays} дней`;
            }
          });

        } else {
          subBlockBox.innerHTML = `
            <div class="card sub-status-hero sub-status-hero--inactive" role="status">
              <div class="sub-status-hero__ring sub-status-hero__ring--muted" aria-hidden="true">
                <svg class="sub-status-hero__lock" viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                  <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                </svg>
              </div>
              <p class="sub-status-hero__title">Подписка не оформлена</p>
              <p class="sub-status-hero__meta">Без тарифа VPN-серверы недоступны. Тарифы и оплата — в отдельном окне.</p>
            </div>
            <div class="gap-12"></div>
            <button class="btn-primary" onclick="window.showModal('modalPlan')">Выбрать тариф</button>
          `;
        }
      }
    }

    renderProfileAuthLinks();

    // Onboarding Slide 0 (Activation Check)
    const subCheckBlock = document.getElementById('obSubCheckBlock');
    if (subCheckBlock) {
      if (sub && sub.isActive) {
        subCheckBlock.innerHTML = `
          <div class="card" style="padding:16px; text-align:center; background: rgba(52,199,89,0.1); border: 1px solid rgba(52,199,89,0.3); border-radius: 12px;">
            <div style="font-size:24px; margin-bottom:8px;">✅</div>
            <p class="subtitle" style="color:var(--tg-theme-success-color, #34c759); font-weight:700; margin-bottom:4px;">Подписка активна</p>
            <p class="body text-muted" style="font-size:13px;">Теперь вы можете перейти к настройке устройства.</p>
          </div>
        `;
      } else {
        let checkHtml = `
          <div class="card" style="padding:16px; text-align:center; background: rgba(58,168,252,0.1); border: 1px dashed var(--accent_text_color, #3aa8fc); border-radius: 12px;">
            <p class="body" style="font-size:14px; margin:0 0 12px; line-height:1.4;">Для подключения устройств необходимо сначала приобрести подписку.</p>
        `;
        if (S.user && S.user.trialAvailable) {
          checkHtml += `
              <div style="margin-bottom: 6px;">
                <svg class="gift-anim" viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="var(--accent_text_color, #3aa8fc)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="20 12 20 22 4 22 4 12"></polyline>
                  <rect x="2" y="7" width="20" height="5"></rect>
                  <line x1="12" y1="22" x2="12" y2="7"></line>
                  <path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z"></path>
                  <path d="M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z"></path>
                </svg>
              </div>
              <p class="body text-muted" style="margin-bottom:12px; font-size:13px;">Или попробуйте бесплатно — заберите пробный период в подарок!</p>
              <button class="btn-primary" id="obBtnTrial" style="min-height:40px; font-size:14px; width:100%;">Забрать ${S.user.trialDays} дней</button>
            `;
          setTimeout(() => {
            const b = document.getElementById('obBtnTrial');
            if (b) b.onclick = async function () {
              this.disabled = true; this.textContent = '...';
              const d = await api('https://xdoublegroup.online/api/trial/activate', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initData: tg.initData })
              });
              if (d && d.status === 'ok') {
                showSuccessOverlay('Подарок получен! 🎁', 'Вы получили бесплатные дни доступа. Настройте устройство на следующем шаге!');
                await loadUser();
              } else {
                showToast('Ошибка: ' + (d ? d.error : '?'));
                this.disabled = false; this.textContent = `Забрать ${S.user.trialDays} дней`;
              }
            };
          }, 0);
        } else {
          checkHtml += `
              <button class="btn-primary" style="min-height:40px; font-size:14px; width:100%;" onclick="window.showModal('modalPlan')">Выбрать тариф</button>
            `;
        }
        checkHtml += `</div>`;
        subCheckBlock.innerHTML = checkHtml;
      }

      // Update onboarding visibility based on sub
      if (typeof window.updateOnboardingSubState === 'function') {
        window.updateOnboardingSubState();
      }
    }

    // Subscription URL
    if (sub && sub.subscriptionUrl) {
      const elSetup = document.getElementById('subUrlSetup');
      const elProfile = document.getElementById('subUrlProfile');
      if (elSetup) elSetup.value = sub.subscriptionUrl;
      if (elProfile) elProfile.value = sub.subscriptionUrl;
    }

    // Refresh servers layout (pageSize may change if trial block appeared)
    renderServers();
  }

  /* ═══════ Load Data ═══════ */
  async function loadData() {
    let tariffUrl = 'https://xdoublegroup.online/api/tariffs';
    if (tg && tg.initData) {
      tariffUrl += '?initData=' + encodeURIComponent(tg.initData);
    }

    const [tariffs, pm, servers] = await Promise.all([
      api(tariffUrl),
      api('https://xdoublegroup.online/api/payment-methods'),
      api('https://xdoublegroup.online/api/servers'),
    ]);

    if (Array.isArray(tariffs) && tariffs.length) {
      S.tariffs = tariffs;
      S.selectedTariff = tariffs.reduce((prev, curr) => (curr.price > prev.price ? curr : prev));
      renderTariffs();
      updateTotal();
    }
    if (Array.isArray(pm) && pm.length) {
      // Sort so 'yookassa' is first
      pm.sort((a, b) => {
        if (a.id === 'yookassa') return -1;
        if (b.id === 'yookassa') return 1;
        return 0;
      });
      S.paymentMethods = pm;
      S.selectedPM = pm[0];
      renderPM();
      updateTotal();
    }

    if (Array.isArray(servers)) {
      S.servers = servers;
      renderServers();
      startPingRefresh();
    } else {
      renderServers(); // show "Нет серверов"
    }
  }

  /* ═══════ Payment Polling ═══════ */
  let checkPaymentInterval = null;
  function startPaymentPolling() {
    if (checkPaymentInterval) return;
    console.log('[Payment] Starting polling for success...');
    checkPaymentInterval = setInterval(async () => {
      const pendingTime = localStorage.getItem('pending_payment_time');
      if (!pendingTime) {
        stopPaymentPolling();
        return;
      }
      // Stop polling after 15 mins
      if (Date.now() - parseInt(pendingTime) > 15 * 60 * 1000) {
        console.log('[Payment] Polling timeout');
        stopPaymentPolling();
        localStorage.removeItem('pending_payment_time');
        return;
      }
      await loadUser(true);
    }, 4000);
  }

  function stopPaymentPolling() {
    if (checkPaymentInterval) {
      console.log('[Payment] Stopping polling');
      clearInterval(checkPaymentInterval);
      checkPaymentInterval = null;
    }
  }

  async function loadUser(silent = false) {
    // Android WebView mode: use JWT instead of tg.initData
    if (IS_ANDROID) {
      const wasActive = S.subscription && S.subscription.isActive;
      const oldEnd = S.subscription && S.subscription.endDate;
      const d = await api('/api/user', {
        method: 'GET',
        headers: { 'Authorization': 'Bearer ' + ANDROID_JWT }
      });
      if (d && d.user) {
        S.authViaWebJwt = false;
        S.user = d.user;
        S.subscription = d.subscription;
        S.referral = normalizeReferralFromApi(d.referral);
        renderUser();
        const isActive = S.subscription && S.subscription.isActive;
        const newEnd = S.subscription && S.subscription.endDate;
        const pendingTime = localStorage.getItem('pending_payment_time');
        if (pendingTime && (Date.now() - parseInt(pendingTime)) < 20 * 60 * 1000) {
          if ((!wasActive && isActive) || (oldEnd && newEnd && oldEnd !== newEnd)) {
            localStorage.removeItem('pending_payment_time');
            stopPaymentPolling();
            showSuccessOverlay('Оплата успешна!', 'Ваша подписка активирована.<br>Детальный чек отправлен вам в бот.');
            hideModal('modalPlan');
          }
        }
      }
      return;
    }

    const inTelegramWithData = !!(tg && tg.initData);

    // Обычный браузер / нет initData: только email-JWT (подарки не зависят от Telegram WebApp)
    if (!inTelegramWithData && !IS_MOBILE_APP && localStorage.getItem('svoyvpn_web_jwt')) {
      const d = await api('/api/user', { method: 'GET' });
      if (d && d.user) {
        S.authViaWebJwt = true;
        S.user = d.user;
        S.subscription = d.subscription;
        S.referral = normalizeReferralFromApi(d.referral);
        renderUser();
        if (!silent) showScreen('screenVpn');
      } else {
        S.authViaWebJwt = false;
        localStorage.removeItem('svoyvpn_web_jwt');
        showScreen('screenAuth');
      }
      return;
    }

    // Telegram WebApp: вход только по initData (тот же /api/user, поле referral — без отдельного /api/referral)
    if (inTelegramWithData) {
      const wasActive = S.subscription && S.subscription.isActive;
      const oldEnd = S.subscription && S.subscription.endDate;

      const d = await api('/api/user', {
        method: 'POST',
        skipWebJwt: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: tg.initData }),
      });

      if (d && d.user) {
        S.authViaWebJwt = false;
        S.user = d.user;
        S.subscription = d.subscription;
        S.referral = normalizeReferralFromApi(d.referral);
        try {
          sessionStorage.setItem('svoy_tg_init_data', String(tg.initData));
        } catch (_) { }
        renderUser();
        if (!silent) showScreen('screenVpn');

        const isActive = S.subscription && S.subscription.isActive;
        const newEnd = S.subscription && S.subscription.endDate;
        const pendingTime = localStorage.getItem('pending_payment_time');
        if (pendingTime && (Date.now() - parseInt(pendingTime)) < 20 * 60 * 1000) {
          if ((!wasActive && isActive) || (oldEnd && newEnd && oldEnd !== newEnd)) {
            localStorage.removeItem('pending_payment_time');
            stopPaymentPolling();
            showSuccessOverlay('Оплата успешна!', 'Ваша подписка активирована.<br>Детальный чек отправлен вам в бот.');
            hideModal('modalPlan');
          }
        }
      }
      return;
    }

    if (!IS_MOBILE_APP) showScreen('screenAuth');
  }

  /* ═══════ Payment ═══════ */
  async function handlePay() {
    if (!S.selectedTariff || !S.selectedPM) return;
    if (!IS_ANDROID && (!tg || !tg.initData)) {
      showToast('Оплата доступна только в Telegram');
      return;
    }
    const payBody = IS_ANDROID
      ? { tariffId: S.selectedTariff.id, paymentMethod: S.selectedPM.id, deviceCount: 1 }
      : { initData: tg.initData, tariffId: S.selectedTariff.id, paymentMethod: S.selectedPM.id, deviceCount: 1 };
    const payHeaders = IS_ANDROID
      ? { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + ANDROID_JWT }
      : { 'Content-Type': 'application/json' };
    const d = await api('https://xdoublegroup.online/api/payment/create', {
      method: 'POST',
      headers: payHeaders,
      body: JSON.stringify(payBody),
    });

    if (d && (d.paymentUrl || d.invoiceUrl)) {
      // Mark that we are initiating a payment
      localStorage.setItem('pending_payment_time', Date.now().toString());
      startPaymentPolling();

      const url = d.paymentUrl || d.invoiceUrl;
      console.log('[Payment] Order info:', d);

      const isNativeInvoice = url.includes('t.me/$') || url.includes('t.me/invoice');
      const isCryptoPay = url.includes('CryptoBot') || url.includes('CryptoTestnetBot');

      if (isNativeInvoice) {
        if (tg && tg.openInvoice) {
          tg.openInvoice(url, function (status) {
            if (status === 'paid') {
              localStorage.removeItem('pending_payment_time');
              stopPaymentPolling();
              showSuccessOverlay('Оплата успешна!', 'Ваша подписка активирована.<br>Детальный чек отправлен вам в бот.');
            } else if (status === 'failed') {
              localStorage.removeItem('pending_payment_time');
              stopPaymentPolling();
              showToast('Ошибка при оплате');
            }
          });
        } else {
          tg && tg.openLink ? tg.openLink(url) : window.open(url, '_blank');
        }
      } else if (isCryptoPay) {
        // CryptoBot mini app invoice URL should be opened via openLink
        if (tg && tg.openLink) {
          tg.openLink(url);
        } else {
          window.open(url, '_blank');
        }
      } else {
        // Other external links
        if (tg && tg.openLink) {
          tg.openLink(url, { try_instant_view: false });
        } else {
          window.open(url, '_blank');
        }
      }
    } else {
      showToast('Ошибка создания платежа');
    }
  }

  function showSuccessOverlay(title, sub) {
    const ov = document.getElementById('successOverlay');
    if (ov) {
      if (title) ov.querySelector('.title-s').textContent = title;
      if (sub) ov.querySelector('.body').innerHTML = sub;
      ov.style.display = 'flex';
      fireConfetti();
      haptic('success');
    }
  }

  window.hideSuccessOverlay = function () {
    const ov = document.getElementById('successOverlay');
    if (ov) {
      ov.style.display = 'none';
      loadUser();
    }
  };

  function fireConfetti() {
    const container = document.getElementById('confetti-container');
    if (!container) return;
    container.innerHTML = '';
    const colors = ['#3aa8fc', '#34c759', '#ff9f0a', '#ff3b30', '#ffffff'];
    for (let i = 0; i < 60; i++) {
      const conf = document.createElement('div');
      conf.className = 'confetti';
      conf.style.left = Math.random() * 100 + '%';
      conf.style.top = Math.random() * 20 - 20 + 'px';
      conf.style.backgroundColor = colors[Math.floor(Math.random() * colors.length)];
      conf.style.width = Math.random() * 8 + 4 + 'px';
      conf.style.height = Math.random() * 8 + 4 + 'px';
      conf.style.position = 'absolute';
      conf.style.borderRadius = '2px';
      container.appendChild(conf);

      const duration = 2 + Math.random() * 2;
      conf.animate([
        { transform: `translate3d(0,0,0) rotate(0deg)`, opacity: 1 },
        { transform: `translate3d(${(Math.random() - 0.5) * 250}px, ${window.innerHeight + 50}px, 0) rotate(${Math.random() * 1000}deg)`, opacity: 0 }
      ], {
        duration: duration * 1000,
        easing: 'cubic-bezier(0, .9, .6, 1)',
        fill: 'forwards'
      });
    }
  }

  /* ═══════ Init ═══════ */
  document.addEventListener('DOMContentLoaded', () => {
    // Telegram WebApp — init FIRST so themeParams is ready
    if (tg) {
      tg.ready();
      tg.expand();
      // Subscribe to future theme changes
      tg.onEvent && tg.onEvent('themeChanged', applyTheme);

      // Auto-refresh when user returns to the app (e.g. from payment browser)
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') {
          loadUser();
        }
      });
    }

    // Apply theme after tg.ready() so themeParams is populated
    applyTheme();
    // Re-apply after a tick in case Telegram populates themeParams async
    setTimeout(applyTheme, 150);

    // Load SVG sprite
    loadSprite();

    // If there was a pending payment from previous session, resume polling
    if (localStorage.getItem('pending_payment_time')) {
      startPaymentPolling();
    }

    // Pre-fill user info from tg immediately (before API call)
    if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
      const u = tg.initDataUnsafe.user;
      S.user = {
        firstName: u.first_name || '',
        lastName: u.last_name || '',
        username: u.username || '',
        photoUrl: u.photo_url || '',
      };
      renderUser();
    }

    // Tab bar navigation
    document.querySelectorAll('.tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (btn.dataset.screen) showScreen(btn.dataset.screen);
        if (btn.dataset.screen === 'screenReferral') loadReferral();
      });
    });

    function addClick(id, handler) {
      const el = document.getElementById(id);
      if (el) el.addEventListener('click', handler);
    }

    // btnReferral — вкладка; тариф — onclick в renderUser (showModal)

    // btnReferral is handled via tab bar now

    let refLink = '';

    function setReferralActionButtons(enabled) {
      ['btnCopyRef', 'btnShareRef'].forEach(function (id) {
        const b = document.getElementById(id);
        if (!b) return;
        b.disabled = !enabled;
        b.style.opacity = enabled ? '1' : '0.5';
        b.style.pointerEvents = enabled ? '' : 'none';
      });
    }

    /** Плейсхолдеры экрана «Подарки» (без технических деталей для пользователя). */
    function setReferralScreenPlaceholder(mode) {
      const refDesc = document.getElementById('refDesc');
      const refL = document.getElementById('refLinkText');
      const refC = document.getElementById('refCount');
      const refB = document.getElementById('refBonus');
      refLink = '';
      setReferralActionButtons(false);
      if (mode === 'loading') {
        if (refDesc) refDesc.textContent = 'Загружаем…';
        if (refL) refL.textContent = '…';
        if (refC) refC.textContent = '…';
        if (refB) refB.textContent = '…';
        return;
      }
      if (mode === 'guest') {
        if (refDesc) {
          refDesc.textContent =
            'Войдите в аккаунт (email и пароль на экране входа) — здесь появится ваша ссылка для приглашения друзей и бонусные дни.';
        }
        if (refL) refL.textContent = '—';
        if (refC) refC.textContent = '—';
        if (refB) refB.textContent = '—';
        return;
      }
      if (mode === 'pending') {
        if (refDesc) {
          refDesc.textContent =
            'Не удалось загрузить ссылку. Обновите страницу (потяните вниз в приложении) или зайдите в раздел позже. Если проблема останется — напишите в поддержку.';
        }
        if (refL) refL.textContent = '—';
        if (refC) refC.textContent = '0 чел.';
        if (refB) refB.textContent = '—';
      }
    }

    function applyReferralPayload(d) {
      const n = normalizeReferralFromApi(d);
      if (!n) return false;
      S.referral = n;
      refLink = n.refLink;
      const refL = document.getElementById('refLinkText');
      if (refL) refL.textContent = n.refLink;
      const refC = document.getElementById('refCount');
      if (refC) refC.textContent = n.referralCount + ' чел.';
      const refB = document.getElementById('refBonus');
      if (refB) refB.textContent = n.inviterBonusDays + ' дн. за друга';
      const refDEl = document.getElementById('refDesc');
      if (refDEl) {
        refDEl.textContent =
          `Дарим ${n.inviterBonusDays} дней Вам и ${n.invitedBonusDays} дня другу за каждое успешное приглашение.`;
      }
      setReferralActionButtons(true);
      return true;
    }

    async function loadReferral() {
      setReferralScreenPlaceholder('loading');
      if (!applyReferralPayload(S.referral)) {
        await loadUser(true);
      }
      if (applyReferralPayload(S.referral)) return;
      if (!S.user) {
        setReferralScreenPlaceholder('guest');
        return;
      }
      setReferralScreenPlaceholder('pending');
    }

    addClick('btnCopyRef', function () {
      if (!refLink) {
        showToast('Ссылка ещё не готова — обновите экран или войдите в аккаунт.', 4000);
        return;
      }
      copyText(refLink, this);
    });
    addClick('btnShareRef', function () {
      if (!refLink) return;
      const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(refLink)}&text=${encodeURIComponent('Попробуй этот отличный VPN! Дают бонусные дни при регистрации по ссылке 🎁')}`;
      tg && tg.openTelegramLink ? tg.openTelegramLink(shareUrl) : window.open(shareUrl, '_blank');
    });

    // Copy: онбординг — obBtnNext; профиль — btnCopyProfile (subUrlSetup в разметке нет)

    addClick('btnCopyProfile', function () {
      const el = document.getElementById('subUrlProfile');
      if (el) copyText(el.value, this);
    });

    // Pay
    addClick('btnPay', handlePay);

    // Links
    addClick('btnChannel', () => {
      const channel = 'https://t.me/SvoyVPN_channel';
      tg && tg.openTelegramLink
        ? tg.openTelegramLink(channel)
        : window.open(channel, '_blank');
    });
    addClick('btnSupport', () => {
      const link = (S.user && S.user.supportLink) || 'https://t.me/SvoyVPN_support';
      tg && tg.openTelegramLink
        ? tg.openTelegramLink(link)
        : window.open(link, '_blank');
    });

    /* ── Logout Button (Web & Mobile) ── */
    const lgBtn = document.getElementById('btnLogout');
    if (lgBtn) {
      const isWebJWT = localStorage.getItem('svoyvpn_web_jwt');
      // Show button if in native app OR if web token exists
      if (IS_MOBILE_APP || isWebJWT) {
        lgBtn.style.display = 'flex';
      }

      // Use a single robust listener
      lgBtn.addEventListener('click', (e) => {
        haptic('medium');
        console.log('[Logout] click, IS_MOBILE_APP:', IS_MOBILE_APP);
        
        let bridgeFound = false;
        if (IS_MOBILE_APP) {
          if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.iOSBridge) {
            window.webkit.messageHandlers.iOSBridge.postMessage({ action: 'logout' });
            bridgeFound = true;
          } else if (window.AndroidBridge && window.AndroidBridge.logout) {
            window.AndroidBridge.logout();
            bridgeFound = true;
          } else if (window.__androidLogout) {
            window.__androidLogout();
            bridgeFound = true;
          }
        }
        
        // If not in a native bridge, or bridge failed, use web logout
        if (!bridgeFound) {
          if (confirm('Вы уверены, что хотите выйти?')) {
            if (window.WEB_LOGOUT) {
              window.WEB_LOGOUT();
            } else {
              localStorage.removeItem('svoyvpn_web_jwt');
              sessionStorage.clear();
              location.reload();
            }
          }
        }
      });
    }

    // Load data
    loadData();
    loadUser();

    window._pendingLinkEmail = '';

    function resetLinkEmailModalSteps() {
      const s1 = document.getElementById('linkEmailStep1');
      const sM = document.getElementById('linkEmailMergeStep');
      const s2 = document.getElementById('linkEmailStep2');
      const t = document.querySelector('#modalLinkEmail .modal-link-email__title');
      if (s1) s1.style.display = 'block';
      if (sM) sM.style.display = 'none';
      if (s2) s2.style.display = 'none';
      if (t) t.textContent = 'Привязка почты';
    }

    window.openLinkEmailModal = function () {
      if (!tg || !tg.initData) {
        showToast('Нужен Telegram');
        return;
      }
      window._pendingLinkEmail = '';
      resetLinkEmailModalSteps();
      const a = document.getElementById('linkEmailAddr');
      const p = document.getElementById('linkEmailPass');
      const o = document.getElementById('linkEmailOtp');
      if (a) a.value = '';
      if (p) p.value = '';
      if (o) o.value = '';
      showModal('modalLinkEmail');
    };

    async function handleLinkEmailSendClick(btnEl, confirmMerge) {
      if (!tg || !tg.initData) return;
      const email = (document.getElementById('linkEmailAddr') || {}).value.trim().toLowerCase();
      const password = (document.getElementById('linkEmailPass') || {}).value;
      if (!email || email.indexOf('@') < 0) {
        showToast('Укажите email');
        return;
      }
      btnEl.disabled = true;
      const base = window.AppConfig.apiBaseURL || '';
      const r = await fetch(base + '/api/auth/link-email/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          initData: tg.initData,
          email,
          password,
          confirm_merge: !!confirmMerge,
        }),
      });
      let d = {};
      try {
        d = await r.json();
      } catch (_) {}
      btnEl.disabled = false;
      if (!r.ok) {
        showToast(d.error || 'Ошибка');
        return;
      }
      if (d.status === 'already_linked') {
        showToast('Почта уже привязана');
        hideModal('modalLinkEmail');
        await loadUser(true);
        return;
      }
      if (d.status === 'merge_confirm_required') {
        const masked = d.masked_email || '…';
        const mEl = document.getElementById('linkEmailMergeMasked');
        if (mEl) mEl.textContent = masked;
        const s1 = document.getElementById('linkEmailStep1');
        const sM = document.getElementById('linkEmailMergeStep');
        const t = document.querySelector('#modalLinkEmail .modal-link-email__title');
        if (s1) s1.style.display = 'none';
        if (sM) sM.style.display = 'block';
        if (t) t.textContent = 'Объединение аккаунтов';
        return;
      }
      window._pendingLinkEmail = email;
      const s1 = document.getElementById('linkEmailStep1');
      const sM = document.getElementById('linkEmailMergeStep');
      const s2 = document.getElementById('linkEmailStep2');
      const t = document.querySelector('#modalLinkEmail .modal-link-email__title');
      if (s1) s1.style.display = 'none';
      if (sM) sM.style.display = 'none';
      if (s2) s2.style.display = 'block';
      if (t) t.textContent = 'Привязка почты';
      showToast('Код отправлен в Telegram и на почту');
    }

    const btnSend = document.getElementById('btnLinkEmailSend');
    if (btnSend) {
      btnSend.addEventListener('click', function () {
        handleLinkEmailSendClick(this, false);
      });
    }
    const btnMergeOk = document.getElementById('btnLinkEmailMergeConfirm');
    if (btnMergeOk) {
      btnMergeOk.addEventListener('click', function () {
        handleLinkEmailSendClick(this, true);
      });
    }
    const btnMergeCancel = document.getElementById('btnLinkEmailMergeCancel');
    if (btnMergeCancel) {
      btnMergeCancel.addEventListener('click', function () {
        resetLinkEmailModalSteps();
      });
    }

    const btnConf = document.getElementById('btnLinkEmailConfirm');
    if (btnConf) {
      btnConf.addEventListener('click', async function () {
        if (!tg || !tg.initData) return;
        const email =
          window._pendingLinkEmail ||
          (document.getElementById('linkEmailAddr') || {}).value.trim().toLowerCase();
        const otp = (document.getElementById('linkEmailOtp') || {}).value.trim();
        if (!otp) {
          showToast('Введите код');
          return;
        }
        this.disabled = true;
        const base = window.AppConfig.apiBaseURL || '';
        const r = await fetch(base + '/api/auth/link-email/confirm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ initData: tg.initData, email, otp }),
          skipWebJwt: true,
        });
        let d = {};
        try {
          d = await r.json();
        } catch (_) {}
        this.disabled = false;
        if (!r.ok) {
          showToast(d.error || 'Ошибка');
          return;
        }
        hideModal('modalLinkEmail');
        showToast('Почта привязана');
        await loadUser(true);
      });
    }

    // ═══════════════════════════════════════
    //  ONBOARDING CAROUSEL — Setup Screen
    // ═══════════════════════════════════════
    initOnboarding();
  });

  /* ─────────────────────────────────────────
     Onboarding carousel controller
  ───────────────────────────────────────── */
  function initOnboarding() {
    const TOTAL_SLIDES = 6;

    let currentSlide = 0;
    let selectedDevice = null;
    let selectedApp = null;

    const APPS = {
      apple: [
        { id: 'happ', name: 'Happ', iconImg: './images/happ.png', storeUrl: 'https://apps.apple.com/kz/app/happ-proxy-utility/id6504287215' },
        { id: 'hiddify', name: 'Hiddify', iconImg: './images/hiddify.png', storeUrl: 'https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532' },
        { id: 'v2raytun', name: 'V2RayTun', iconImg: './images/v2raytun.png', storeUrl: 'https://apps.apple.com/app/v2raytun/id6476628951' }
      ],
      android: [
        { id: 'happ', name: 'Happ', iconImg: './images/happ.png', storeUrl: 'https://play.google.com/store/apps/details?id=com.happproxy' },
        { id: 'hiddify', name: 'Hiddify', iconImg: './images/hiddify.png', storeUrl: 'https://play.google.com/store/apps/details?id=app.hiddify.com' },
        { id: 'v2raytun', name: 'V2RayTun', iconImg: './images/v2raytun.png', storeUrl: 'https://play.google.com/store/apps/details?id=com.v2raytun.android' }
      ],
      windows: [
        { id: 'happ', name: 'Happ', iconImg: './images/happ.png', storeUrl: 'https://github.com/Happ-proxy/happ-desktop/releases/download/2.4.0/setup-Happ.x64.exe' },
        { id: 'hiddify', name: 'Hiddify', iconImg: './images/hiddify.png', storeUrl: 'https://github.com/hiddify/hiddify-app/releases' },
        { id: 'v2rayn', name: 'V2RayN', iconImg: './images/v2raytun.png', storeUrl: 'https://github.com/2dust/v2rayN/releases' }
      ],
      mac: [
        { id: 'happ', name: 'Happ', iconImg: './images/happ.png', storeUrl: 'https://apps.apple.com/kz/app/happ-proxy-utility/id6504287215' },
        { id: 'hiddify', name: 'Hiddify', iconImg: './images/hiddify.png', storeUrl: 'https://github.com/hiddify/hiddify-app/releases' },
        { id: 'v2raytun', name: 'V2RayTun', iconImg: './images/v2raytun.png', storeUrl: 'https://apps.apple.com/app/v2raytun/id6476628951' }
      ]
    };

    const track = document.getElementById('obTrack');
    const btnNext = document.getElementById('obBtnNext');
    const btnBack = document.getElementById('obBtnBack');
    const dots = [0, 1, 2, 3, 4, 5].map(i => document.getElementById('obDot' + i));
    const obActionRow = document.getElementById('obActionRow');
    const obBtnCopied = document.getElementById('obBtnCopied');
    const obConnectContent = document.getElementById('obConnectContent');
    const obDownloadContent = document.getElementById('obDownloadContent');

    if (!track || !btnNext || !btnBack) return;

    function goToSlide(idx, direction) {
      if (idx < 0 || idx >= TOTAL_SLIDES) return;

      const animClass = direction === 'forward' ? 'anim-in' : 'anim-back';
      const slide = document.getElementById('obSlide' + idx);
      if (slide) {
        slide.classList.remove('anim-in', 'anim-back');
        void slide.offsetWidth;
        slide.classList.add(animClass);
        setTimeout(() => slide.classList.remove(animClass), 400);
      }

      currentSlide = idx;
      
      const subActive = S.subscription && S.subscription.isActive;
      const shift = subActive ? 1 : 0;
      track.style.transform = `translateX(-${(idx - shift) * 100}%)`;

      dots.forEach((d, i) => {
        if (d) d.classList.toggle('active', i === idx);
      });

      updateButtons();
      haptic('light');
    }

    function updateButtons() {
      const subActive = S.subscription && S.subscription.isActive;
      const isActuallyStart = currentSlide === 0 || (subActive && currentSlide === 1);

      // Label
      if (currentSlide === 0) {
        btnNext.textContent = 'Выбрать тариф';
      } else if (currentSlide === TOTAL_SLIDES - 1) {
        btnNext.textContent = 'Понятно ✓';
      } else {
        btnNext.textContent = 'Далее →';
      }

      // Layout split (Back button)
      if (isActuallyStart) {
        if (obActionRow) obActionRow.classList.remove('split');
      } else {
        if (obActionRow) obActionRow.classList.add('split');
        if (obBtnCopied) {
          obBtnCopied.textContent = '← Назад';
          obBtnCopied.className = 'ob-btn-copied is-back';
        }
      }

      // Disabled state
      switch (currentSlide) {
        case 0: btnNext.disabled = false; break;
        case 1: btnNext.disabled = !selectedDevice; break;
        default: btnNext.disabled = false;
      }

      if (btnBack) btnBack.style.display = 'none';
    }

    function renderDownloadSlide() {
      if (!obDownloadContent || !selectedDevice) return;
      obDownloadContent.innerHTML = '';

      const title = document.createElement('p');
      title.className = 'ob-title';
      title.style.marginTop = '20px';
      title.textContent = 'Установка';
      obDownloadContent.appendChild(title);

      const desc = document.createElement('p');
      desc.className = 'ob-desc';
      desc.textContent = 'Скачайте и установите одно из этих приложений:';
      obDownloadContent.appendChild(desc);

      const list = document.createElement('div');
      list.className = 'ob-app-list';
      const apps = APPS[selectedDevice] || [];

      apps.forEach(app => {
        const item = document.createElement('a');
        item.className = 'ob-app-item';
        item.href = app.storeUrl;
        item.target = '_blank';
        item.rel = 'noopener';
        item.addEventListener('click', (e) => {
          e.preventDefault();
          const tg = window.Telegram && window.Telegram.WebApp;
          tg && tg.openLink ? tg.openLink(app.storeUrl) : window.open(app.storeUrl, '_blank');
        });

        const iconHtml = '<div class="ob-app-icon ob-app-icon--img" style="background:rgba(58,168,252,.12);">' +
          '<img src="' + app.iconImg + '?v=70" alt="' + app.name + '" ' +
          'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'" />' +
          '<span class="ob-app-icon-fallback" style="display:none; color:var(--accent_text_color,#3aa8fc); font-weight:bold; font-size:14px;">' + app.name.charAt(0) + '</span>' +
          '</div>';

        item.innerHTML =
          iconHtml +
          '<div class="ob-app-info">' +
          '<p class="ob-app-name">' + app.name + '</p>' +
          '<p class="ob-app-store text-muted">Скачать приложение</p>' +
          '</div>' +
          '<span class="ob-app-arrow">›</span>';

        list.appendChild(item);
      });
      obDownloadContent.appendChild(list);
    }

    function renderConnectSlide() {
      if (!obConnectContent || !selectedDevice) return;
      obConnectContent.innerHTML = '';

      const title = document.createElement('p');
      title.className = 'ob-title';
      title.style.marginTop = '20px';
      title.textContent = 'Подключение';
      obConnectContent.appendChild(title);

      const desc = document.createElement('p');
      desc.className = 'ob-desc';
      desc.textContent = 'Нажмите на приложение, чтобы добавить серверы:';
      obConnectContent.appendChild(desc);

      const list = document.createElement('div');
      list.className = 'ob-app-list';
      const apps = APPS[selectedDevice] || [];
      const token = S.subscription ? (S.subscription.token || 'TOKEN') : 'TOKEN';

      apps.forEach(app => {
        const devicePath = selectedDevice === 'mac' ? 'apple' : selectedDevice;
        const connectUrl = `https://xdoublegroup.online/${devicePath}/${app.id}/${token}`;
        const item = document.createElement('a');
        item.className = 'ob-app-item';
        item.href = connectUrl;
        item.target = '_blank';
        item.rel = 'noopener';
        item.addEventListener('click', (e) => {
          e.preventDefault();
          const tg = window.Telegram && window.Telegram.WebApp;
          tg && tg.openLink ? tg.openLink(connectUrl) : window.open(connectUrl, '_blank');
        });

        const iconHtml = '<div class="ob-app-icon ob-app-icon--img" style="background:rgba(52,199,89,.12);">' +
          '<img src="' + app.iconImg + '?v=70" alt="' + app.name + '" ' +
          'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'" />' +
          '<span class="ob-app-icon-fallback" style="display:none; color:#34c759; font-weight:bold; font-size:14px;">' + app.name.charAt(0) + '</span>' +
          '</div>';

        item.innerHTML =
          iconHtml +
          '<div class="ob-app-info">' +
          '<p class="ob-app-name">' + app.name + '</p>' +
          '<p class="ob-app-store" style="color:#34c759; font-weight:500;">Подключить в 1 клик</p>' +
          '</div>' +
          '<span class="ob-app-arrow" style="color:#34c759;">›</span>';

        list.appendChild(item);
      });
      obConnectContent.appendChild(list);
    }

    if (obBtnCopied) {
      obBtnCopied.addEventListener('click', () => {
        if (currentSlide > 0) {
          const subActive = S.subscription && S.subscription.isActive;
          if (subActive && currentSlide === 1) return;
          goToSlide(currentSlide - 1, 'back');
        }
      });
    }

    document.querySelectorAll('.ob-device-card').forEach(card => {
      card.addEventListener('click', () => {
        document.querySelectorAll('.ob-device-card').forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        selectedDevice = card.dataset.device;
        haptic('light');
        updateButtons();
      });
    });

    btnNext.addEventListener('click', () => {
      haptic('light');
      if (currentSlide === 0) {
        if (S.subscription && S.subscription.isActive) {
          goToSlide(1, 'forward');
        } else {
          window.showModal('modalPlan');
        }
      } else if (currentSlide < TOTAL_SLIDES - 1) {
        if (currentSlide === 1) renderDownloadSlide();
        if (currentSlide === 2) renderConnectSlide();
        goToSlide(currentSlide + 1, 'forward');
      } else {
        showScreen('screenVpn');
      }
    });

    let touchStartX = 0;
    let touchStartY = 0;
    let isSwiping = false;
    const carousel = document.getElementById('obCarousel');

    if (carousel) {
      carousel.addEventListener('touchstart', e => {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
        isSwiping = false;
      }, { passive: true });

      carousel.addEventListener('touchmove', e => {
        const dx = e.touches[0].clientX - touchStartX;
        const dy = e.touches[0].clientY - touchStartY;
        if (!isSwiping && Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 8) {
          isSwiping = true;
        }
      }, { passive: true });

      carousel.addEventListener('touchend', e => {
        if (!isSwiping) return;
        const dx = e.changedTouches[0].clientX - touchStartX;
        if (Math.abs(dx) < 40) return;

        if (dx < 0 && currentSlide < TOTAL_SLIDES - 1) {
          const canNext = !btnNext.disabled;
          if (!canNext) { haptic('error'); return; }
          const subActive = S.subscription && S.subscription.isActive;
          if (currentSlide === 1) renderDownloadSlide();
          if (currentSlide === 2) renderConnectSlide();
          goToSlide(currentSlide + 1, 'forward');
        } else if (dx > 0 && currentSlide > 0) {
          const subActive = S.subscription && S.subscription.isActive;
          if (subActive && currentSlide === 1) {
            isSwiping = false;
            return;
          }
          goToSlide(currentSlide - 1, 'back');
        }
        isSwiping = false;
      }, { passive: true });
    }

    function resetCarousel() {
      const subActive = S.subscription && S.subscription.isActive;
      currentSlide = subActive ? 1 : 0;
      selectedDevice = null;
      const shift = subActive ? 1 : 0;
      track.style.transform = `translateX(-${(currentSlide - shift) * 100}%)`;
      dots.forEach((d, i) => d && d.classList.toggle('active', i === currentSlide));
      document.querySelectorAll('.ob-device-card').forEach(c => c.classList.remove('selected'));
      if (obActionRow) obActionRow.classList.remove('split');
      updateButtons();
    }

    function updateOnboardingSubState() {
      const subActive = S.subscription && S.subscription.isActive;
      const s0 = document.getElementById('obSlide0');
      const d0 = document.getElementById('obDot0');
      if (s0) s0.style.display = subActive ? 'none' : 'block';
      if (d0) d0.style.display = subActive ? 'none' : 'block';

      if (subActive && currentSlide === 0) {
        currentSlide = 1;
        updateButtons();
        track.style.transform = 'translateX(0)';
        dots.forEach((d, i) => d && d.classList.toggle('active', i === 1));
      } else if (!subActive && currentSlide > 0) {
        currentSlide = 0;
        updateButtons();
        track.style.transform = 'translateX(0)';
        dots.forEach((d, i) => d && d.classList.toggle('active', i === 0));
      }
    }
    window.updateOnboardingSubState = updateOnboardingSubState;
    window.onboardingNext = () => {
      if (currentSlide < TOTAL_SLIDES - 1) {
        if (currentSlide === 1) renderDownloadSlide();
        if (currentSlide === 2) renderConnectSlide();
        goToSlide(currentSlide + 1, 'forward');
      }
    };

    document.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.dataset.screen !== 'screenSetup') {
          setTimeout(resetCarousel, 400);
        } else {
          updateOnboardingSubState();
        }
      });
    });

    updateButtons();
  }
})();




