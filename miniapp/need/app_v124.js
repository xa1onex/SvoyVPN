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

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  window.onerror = function (msg, url, line, col, error) {
    const errDiv = document.createElement('div');
    errDiv.style.cssText = 'position:fixed;top:0;left:0;width:100%;background:red;color:white;z-index:9999;padding:10px;font-size:12px;line-height:1.2;';
    const b = document.createElement('b');
    b.textContent = 'JS Error: ';
    errDiv.appendChild(b);
    errDiv.appendChild(
      document.createTextNode(String(msg) + ' Line: ' + String(line) + ':' + String(col))
    );
    document.body.appendChild(errDiv);
    return false;
  };

  /* ── State ── */
  const S = {
    user: null,
    subscription: null,
    referral: null,
    /** Вход по email в WebView (отрицательный user id) — нужна привязка Telegram */
    authViaWebJwt: false,
    tariffs: [],
    paymentMethods: [],
    servers: [],
    selectedTariff: null,
    selectedPM: null,
    trafficPacks: [],
    selectedTrafficPack: null,
    trafficPackPM: null,
    esimCountries: [],
    esimPackages: [],
    esimSelectedCountry: '',
    esimSelectedPackage: null,
    esimMode: 'test',
    esimLoadedCountries: false,
    esimSelectedPM: null,
  };

  const PRODUCT_KEY = 'svoy_product';

  function productHomeScreenId() {
    try {
      return localStorage.getItem(PRODUCT_KEY) === 'esim' ? 'screenEsim' : 'screenVpn';
    } catch (_) {
      return 'screenVpn';
    }
  }

  function applyProductSwitchUI() {
    let p = 'vpn';
    try {
      p = localStorage.getItem(PRODUCT_KEY) || 'vpn';
    } catch (_) {}
    document.documentElement.setAttribute('data-product', p);
    const bar = document.getElementById('tabBar');
    if (bar) bar.classList.toggle('tab-bar--esim', p === 'esim');

    document.querySelectorAll('.product-switch__btn').forEach((btn) => {
      const on = btn.dataset.product === p;
      btn.classList.toggle('product-switch__btn--active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  function setProduct(product, navigate) {
    try {
      localStorage.setItem(PRODUCT_KEY, product);
    } catch (_) {}
    applyProductSwitchUI();
    if (navigate) showScreen(product === 'esim' ? 'screenEsim' : 'screenVpn');
    if (product === 'esim') {
      syncEsimBetaGate();
      if (userIsEsimAdmin()) ensureEsimCountriesLoaded();
    }
  }

  function escapeHtml(s) {
    if (s == null || s === '') return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function isSafeHttpsUrl(u) {
    if (u == null || u === '') return false;
    try {
      return new URL(String(u).trim(), window.location.origin).protocol === 'https:';
    } catch (_) {
      return false;
    }
  }

  function isSafeHttpUrl(u) {
    if (u == null || u === '') return false;
    try {
      const p = new URL(String(u).trim());
      return p.protocol === 'https:' || p.protocol === 'http:';
    } catch (_) {
      return false;
    }
  }

  function isSafeTelegramDeepLink(u) {
    if (!u) return false;
    try {
      const p = new URL(String(u));
      if (p.protocol !== 'https:') return false;
      const h = p.hostname.toLowerCase();
      return h === 't.me' || h === 'telegram.me' || h.endsWith('.t.me');
    } catch (_) {
      return false;
    }
  }

  function normalizeReferralFromApi(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const code = raw.referralCode || raw.referral_code;
    if (!code) return null;
    const bot =
      (window.AppConfig && window.AppConfig.referralBotUsername) || 'SvoyVPN_bot';
    const fallback =
      'https://t.me/' + bot + '?start=ref_' + encodeURIComponent(String(code));
    let refLink = String(raw.refLink || raw.ref_link || '').trim();
    if (!refLink) {
      refLink = fallback;
    } else {
      try {
        const p = new URL(refLink);
        const h = p.hostname.toLowerCase();
        if (
          (p.protocol !== 'https:' && p.protocol !== 'http:') ||
          (h !== 't.me' && h !== 'telegram.me' && !h.endsWith('.t.me'))
        ) {
          refLink = fallback;
        }
      } catch (_) {
        refLink = fallback;
      }
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

  /** ДД.ММ.ГГГГ для подписи в скобках */
  function fmtDateShortDots(iso) {
    if (!iso) return '—';
    const s = String(iso).trim();
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    let y, mo, d;
    if (m) {
      y = +m[1];
      mo = +m[2] - 1;
      d = +m[3];
    } else {
      const dt = new Date(iso);
      if (Number.isNaN(dt.getTime())) return '—';
      y = dt.getFullYear();
      mo = dt.getMonth();
      d = dt.getDate();
    }
    return `${String(d).padStart(2, '0')}.${String(mo + 1).padStart(2, '0')}.${y}`;
  }

  /** Календарных дней от сегодня до дня окончания: 0 = сегодня, 1 = завтра */
  function calendarDaysUntilEnd(iso) {
    if (!iso) return null;
    const s = String(iso).trim();
    let y, mo, d;
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) {
      y = +m[1];
      mo = +m[2] - 1;
      d = +m[3];
    } else {
      const dt = new Date(iso);
      if (Number.isNaN(dt.getTime())) return null;
      y = dt.getFullYear();
      mo = dt.getMonth();
      d = dt.getDate();
    }
    const endDay = new Date(y, mo, d);
    const now = new Date();
    const startDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((endDay - startDay) / 86400000);
  }

  /** HTML: «VPN: активен СЕГОДНЯ/ЗАВТРА (ДД.ММ.ГГГГ)» или «Действует до …» */
  function formatVpnActiveDateLine(iso) {
    const cal = calendarDaysUntilEnd(iso);
    const short = fmtDateShortDots(iso);
    if (cal === 0) return `VPN: активен <b>СЕГОДНЯ</b> (${escapeHtml(short)})`;
    if (cal === 1) return `VPN: активен <b>ЗАВТРА</b> (${escapeHtml(short)})`;
    return `Действует до ${escapeHtml(fmtDate(iso))}`;
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

  /* ═══════ Theme ═══════
     SDK по умолчанию colorScheme = 'light'; вне Telegram initData пустой — иначе OS-тема игнорируется. */
  function isTelegramMiniApp() {
    return !!(tg && typeof tg.initData === 'string' && tg.initData.length > 0);
  }

  function systemColorScheme() {
    try {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    } catch (_) {
      return 'light';
    }
  }

  function getResolvedThemeScheme() {
    if (URL_THEME) return URL_THEME;
    if (isTelegramMiniApp()) {
      const cs = tg.colorScheme;
      return cs === 'dark' || cs === 'light' ? cs : 'dark';
    }
    return systemColorScheme();
  }

  function forceHeaderColor() {
    if (!tg) return;
    const scheme = getResolvedThemeScheme();
    const bgColor = scheme === 'dark' ? '#18222d' : '#ffffff';
    const secBgColor = scheme === 'dark' ? '#21303f' : '#f7f9fb';
    try { tg.setHeaderColor(bgColor); } catch (_) { }
    try { tg.setBackgroundColor(bgColor); } catch (_) { }
    try { tg.setBottomBarColor(secBgColor); } catch (_) { }
  }

  function applyTheme() {
    const scheme = getResolvedThemeScheme();
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
      const res = await fetch('/miniapp/need/assets/sprite.svg');
      if (!res.ok) return;
      const text = await res.text();
      const host = document.getElementById('spriteHost');
      if (host) host.innerHTML = text;
    } catch (_) {
      // Sprite failed to load — icons will be empty but app works
    }
  }

  const SERVERS_PROFILE_HINT_KEY = 'svoy_hint_servers_profile_v2';
  const PROFILE_TAB_SERVERS_HINT_TITLE = 'Сервера и пинг — во вкладке «Профиль»';
  const DESKTOP_MAP_MIN = 1024;

  function isDesktopMapServers() {
    return typeof window !== 'undefined' && window.innerWidth >= DESKTOP_MAP_MIN;
  }

  function dismissServersRelocatedHint() {
    const dot = document.getElementById('profileServersHintDot');
    if (dot) dot.hidden = true;
    const tab = document.querySelector('.tab[data-screen="screenProfile"]');
    if (tab) tab.removeAttribute('title');
    try {
      localStorage.setItem(SERVERS_PROFILE_HINT_KEY, '1');
    } catch (_) {}
  }

  function maybeShowServersRelocatedHint() {
    const dot = document.getElementById('profileServersHintDot');
    if (isDesktopMapServers()) {
      if (dot) dot.hidden = true;
      const tab = document.querySelector('.tab[data-screen="screenProfile"]');
      if (tab) tab.removeAttribute('title');
      return;
    }
    try {
      if (localStorage.getItem(SERVERS_PROFILE_HINT_KEY)) {
        if (dot) dot.hidden = true;
        return;
      }
    } catch (_) {}
    if (!dot) return;
    dot.hidden = false;
    const tab = document.querySelector('.tab[data-screen="screenProfile"]');
    if (tab) tab.setAttribute('title', PROFILE_TAB_SERVERS_HINT_TITLE);
  }

  /* ═══════ Navigation ═══════ */
  function showScreen(id) {
    document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));

    const screen = document.getElementById(id);
    if (screen) screen.classList.add('active');

    let tab = document.querySelector(`.tab[data-screen="${id}"]`);
    if (!tab && id === 'screenEsim') {
      tab = document.querySelector('.tab[data-home-tab="1"]');
    }
    if (tab) tab.classList.add('active');

    if (id === 'screenEsim') {
      syncEsimBetaGate();
      if (userIsEsimAdmin()) ensureEsimCountriesLoaded();
    }
    if (id === 'screenEsimMine') {
      syncEsimBetaGate();
      if (userIsEsimAdmin()) loadEsimMineList();
    }
    if (id === 'screenVpn') maybeShowServersRelocatedHint();
    if (id === 'screenProfile') dismissServersRelocatedHint();

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
    opts = opts || {};
    try {
      const r = await fetch(url, opts);
      const d = await r.json();
      if (!r.ok) {
        if (!opts.silentError) {
          showToast('API Error: ' + (d.error || r.status), 5000);
        }
        return opts.silentError ? d : null;
      }
      return d;
    } catch (e) {
      if (!opts.silentError) {
        showToast('Fetch Error: ' + e.message, 5000);
      }
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

      const n = m.name ? m.name.toLowerCase() : '';
      let svgIcon;
      if (m.id === 'stars' || n.includes('star')) {
        svgIcon = `<svg viewBox="0 0 24 24" class="icon-star"><path d="M12 2.3l2.4 7.4 7.6.6-5.8 4.7 1.8 7.3-6-4.3-6 4.3 1.8-7.3-5.8-4.7 7.6-.6z" stroke="currentColor" stroke-width="2" stroke-linejoin="round" class="star-shape"/><circle class="sparkle sp-1" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-2" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-3" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-4" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-5" cx="12" cy="12" r="1.5"/></svg>`;
      } else if (m.id === 'cryptopay' || n.includes('crypto')) {
        svgIcon = `<svg viewBox="0 0 77 42" class="icon-crypto" style="width:32px;height:18px;display:block;">
          <path d="M2.72194715,0 L26.6266393,0 C28.4220313,0 30.0735687,0.988569903 30.9307924,2.57636085 L52.2150034,42 L23.7342299,42 C21.9388379,42 20.2873006,41.0114301 19.4300769,39.4236391 L0.330751009,4.04694928 C-0.386869546,2.71773798 0.101959028,1.05466888 1.42258019,0.332380469 C1.82138733,0.114260557 2.26806337,0 2.72194715,0 Z" fill="#25A3F2"/>
          <path d="M73.643684,0 C74.0975678,0 74.5442438,0.114260557 74.943051,0.332380469 C76.2236533,1.03278135 76.7221109,2.61780981 76.0968053,3.92522764 L76.0348801,4.04694928 L56.9355543,39.4236391 C56.1059829,40.960211 54.5325175,41.9355978 52.8046779,41.996927 L52.6314012,42 L24.5945392,42 L23.7342299,42 L45.4348388,2.57636085 C46.2644101,1.03978897 47.8378756,0.0644022425 49.5657151,0.00307299695 L49.7389918,0 L73.643684,0 Z" fill="#25A3F2" fill-opacity="0.85"/>
        </svg>`;
      } else if (m.id === 'yookassa' || n.includes('юkassa') || n.includes('юкасса') || n.includes('юк') || n.includes('yoo') || n.includes('yuk') || n.includes('карт') || n.includes('card')) {
        svgIcon = `<svg viewBox="0 0 24 24" class="icon-card"><rect x="2" y="5" width="20" height="14" rx="2" ry="2" fill="none" class="card-outline"></rect><line x1="2" y1="10" x2="22" y2="10" class="card-line"></line></svg>`;
      } else {
        svgIcon = escapeHtml(String(m.icon || '💳'));
      }

      el.innerHTML =
        `<span class="pm-icon flex-center">${svgIcon}</span>` +
        `<span class="pm-name">${escapeHtml(m.name || '')}</span>`;
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
      const r = await fetch('/miniapp/api/ping?id=' + serverId, { cache: 'no-store' });
      const t1 = performance.now();
      if (!r.ok) return -1;
      const d = await r.json();
      if (d && typeof d.ping === 'number') return d.ping;
      return Math.round(t1 - t0);
    } catch (_) {
      return -1;
    }
  }

  function createServerCard(s, opts) {
    opts = opts || {};
    const carousel = !!opts.carousel;
    const el = document.createElement('div');
    el.className = 'server-card' + (carousel ? ' server-card--carousel' : '');
    el.setAttribute('data-server-id', s.id);
    const flagOrEmoji = escapeHtml(String(s.emoji || getFlag(s.name) || ''));
    const nameEsc = escapeHtml(String(s.name || ''));
    const ipEsc = escapeHtml(String(maskIp(s.ip) || ''));
    el.innerHTML =
      '<div class="server-card__header">' +
      '<span class="server-card__flag">' + flagOrEmoji + '</span>' +
      '<span class="server-card__name">' + nameEsc + '</span>' +
      '</div>' +
      '<span class="server-card__ip">' + ipEsc + '</span>' +
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
    const wProfile = document.getElementById('serversWrapProfile');
    var oldNav = document.getElementById('serverNav');
    if (oldNav) oldNav.remove();
    if (!wProfile) return;

    if (!S.servers.length) {
      wProfile.innerHTML = '<div class="server-card server-card--loading text-muted body">Нет серверов</div>';
      return;
    }

    wProfile.innerHTML = '';
    S.servers.forEach(function (s) {
      wProfile.appendChild(createServerCard(s, { carousel: true }));
    });
  }

  // Auto-refresh pings every 60 seconds (only visible cards)
  let pingInterval;
  function startPingRefresh() {
    if (pingInterval) clearInterval(pingInterval);
    pingInterval = setInterval(function () {
      if (!S.servers.length) return;
      document.querySelectorAll('#serversWrapProfile .server-card[data-server-id]').forEach(function (card) {
        var sid = card.getAttribute('data-server-id');
        var pingWrap = card.querySelector('.server-card__ping-wrap');
        if (!sid || !pingWrap) return;
        measurePing(sid).then(function (ms) { renderPingBadge(pingWrap, ms); });
      });
    }, 60000);
  }

  /* ═══════ Render: Total ═══════ */
  function updateTotal() {
    const el = document.getElementById('totalPrice');
    const btn = document.getElementById('btnPay');
    if (S.selectedTariff) {
      const isStars = S.selectedPM && S.selectedPM.id === 'stars';
      const starIcon = `<svg viewBox="0 0 24 24" style="width:1em;height:1em;vertical-align:-0.15em;fill:var(--accent_text_color, #3aa8fc)"><path d="M12 2.3l2.4 7.4 7.6.6-5.8 4.7 1.8 7.3-6-4.3-6 4.3 1.8-7.3-5.8-4.7 7.6-.6z" stroke="var(--accent_text_color, #3aa8fc)" stroke-width="2" stroke-linejoin="round"/></svg>`;
      const currency = isStars ? starIcon : '₽';
      const rubBase = Number(S.selectedTariff.basePrice ?? S.selectedTariff.price ?? 0);
      const starsBase = Number(S.selectedTariff.basePriceStars ?? S.selectedTariff.priceStars ?? 0);
      const price = isStars ? starsBase : rubBase;

      el.innerHTML = fmtPrice(price) + ' ' + currency;
      btn.disabled = !S.selectedPM;
    } else {
      el.textContent = '—';
      btn.disabled = true;
    }
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
        '<p class="body text-muted" style="margin:0 0 12px;font-size:13px;line-height:1.4;">Код придёт сюда и на email — можно входить с сайта.</p>' +
        '<button type="button" class="btn-secondary" style="width:100%" onclick="window.openLinkEmailModal()">Привязать почту</button>';
    } else if (needE) {
      inner +=
        '<p class="body text-muted" style="margin:0;font-size:13px;line-height:1.4;">Откройте приложение из Telegram.</p>';
    }
    if (needTg && S.authViaWebJwt) {
      inner +=
        '<p class="subtitle" style="margin:12px 0 6px;">Привяжите Telegram</p>' +
        '<p class="body text-muted" style="margin:0 0 12px;font-size:13px;line-height:1.4;">Откройте бота и нажмите Start.</p>' +
        '<button type="button" class="btn-primary" style="width:100%" onclick="window.startLinkTelegramFlow()">Открыть бота</button>';
    }
    inner += '</div>';
    box.innerHTML = inner;
  }

  window.startLinkTelegramFlow = async function () {
    const jwt = localStorage.getItem('svoyvpn_web_jwt') || ANDROID_JWT;
    if (!jwt) {
      showToast('Сначала войдите по почте');
      return;
    }
    const r = await fetch('/miniapp/api/auth/link-telegram/init', {
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
    if (!isSafeTelegramDeepLink(d.botUrl)) {
      showToast('Некорректная ссылка бота');
      return;
    }
    showModal('modalLinkTg');
    const a = document.getElementById('linkTgOpenBtn');
    if (a) a.href = d.botUrl;
    if (window._linkTgPoll) clearInterval(window._linkTgPoll);
    const nonce = d.nonce;
    window._linkTgPoll = setInterval(async () => {
      try {
        const pr = await fetch(
          '/miniapp/api/auth/link-telegram/poll?nonce=' + encodeURIComponent(nonce),
          { headers: { Authorization: 'Bearer ' + jwt } }
        );
        const pj = await pr.json();
        if (pj.status === 'ok' && pj.token) {
          clearInterval(window._linkTgPoll);
          window._linkTgPoll = null;
          try {
            localStorage.setItem('svoyvpn_web_jwt', pj.token);
          } catch (_) {}
          hideModal('modalLinkTg');
          showToast('Готово! Вход через Telegram');
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
      if (photoUrl && isSafeHttpsUrl(photoUrl) && avatar) {
        avatar.innerHTML = '';
        avatar.style.overflow = 'hidden';
        const img = document.createElement('img');
        img.src = photoUrl;
        img.alt = name;
        img.style.cssText = 'width:100%;height:100%;object-fit:cover;border-radius:50%;';
        img.onerror = function () {
          avatar.textContent = '';
          avatar.appendChild(
            document.createTextNode(name.charAt(0).toUpperCase())
          );
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

    let daysCal = null;
    if (sub && sub.isActive && sub.endDate) {
      daysCal = calendarDaysUntilEnd(sub.endDate);
    }

    if (pBadge) {
      const pBadgeText = document.getElementById('profileBadgeText');
      if (sub && sub.isActive) {
        pBadge.style.display = 'inline-flex';
        if (pBadgeText) {
          if (daysCal === 0) {
            pBadgeText.textContent = 'Сегодня';
          } else if (daysCal === 1) {
            pBadgeText.textContent = 'Завтра';
          } else if (daysCal != null && daysCal > 1) {
            pBadgeText.textContent = `Активна на ${daysCal} ${dw(daysCal)}`;
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
    if (S.user && S.user.supportLink && isSafeHttpUrl(S.user.supportLink)) {
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

        const daysMeta =
          daysCal === 0
            ? 'Подписка заканчивается сегодня — при необходимости продлите.'
            : daysCal === 1
              ? 'Остался 1 день полной свободы.'
              : daysCal != null && daysCal > 1
                ? `Осталось ${daysCal} ${dw(daysCal)} полной свободы.`
                : 'Срок окончания указан ниже — подключайтесь в любой момент.';
        const warnSoon = daysCal != null && daysCal >= 0 && daysCal <= 7;
        const heroMod = warnSoon ? ' sub-status-hero--warn' : '';

        let trafficLine = '';
        const tr = sub.traffic;
        if (tr && typeof tr.usedGb === 'number' && typeof tr.limitGb === 'number') {
          const used = Number(tr.usedGb);
          const lim = Number(tr.limitGb);
          const pct = lim > 0 ? Math.min(100, Math.round((used / lim) * 100)) : 0;
          const ex = tr.trafficExceeded ? ' <span style="color:#ff3b30;">Лимит исчерпан.</span>' : '';
          const bonusTotal = Number(tr.bonusGb || 0);
          const bonusRem = Number(tr.bonusRemainingGb || 0);
          const bonusSuffix = bonusTotal > 0
            ? ` · пакет: ${bonusRem.toFixed(1)} из ${bonusTotal} ГБ`
            : '';
          trafficLine = `<p class="sub-status-hero__meta" style="margin-top:6px;">Трафик: <b>${used.toFixed(1)} / ${lim.toFixed(0)} ГБ</b> (${pct}%)${bonusSuffix}${ex}</p>`;
        }

        let statusHtml = `
            <div class="card sub-status-hero sub-status-hero--active${heroMod}" role="status">
              <div class="sub-status-hero__ring" aria-hidden="true">
                <svg class="sub-status-hero__check" viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                  <polyline points="22 4 12 14.01 9 11.01"/>
                </svg>
              </div>
              <div class="sub-status-hero__body">
                <p class="sub-status-hero__title">Подписка активна</p>
                <p class="sub-status-hero__date">${formatVpnActiveDateLine(sub.endDate)}</p>
                <p class="sub-status-hero__meta">${escapeHtml(daysMeta)}</p>
                ${trafficLine}
                ${
                  warnSoon
                    ? '<p class="sub-status-hero__urgent">Скоро окончание — продлите подписку, чтобы оставться в сети!</p>'
                    : ''
                }
              </div>
            </div>
            <div class="gap-12"></div>
        `;

        statusHtml += `
          <div style="display:flex;flex-direction:column;gap:10px;">
            <button type="button" class="btn-primary btn-primary--sub-block" onclick="window.showModal('modalPlan')">Продлить</button>
            <button type="button" class="btn-secondary btn-primary--sub-block" onclick="window.openTrafficPackModal && window.openTrafficPackModal()">Докупить ГБ</button>
          </div>
        `;
        subBlockBox.innerHTML = statusHtml;

      } else {
        if (vpnStatus) vpnStatus.textContent = 'Быстрый и приватный VPN';
        if (pStatus) {
          pStatus.textContent = '';
        }

        if (S.user && S.user.trialAvailable) {
          const trialDays = Number(S.user.trialDays) || 0;
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
               <p class="body text-muted" style="margin-bottom:10px; font-size:12px; line-height: 1.3;">Доступно <b>${trialDays} дней</b> теста без привязки карты.</p>
               <button class="btn-primary" id="btnActivateTrial" style="min-height: 40px; font-size: 14px; padding: 8px;">Забрать ${trialDays} дней</button>
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

            const d = await api('/miniapp/api/trial/activate', {
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
              this.textContent = `Забрать ${trialDays} дней`;
            }
          });

        } else {
          subBlockBox.innerHTML = `
            <div class="card sub-status-hero sub-status-hero--inactive" role="status">
              <div class="sub-status-hero__ring sub-status-hero__ring--muted" aria-hidden="true">
                <svg class="sub-status-hero__lock" viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                  <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                </svg>
              </div>
              <div class="sub-status-hero__body">
                <p class="sub-status-hero__title">Подписка не оформлена</p>
                <p class="sub-status-hero__meta">Без тарифа VPN-серверы недоступны. Тарифы и оплата — в отдельном окне.</p>
              </div>
            </div>
            <div class="gap-12"></div>
            <button type="button" class="btn-primary btn-primary--sub-block" onclick="window.showModal('modalPlan')">Выбрать тариф</button>
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
          const obTrialDays = Number(S.user.trialDays) || 0;
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
              <button class="btn-primary" id="obBtnTrial" style="min-height:40px; font-size:14px; width:100%;">Забрать ${obTrialDays} дней</button>
            `;
          setTimeout(() => {
            const b = document.getElementById('obBtnTrial');
            if (b) b.onclick = async function () {
              this.disabled = true; this.textContent = '...';
              const d = await api('/miniapp/api/trial/activate', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initData: tg.initData })
              });
              if (d && d.status === 'ok') {
                showSuccessOverlay('Подарок получен! 🎁', 'Вы получили бесплатные дни доступа. Настройте устройство на следующем шаге!');
                await loadUser();
              } else {
                showToast('Ошибка: ' + (d ? d.error : '?'));
                this.disabled = false; this.textContent = `Забрать ${obTrialDays} дней`;
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
      if (elSetup) elSetup.value = sub.subscriptionUrl;
    }

    // Refresh servers layout (pageSize may change if trial block appeared)
    renderServers();
    syncEsimBetaGate();
  }

  function userIsEsimAdmin() {
    return !!(
      S.user &&
      (S.user.isAdmin === true || S.user.esimBetaAccess === true)
    );
  }

  function syncEsimBetaGate() {
    const admin = userIsEsimAdmin();
    document.querySelectorAll('.esim-beta-gate').forEach((el) => {
      el.hidden = admin;
    });
    const needMail = !!(S.user && S.user.needLinkEmail);
    document.querySelectorAll('.esim-beta-gate__hint--need-mail').forEach((el) => {
      el.hidden = !needMail;
    });
    document.querySelectorAll('.esim-beta-gate__hint--has-mail').forEach((el) => {
      el.hidden = needMail;
    });
    document.querySelectorAll('.esim-beta-gate__link-email').forEach((el) => {
      el.hidden = !needMail;
    });
  }

  function fmtBytes(n) {
    const v = Number(n) || 0;
    if (v >= 1073741824) return (v / 1073741824).toFixed(v >= 2147483648 ? 0 : 1) + ' ГБ';
    if (v >= 1048576) return Math.round(v / 1048576) + ' МБ';
    return v + ' Б';
  }

  const ESIM_API_PREFIX = '/miniapp/api';

  async function ensureEsimCountriesLoaded() {
    if (!userIsEsimAdmin()) return;
    if (S.esimLoadedCountries) return;
    const sel = document.getElementById('esimCountry');
    if (!sel) return;
    sel.innerHTML = '<option value="">Загрузка…</option>';
    let url = ESIM_API_PREFIX + '/esim/countries';
    if (tg && tg.initData) url += '?initData=' + encodeURIComponent(tg.initData);
    const skipJwt = !!(tg && tg.initData);
    try {
      const d = await api(url, { method: 'GET', skipWebJwt: skipJwt });
      if (d && d.countries && d.countries.length) {
        S.esimCountries = d.countries;
        S.esimMode = d.esimMode || 'test';
        S.esimLoadedCountries = true;
        sel.innerHTML = '<option value="">Выберите страну</option>';
        d.countries.forEach((c) => {
          const o = document.createElement('option');
          o.value = c.code;
          o.textContent = (c.name || c.code) + ' (' + c.code + ')';
          sel.appendChild(o);
        });
      } else {
        sel.innerHTML = '<option value="">Нет данных</option>';
        showToast(d && d.error ? d.error : 'Не удалось загрузить страны');
      }
    } catch (e) {
      sel.innerHTML = '<option value="">Ошибка сети</option>';
    }
    const hint = document.getElementById('esimModeHint');
    if (hint) {
      hint.textContent =
        S.esimMode === 'live' ? '' : 'Режим теста: после оплаты выдаётся демо eSIM (без провайдера).';
    }
  }

  async function loadEsimPackages(country) {
    if (!userIsEsimAdmin()) return;
    const wrap = document.getElementById('esimPackages');
    const buy = document.getElementById('btnEsimBuy');
    S.esimSelectedPackage = null;
    if (buy) buy.disabled = true;
    if (!wrap) return;
    if (!country) {
      wrap.innerHTML = '<div class="esim-packages-placeholder text-muted body">Выберите страну</div>';
      return;
    }
    wrap.innerHTML = '<div class="esim-packages-placeholder text-muted body">Загрузка…</div>';
    let url = ESIM_API_PREFIX + '/esim/packages?country=' + encodeURIComponent(country);
    if (tg && tg.initData) url += '&initData=' + encodeURIComponent(tg.initData);
    const skipJwt = !!(tg && tg.initData);
    try {
      const d = await api(url, { method: 'GET', skipWebJwt: skipJwt });
      if (!d || !d.packages || !d.packages.length) {
        wrap.innerHTML = '<div class="esim-packages-placeholder text-muted body">Нет тарифов</div>';
        return;
      }
      S.esimPackages = d.packages;
      S.esimMode = d.esimMode || S.esimMode;
      wrap.innerHTML = '';
      d.packages.forEach((pkg) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'esim-pkg';
        btn.dataset.code = pkg.packageCode;
        const vol = fmtBytes(pkg.volume);
        const days = pkg.duration ? pkg.duration + ' ' + (pkg.durationUnit === 'DAY' ? 'дн.' : pkg.durationUnit || '') : '';
        const priceK = pkg.salePriceKopecks != null ? pkg.salePriceKopecks : 0;
        const priceRub = (priceK / 100).toFixed(priceK % 100 === 0 ? 0 : 2);
        btn.innerHTML =
          '<p class="esim-pkg__title">' +
          escapeHtml(pkg.name || pkg.description || pkg.packageCode) +
          '</p>' +
          '<p class="esim-pkg__meta">' +
          escapeHtml(vol + (days ? ' · ' + days : '')) +
          '</p>' +
          '<p class="esim-pkg__price">' +
          escapeHtml(priceRub) +
          ' ₽</p>';
        btn.onclick = () => {
          wrap.querySelectorAll('.esim-pkg').forEach((x) => x.classList.remove('esim-pkg--selected'));
          btn.classList.add('esim-pkg--selected');
          S.esimSelectedPackage = pkg;
          if (buy) buy.disabled = false;
          const m = document.getElementById('modalEsimPay');
          if (m && m.classList.contains('active')) updateEsimPayTotal();
          haptic('light');
        };
        wrap.appendChild(btn);
      });
    } catch (e) {
      wrap.innerHTML = '<div class="esim-packages-placeholder text-muted body">Ошибка загрузки</div>';
    }
  }

  function hideEsimResult() {
    const block = document.getElementById('esimResult');
    if (block) block.style.display = 'none';
  }

  function showEsimDelivery(delivery) {
    const block = document.getElementById('esimResult');
    const img = document.getElementById('esimQrImg');
    const ac = document.getElementById('esimAcCode');
    const smdp = document.getElementById('esimSmdpCode');
    if (!block || !ac || !smdp) return;
    block.style.display = 'block';
    ac.textContent = delivery.activationCode || delivery.ac || '—';
    smdp.textContent = delivery.smdpAddress || delivery.smdp || '—';
    if (img) {
      if (delivery.qrImagePngBase64) {
        img.src = 'data:image/png;base64,' + delivery.qrImagePngBase64;
        img.style.display = 'block';
      } else if (delivery.qrCodeUrl && isSafeHttpsUrl(delivery.qrCodeUrl)) {
        img.src = delivery.qrCodeUrl;
        img.style.display = 'block';
      } else {
        img.style.display = 'none';
      }
    }
  }

  function fmtEsimMineWhen(iso) {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      return d.toLocaleString('ru-RU', {
        day: 'numeric',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch (_) {
      return String(iso);
    }
  }

  function esimMineQrHtml(del) {
    if (!del || typeof del !== 'object') return '';
    const b64 = del.qrImagePngBase64;
    if (b64 && typeof b64 === 'string') {
      const clean = b64.replace(/\s/g, '');
      if (/^[A-Za-z0-9+/=]+$/.test(clean)) {
        return (
          '<img class="esim-mine-card__qr" alt="" src="data:image/png;base64,' + clean + '"/>'
        );
      }
    }
    if (del.qrCodeUrl && isSafeHttpsUrl(del.qrCodeUrl)) {
      return (
        '<img class="esim-mine-card__qr" alt="" src="' + escapeHtml(del.qrCodeUrl) + '"/>'
      );
    }
    return '';
  }

  async function loadEsimMineList() {
    if (!userIsEsimAdmin()) return;
    const wrap = document.getElementById('esimMineList');
    if (!wrap) return;
    wrap.innerHTML = '<p class="body text-muted text-center">Загрузка…</p>';
    let url = ESIM_API_PREFIX + '/esim/mine';
    const opts = { method: 'GET', silentError: true };
    if (IS_ANDROID) {
      opts.headers = { Authorization: 'Bearer ' + ANDROID_JWT };
    } else if (tg && tg.initData) {
      url += '?initData=' + encodeURIComponent(tg.initData);
      opts.skipWebJwt = true;
    } else {
      wrap.innerHTML =
        '<p class="body text-muted text-center" style="padding:12px 8px;">Войдите через приложение Telegram.</p>';
      return;
    }
    let d;
    try {
      d = await api(url, opts);
    } catch (_) {
      wrap.innerHTML =
        '<p class="body text-muted text-center" style="padding:12px 8px;">Не удалось загрузить список.</p>';
      return;
    }
    if (!d || d.error) {
      wrap.innerHTML =
        '<p class="body text-muted text-center" style="padding:12px 8px;">Не удалось загрузить список.</p>';
      return;
    }
    const orders = (d.orders || []).filter((o) => o && o.delivery);
    if (!orders.length) {
      wrap.innerHTML =
        '<p class="body text-muted text-center" style="padding:12px 8px;">Пока нет готовых eSIM. Оформите тариф на вкладке «Главная».</p>';
      return;
    }
    wrap.innerHTML = '';
    orders.forEach((o) => {
      const del = o.delivery || {};
      const ac = del.activationCode || del.ac || '—';
      const smdp = del.smdpAddress || del.smdp || '—';
      const loc = o.locationCode ? 'Регион: ' + escapeHtml(String(o.locationCode)) + ' · ' : '';
      const meta = loc + escapeHtml(fmtEsimMineWhen(o.createdAt));
      const card = document.createElement('div');
      card.className = 'esim-mine-card';
      card.innerHTML =
        '<p class="esim-mine-card__meta">' +
        meta +
        '</p>' +
        esimMineQrHtml(del) +
        '<p class="caption text-muted" style="margin:8px 0 4px;">Код активации</p>' +
        '<div class="esim-code-row">' +
        '<code class="esim-ac">' +
        escapeHtml(String(ac)) +
        '</code>' +
        '<button type="button" class="btn-secondary esim-copy-btn esim-mine-copy-ac">Копировать</button></div>' +
        '<p class="caption text-muted" style="margin:12px 0 4px;">SMDP+ адрес</p>' +
        '<code class="esim-ac esim-ac--sm">' +
        escapeHtml(String(smdp)) +
        '</code>';
      wrap.appendChild(card);
      const copyBtn = card.querySelector('.esim-mine-copy-ac');
      if (copyBtn) {
        copyBtn.addEventListener('click', function () {
          copyText(String(ac), this);
        });
      }
    });
  }

  let checkEsimInterval = null;
  function stopEsimPaymentPolling() {
    if (checkEsimInterval) {
      clearInterval(checkEsimInterval);
      checkEsimInterval = null;
    }
  }
  function startEsimPaymentPolling() {
    if (checkEsimInterval) return;
    if (!localStorage.getItem('pending_esim_time')) {
      localStorage.setItem('pending_esim_time', Date.now().toString());
    }
    checkEsimInterval = setInterval(async () => {
      const pendingTime = localStorage.getItem('pending_esim_time');
      if (!pendingTime) {
        stopEsimPaymentPolling();
        return;
      }
      if (Date.now() - parseInt(pendingTime, 10) > 15 * 60 * 1000) {
        stopEsimPaymentPolling();
        localStorage.removeItem('pending_esim_time');
        return;
      }
      await tryFetchEsimDelivery();
    }, 3500);
  }

  async function tryFetchEsimDelivery() {
    let url = ESIM_API_PREFIX + '/esim/latest';
    const opts = { method: 'GET', silentError: true };
    if (IS_ANDROID) {
      opts.headers = { 'Authorization': 'Bearer ' + ANDROID_JWT };
    } else if (tg && tg.initData) {
      url += '?initData=' + encodeURIComponent(tg.initData);
    } else {
      return false;
    }
    const d = await api(url, opts);
    const delivery = d && d.delivery;
    const code = delivery && (delivery.activationCode || delivery.ac);
    if (!code) return false;
    localStorage.removeItem('pending_esim_time');
    stopEsimPaymentPolling();
    hideModal('modalEsimPay');
    showEsimDelivery(delivery);
    showSuccessOverlay('eSIM готов', 'QR и данные также отправлены вам в Telegram.');
    return true;
  }

  function renderEsimPM() {
    const w = document.getElementById('esimPmWrap');
    if (!w || !S.paymentMethods.length) return;
    if (!S.esimSelectedPM) S.esimSelectedPM = S.paymentMethods[0];
    w.innerHTML = '';
    S.paymentMethods.forEach((m) => {
      const sel = S.esimSelectedPM && S.esimSelectedPM.id === m.id;
      const el = document.createElement('div');
      el.className = 'pm-item' + (sel ? ' selected' : '');
      const n = m.name ? m.name.toLowerCase() : '';
      let svgIcon;
      if (m.id === 'stars' || n.includes('star')) {
        svgIcon = `<svg viewBox="0 0 24 24" class="icon-star"><path d="M12 2.3l2.4 7.4 7.6.6-5.8 4.7 1.8 7.3-6-4.3-6 4.3 1.8-7.3-5.8-4.7 7.6-.6z" stroke="currentColor" stroke-width="2" stroke-linejoin="round" class="star-shape"/><circle class="sparkle sp-1" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-2" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-3" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-4" cx="12" cy="12" r="1.5"/><circle class="sparkle sp-5" cx="12" cy="12" r="1.5"/></svg>`;
      } else if (m.id === 'cryptopay' || n.includes('crypto')) {
        svgIcon = `<svg viewBox="0 0 77 42" class="icon-crypto" style="width:32px;height:18px;display:block;">
          <path d="M2.72194715,0 L26.6266393,0 C28.4220313,0 30.0735687,0.988569903 30.9307924,2.57636085 L52.2150034,42 L23.7342299,42 C21.9388379,42 20.2873006,41.0114301 19.4300769,39.4236391 L0.330751009,4.04694928 C-0.386869546,2.71773798 0.101959028,1.05466888 1.42258019,0.332380469 C1.82138733,0.114260557 2.26806337,0 2.72194715,0 Z" fill="#25A3F2"/>
          <path d="M73.643684,0 C74.0975678,0 74.5442438,0.114260557 74.943051,0.332380469 C76.2236533,1.03278135 76.7221109,2.61780981 76.0968053,3.92522764 L76.0348801,4.04694928 L56.9355543,39.4236391 C56.1059829,40.960211 54.5325175,41.9355978 52.8046779,41.996927 L52.6314012,42 L24.5945392,42 L23.7342299,42 L45.4348388,2.57636085 C46.2644101,1.03978897 47.8378756,0.0644022425 49.5657151,0.00307299695 L49.7389918,0 L73.643684,0 Z" fill="#25A3F2" fill-opacity="0.85"/>
        </svg>`;
      } else if (m.id === 'yookassa' || n.includes('юkassa') || n.includes('юкасса') || n.includes('юк') || n.includes('yoo') || n.includes('yuk') || n.includes('карт') || n.includes('card')) {
        svgIcon = `<svg viewBox="0 0 24 24" class="icon-card"><rect x="2" y="5" width="20" height="14" rx="2" ry="2" fill="none" class="card-outline"></rect><line x1="2" y1="10" x2="22" y2="10" class="card-line"></line></svg>`;
      } else {
        svgIcon = escapeHtml(String(m.icon || '💳'));
      }
      el.innerHTML =
        `<span class="pm-icon flex-center">${svgIcon}</span>` +
        `<span class="pm-name">${escapeHtml(m.name || '')}</span>`;
      el.addEventListener('click', () => {
        S.esimSelectedPM = m;
        renderEsimPM();
        updateEsimPayTotal();
        haptic('light');
      });
      w.appendChild(el);
    });
    updateEsimPayTotal();
  }

  function updateEsimPayTotal() {
    const el = document.getElementById('esimTotalPrice');
    const btn = document.getElementById('btnPayEsim');
    if (!el || !btn) return;
    const pkg = S.esimSelectedPackage;
    if (!pkg) {
      el.textContent = '—';
      btn.disabled = true;
      return;
    }
    const k = pkg.salePriceKopecks != null ? Number(pkg.salePriceKopecks) : 0;
    const isStars = S.esimSelectedPM && S.esimSelectedPM.id === 'stars';
    const starIcon = `<svg viewBox="0 0 24 24" style="width:1em;height:1em;vertical-align:-0.15em;fill:var(--accent_text_color, #3aa8fc)"><path d="M12 2.3l2.4 7.4 7.6.6-5.8 4.7 1.8 7.3-6-4.3-6 4.3 1.8-7.3-5.8-4.7 7.6-.6z" stroke="var(--accent_text_color, #3aa8fc)" stroke-width="2" stroke-linejoin="round"/></svg>`;
    if (isStars) {
      const stars = Math.max(1, Math.ceil(k / 100));
      el.innerHTML = fmtPrice(stars) + ' ' + starIcon;
    } else {
      el.textContent = (k / 100).toFixed(k % 100 === 0 ? 0 : 2) + ' ₽';
    }
    btn.disabled = !S.esimSelectedPM;
  }

  async function handleEsimPay() {
    const pkg = S.esimSelectedPackage;
    const esimSel = document.getElementById('esimCountry');
    const loc = S.esimSelectedCountry || (esimSel && esimSel.value);
    if (!pkg || !loc || !S.esimSelectedPM) return;
    if (!IS_ANDROID && (!tg || !tg.initData)) {
      showToast('Оплата доступна только в Telegram');
      return;
    }
    const payBody = IS_ANDROID
      ? { locationCode: loc, packageCode: pkg.packageCode, paymentMethod: S.esimSelectedPM.id }
      : { initData: tg.initData, locationCode: loc, packageCode: pkg.packageCode, paymentMethod: S.esimSelectedPM.id };
    const payHeaders = IS_ANDROID
      ? { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + ANDROID_JWT }
      : { 'Content-Type': 'application/json' };
    const d = await api(ESIM_API_PREFIX + '/esim/payment/create', {
      method: 'POST',
      headers: payHeaders,
      body: JSON.stringify(payBody),
    });
    if (d && (d.paymentUrl || d.invoiceUrl)) {
      localStorage.setItem('pending_esim_time', Date.now().toString());
      startEsimPaymentPolling();
      const url = d.paymentUrl || d.invoiceUrl;
      const isNativeInvoice = url.includes('t.me/$') || url.includes('t.me/invoice');
      const isCryptoPay = url.includes('CryptoBot') || url.includes('CryptoTestnetBot');
      if (isNativeInvoice) {
        if (tg && tg.openInvoice) {
          tg.openInvoice(url, function (status) {
            if (status === 'paid') {
              setTimeout(function () { tryFetchEsimDelivery(); }, 1500);
            } else if (status === 'failed') {
              localStorage.removeItem('pending_esim_time');
              stopEsimPaymentPolling();
              showToast('Ошибка при оплате');
            }
          });
        } else {
          tg && tg.openLink ? tg.openLink(url) : window.open(url, '_blank');
        }
      } else if (isCryptoPay) {
        if (tg && tg.openLink) tg.openLink(url);
        else window.open(url, '_blank');
      } else {
        if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
        else window.open(url, '_blank');
      }
    } else {
      showToast('Ошибка создания платежа');
    }
  }

  /* ═══════ Load Data ═══════ */
  async function loadData() {
    let tariffUrl = '/miniapp/api/tariffs';
    if (tg && tg.initData) {
      tariffUrl += '?initData=' + encodeURIComponent(tg.initData);
    }

    const [tariffs, pm, servers] = await Promise.all([
      api(tariffUrl),
      api('/miniapp/api/payment-methods'),
      api('/miniapp/api/servers'),
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
        S.authViaWebJwt = !!(d.user.id != null && d.user.id < 0);
        S.user = d.user;
        S.subscription = d.subscription;
        S.referral = normalizeReferralFromApi(d.referral);
        renderUser();
        if (localStorage.getItem('pending_esim_time')) tryFetchEsimDelivery();
        if (!silent) showScreen(productHomeScreenId());
        const isActive = S.subscription && S.subscription.isActive;
        const newEnd = S.subscription && S.subscription.endDate;
        const pendingTime = localStorage.getItem('pending_payment_time');
        if (pendingTime && (Date.now() - parseInt(pendingTime)) < 20 * 60 * 1000) {
          if ((!wasActive && isActive) || (oldEnd && newEnd && oldEnd !== newEnd)) {
            localStorage.removeItem('pending_payment_time');
            stopPaymentPolling();
            showSuccessOverlay('Оплата успешна!', 'Ваша подписка активирована.\nДетальный чек отправлен вам в бот.');
            hideModal('modalPlan');
          }
        }
      }
      return;
    }
    if (!tg || !tg.initData) return;

    // Remember state before update
    const wasActive = S.subscription && S.subscription.isActive;
    const oldEnd = S.subscription && S.subscription.endDate;

    const d = await api('/miniapp/api/user', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: tg.initData }),
    });

    if (d && d.user) {
      S.authViaWebJwt = false;
      S.user = d.user;
      S.subscription = d.subscription;
      S.referral = normalizeReferralFromApi(d.referral);
      if (tg && tg.initData) {
        try {
          sessionStorage.setItem('svoy_tg_init_data', String(tg.initData));
        } catch (_) { }
      }
      renderUser();
      if (localStorage.getItem('pending_esim_time')) tryFetchEsimDelivery();
      if (!silent) showScreen(productHomeScreenId());

      const isActive = S.subscription && S.subscription.isActive;
      const newEnd = S.subscription && S.subscription.endDate;

      // Check for payment success if we were waiting for one
      const pendingTime = localStorage.getItem('pending_payment_time');
      if (pendingTime && (Date.now() - parseInt(pendingTime)) < 20 * 60 * 1000) {
        // If sub changed from inactive to active OR end date shifted forward
        if ((!wasActive && isActive) || (oldEnd && newEnd && oldEnd !== newEnd)) {
          localStorage.removeItem('pending_payment_time');
          stopPaymentPolling();
          showSuccessOverlay('Оплата успешна!', 'Ваша подписка активирована.\nДетальный чек отправлен вам в бот.');
          hideModal('modalPlan');
        }
      }
    }
  }

  function renderTrafficPmList() {
    const w = document.getElementById('trafficPmWrap');
    if (!w) return;
    w.innerHTML = '';
    if (!S.paymentMethods.length) return;
    S.paymentMethods.forEach((m) => {
      const sel = S.trafficPackPM && S.trafficPackPM.id === m.id;
      const el = document.createElement('div');
      el.className = 'pm-item' + (sel ? ' selected' : '');
      el.innerHTML =
        '<span class="pm-icon flex-center">' + escapeHtml(String(m.icon || '💳')) + '</span>' +
        '<span class="pm-name">' + escapeHtml(m.name || m.id) + '</span>';
      el.addEventListener('click', () => {
        S.trafficPackPM = m;
        renderTrafficPmList();
        updateTrafficPackTotal();
        haptic('light');
      });
      w.appendChild(el);
    });
  }

  function renderTrafficPacksGrid() {
    const w = document.getElementById('trafficPacksWrap');
    if (!w) return;
    w.innerHTML = '';
    if (!S.trafficPacks.length) {
      w.innerHTML = '<p class="body text-muted" style="padding:8px;">Пакеты пока не настроены.</p>';
      return;
    }
    S.trafficPacks.forEach((p) => {
      const sel = S.selectedTrafficPack && S.selectedTrafficPack.id === p.id;
      const el = document.createElement('div');
      el.className = 'tariff-card' + (sel ? ' selected' : '');
      el.innerHTML =
        '<p class="months">+' + escapeHtml(String(p.gbAmount)) + ' ГБ</p>' +
        '<p class="price" style="margin-top:6px;">' + escapeHtml(p.title || '') + '</p>' +
        '<p class="per-month">' + fmtPrice(p.price) + ' ₽ · ' + fmtPrice(p.priceStars) + ' ⭐</p>';
      el.addEventListener('click', () => {
        S.selectedTrafficPack = p;
        renderTrafficPacksGrid();
        updateTrafficPackTotal();
        haptic('light');
      });
      w.appendChild(el);
    });
  }

  function updateTrafficPackTotal() {
    const el = document.getElementById('trafficPackTotal');
    const btn = document.getElementById('btnPayTrafficPack');
    if (!S.selectedTrafficPack || !S.trafficPackPM) {
      if (el) el.textContent = '—';
      if (btn) btn.disabled = true;
      return;
    }
    const isStars = S.trafficPackPM.id === 'stars';
    const starIcon = `<svg viewBox="0 0 24 24" style="width:1em;height:1em;vertical-align:-0.15em;fill:var(--accent_text_color, #3aa8fc)"><path d="M12 2.3l2.4 7.4 7.6.6-5.8 4.7 1.8 7.3-6-4.3-6 4.3 1.8-7.3-5.8-4.7 7.6-.6z" stroke="var(--accent_text_color, #3aa8fc)" stroke-width="2" stroke-linejoin="round"/></svg>`;
    const price = isStars ? S.selectedTrafficPack.priceStars : S.selectedTrafficPack.price;
    el.innerHTML = fmtPrice(price) + ' ' + (isStars ? starIcon : '₽');
    btn.disabled = false;
  }

  async function openTrafficPackModal() {
    if (!S.subscription || !S.subscription.isActive) {
      showToast('Нужна активная подписка');
      return;
    }
    if (!IS_ANDROID && (!tg || !tg.initData)) {
      showToast('Откройте мини-приложение из Telegram');
      return;
    }
    const packs = await api('/miniapp/api/traffic/packs', { silentError: true });
    S.trafficPacks = Array.isArray(packs) ? packs : [];
    S.selectedTrafficPack = S.trafficPacks[0] || null;
    S.trafficPackPM = S.selectedPM || S.paymentMethods[0] || null;
    renderTrafficPacksGrid();
    renderTrafficPmList();
    updateTrafficPackTotal();
    showModal('modalTrafficPack');
  }
  window.openTrafficPackModal = openTrafficPackModal;

  async function handleTrafficPackPay() {
    if (!S.selectedTrafficPack || !S.trafficPackPM) return;
    if (!IS_ANDROID && (!tg || !tg.initData)) {
      showToast('Оплата доступна только в Telegram');
      return;
    }
    const payBody = IS_ANDROID
      ? { packId: S.selectedTrafficPack.id, paymentMethod: S.trafficPackPM.id }
      : { initData: tg.initData, packId: S.selectedTrafficPack.id, paymentMethod: S.trafficPackPM.id };
    const payHeaders = IS_ANDROID
      ? { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + ANDROID_JWT }
      : { 'Content-Type': 'application/json' };
    const d = await api('/miniapp/api/traffic/payment/create', {
      method: 'POST',
      headers: payHeaders,
      body: JSON.stringify(payBody),
    });
    if (d && (d.paymentUrl || d.invoiceUrl)) {
      localStorage.setItem('pending_payment_time', Date.now().toString());
      startPaymentPolling();
      const url = d.paymentUrl || d.invoiceUrl;
      const isNativeInvoice = url.includes('t.me/$') || url.includes('t.me/invoice');
      const isCryptoPay = url.includes('CryptoBot') || url.includes('CryptoTestnetBot');
      if (isNativeInvoice) {
        if (tg && tg.openInvoice) {
          tg.openInvoice(url, function (status) {
            if (status === 'paid') {
              localStorage.removeItem('pending_payment_time');
              stopPaymentPolling();
              showSuccessOverlay('Готово!', 'Пакет трафика начислен. Обновите подписку в приложении VPN.');
              hideModal('modalTrafficPack');
              loadUser(true);
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
        if (tg && tg.openLink) tg.openLink(url);
        else window.open(url, '_blank');
      } else {
        if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
        else window.open(url, '_blank');
      }
    } else {
      if (d && d.error === 'subscription_required') {
        showToast('Нужна активная подписка');
      } else {
        showToast('Ошибка создания платежа');
      }
    }
  }

  /* ═══════ Payment ═══════ */
  async function handlePay() {
    if (!S.selectedTariff || !S.selectedPM) return;
    if (!IS_ANDROID && (!tg || !tg.initData)) {
      showToast('Оплата доступна только в Telegram');
      return;
    }
    const payBody = IS_ANDROID
      ? { tariffId: S.selectedTariff.id, paymentMethod: S.selectedPM.id }
      : { initData: tg.initData, tariffId: S.selectedTariff.id, paymentMethod: S.selectedPM.id };
    const payHeaders = IS_ANDROID
      ? { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + ANDROID_JWT }
      : { 'Content-Type': 'application/json' };
    const d = await api('/miniapp/api/payment/create', {
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
              showSuccessOverlay('Оплата успешна!', 'Ваша подписка активирована.\nДетальный чек отправлен вам в бот.');
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
      const titleEl = ov.querySelector('.title-s');
      const bodyEl = ov.querySelector('.body');
      if (title && titleEl) titleEl.textContent = title;
      if (sub && bodyEl) {
        bodyEl.style.whiteSpace = 'pre-line';
        bodyEl.textContent = sub;
      }
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
          if (localStorage.getItem('pending_esim_time')) tryFetchEsimDelivery();
        }
      });
    }

    // Apply theme after tg.ready() so themeParams is populated
    applyTheme();
    // Re-apply after a tick in case Telegram populates themeParams async
    setTimeout(applyTheme, 150);

    if (!URL_THEME) {
      try {
        const mq = window.matchMedia('(prefers-color-scheme: dark)');
        const onSysTheme = () => {
          if (!isTelegramMiniApp()) applyTheme();
        };
        if (mq.addEventListener) mq.addEventListener('change', onSysTheme);
        else if (mq.addListener) mq.addListener(onSysTheme);
      } catch (_) { }
    }

    // Load SVG sprite
    loadSprite();

    // If there was a pending payment from previous session, resume polling
    if (localStorage.getItem('pending_payment_time')) {
      startPaymentPolling();
    }
    if (localStorage.getItem('pending_esim_time')) {
      startEsimPaymentPolling();
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

    try {
      localStorage.setItem(PRODUCT_KEY, 'vpn');
    } catch (_) {}

    applyProductSwitchUI();

    maybeShowServersRelocatedHint();

    let _serversResizeTimer;
    window.addEventListener('resize', function () {
      clearTimeout(_serversResizeTimer);
      _serversResizeTimer = setTimeout(function () {
        renderServers();
        startPingRefresh();
        maybeShowServersRelocatedHint();
      }, 280);
    });

    // Tab bar navigation
    document.querySelectorAll('.tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (btn.dataset.homeTab === '1') {
          showScreen(productHomeScreenId());
        } else if (btn.dataset.screen) {
          showScreen(btn.dataset.screen);
        }
        if (btn.dataset.screen === 'screenReferral') loadReferral();
      });
    });

    document.querySelectorAll('.esim-beta-email-input').forEach((inp) => {
      inp.addEventListener('input', function () {
        const v = this.value;
        document.querySelectorAll('.esim-beta-email-input').forEach((o) => {
          if (o !== this) o.value = v;
        });
      });
    });

    async function submitEsimBetaNotifyRequest(btnEl) {
      const first = document.querySelector('.esim-beta-email-input');
      const email = String((first && first.value) || '').trim();
      if (!email) {
        showToast('Введите email');
        return;
      }
      const body = { email };
      const opts = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '',
        silentError: true,
      };
      if (IS_ANDROID) {
        opts.headers.Authorization = 'Bearer ' + ANDROID_JWT;
      } else if (tg && tg.initData) {
        body.initData = tg.initData;
      } else {
        showToast('Откройте мини-приложение в Telegram');
        return;
      }
      opts.body = JSON.stringify(body);
      if (btnEl) btnEl.disabled = true;
      try {
        const d = await api(ESIM_API_PREFIX + '/esim/beta-notify', opts);
        if (d && d.status === 'ok') {
          showToast(d.message || 'Заявка отправлена.', 4500);
        } else {
          showToast((d && d.error) || 'Не удалось отправить');
        }
      } catch (_) {
        showToast('Ошибка сети');
      } finally {
        if (btnEl) btnEl.disabled = false;
      }
    }

    document.querySelector('.app').addEventListener('click', (ev) => {
      if (ev.target.closest('.esim-beta-gate__link-email')) {
        window.showModal('modalLinkEmail');
        return;
      }
      const nb = ev.target.closest('.esim-beta-notify-btn');
      if (nb) {
        submitEsimBetaNotifyRequest(nb);
        return;
      }
      const b = ev.target.closest('.product-switch__btn');
      if (!b || !b.dataset.product) return;
      const prod = b.dataset.product;
      setProduct(prod, true);
      if (prod === 'vpn') hideEsimResult();
    });

    const esimCountry = document.getElementById('esimCountry');
    if (esimCountry) {
      esimCountry.addEventListener('change', () => {
        const v = esimCountry.value;
        S.esimSelectedCountry = v;
        hideEsimResult();
        if (userIsEsimAdmin()) loadEsimPackages(v);
      });
    }

    const btnEsimBuy = document.getElementById('btnEsimBuy');
    if (btnEsimBuy) {
      btnEsimBuy.addEventListener('click', () => {
        if (!S.esimSelectedPackage) return;
        if (!IS_ANDROID && (!tg || !tg.initData)) {
          showToast('Оплата доступна только в Telegram');
          return;
        }
        if (!S.paymentMethods.length) {
          showToast('Подождите загрузки способов оплаты и повторите');
          return;
        }
        renderEsimPM();
        updateEsimPayTotal();
        window.showModal('modalEsimPay');
      });
    }

    const btnEsimCopyAc = document.getElementById('btnEsimCopyAc');
    if (btnEsimCopyAc) {
      btnEsimCopyAc.addEventListener('click', () => {
        const el = document.getElementById('esimAcCode');
        copyText(el && el.textContent, btnEsimCopyAc);
      });
    }

    function addClick(id, handler) {
      const el = document.getElementById(id);
      if (el) el.addEventListener('click', handler);
      else console.warn('Missing element for click:', id);
    }

    addClick('btnChoosePlan', () => window.showModal('modalPlan'));
    addClick('btnPayEsim', handleEsimPay);

    // btnReferral is handled via tab bar now

    let refLink = '';

    function applyReferralPayload(d) {
      const n = normalizeReferralFromApi(d);
      if (!n) return false;
      S.referral = n;
      refLink = n.refLink;
      d = n;
      const refL = document.getElementById('refLinkText');
      if (refL) refL.textContent = d.refLink;
      const refC = document.getElementById('refCount');
      if (refC) refC.textContent = d.referralCount + ' чел.';
      const refB = document.getElementById('refBonus');
      if (refB) refB.textContent = d.inviterBonusDays + ' дн. за друга';
      const refDEl = document.getElementById('refDesc');
      if (refDEl) {
        refDEl.textContent =
          `Дарим ${d.inviterBonusDays} дней Вам и ${d.invitedBonusDays} дня другу за каждое успешное приглашение.`;
      }
      return true;
    }

    async function loadReferral() {
      const refDesc = document.getElementById('refDesc');
      if (applyReferralPayload(S.referral)) return;
      await loadUser(true);
      if (applyReferralPayload(S.referral)) return;
      if (refDesc) {
        refDesc.textContent =
          'Подарки доступны после входа. Потяните экран вниз для обновления или откройте приложение из бота ещё раз.';
      }
    }

    addClick('btnCopyRef', function () { copyText(refLink, this); });
    addClick('btnShareRef', function () {
      if (!refLink) return;
      const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(refLink)}&text=${encodeURIComponent('Попробуй этот отличный VPN! Дают бонусные дни при регистрации по ссылке 🎁')}`;
      tg && tg.openTelegramLink ? tg.openTelegramLink(shareUrl) : window.open(shareUrl, '_blank');
    });

    // Copy buttons
    addClick('btnCopySetup', function () {
      const el = document.getElementById('subUrlSetup');
      if (el) copyText(el.value, this);
    });

    // Pay
    addClick('btnPay', handlePay);
    addClick('btnPayTrafficPack', handleTrafficPackPay);

    // Links
    addClick('btnChannel', () => {
      const channel = 'https://t.me/SvoyVPN_channel';
      tg && tg.openTelegramLink
        ? tg.openTelegramLink(channel)
        : window.open(channel, '_blank');
    });
    addClick('btnSupport', () => {
      const raw = S.user && S.user.supportLink;
      const link = isSafeHttpUrl(raw) ? raw : 'https://t.me/SvoyVPN_support';
      tg && tg.openTelegramLink
        ? tg.openTelegramLink(link)
        : window.open(link, '_blank');
    });

    /* ── Mobile App Logout ── */
    const lgBtn = document.getElementById('btnAndroidLogout');
    if (lgBtn && IS_MOBILE_APP) {
      lgBtn.style.display = 'flex';
      lgBtn.onclick = () => {
        haptic('medium');
        if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.iOSBridge) {
          window.webkit.messageHandlers.iOSBridge.postMessage({ action: 'logout' });
        } else if (window.AndroidBridge && window.AndroidBridge.logout) {
          window.AndroidBridge.logout();
        } else if (window.__androidLogout) {
          window.__androidLogout();
        }
      };
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
      const r = await fetch('/miniapp/api/auth/link-email/send', {
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
      showToast('Код в Telegram и на почте');
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
        const r = await fetch('/miniapp/api/auth/link-email/confirm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ initData: tg.initData, email, otp }),
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
        { id: 'happ', name: 'Happ', iconImg: '/miniapp/images/happ.png', storeUrl: 'https://apps.apple.com/kz/app/happ-proxy-utility/id6504287215' },
        { id: 'hiddify', name: 'Hiddify', iconImg: '/miniapp/images/hiddify.png', storeUrl: 'https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532' },
        { id: 'v2raytun', name: 'V2RayTun', iconImg: '/miniapp/images/v2raytun.png', storeUrl: 'https://apps.apple.com/app/v2raytun/id6476628951' }
      ],
      android: [
        { id: 'happ', name: 'Happ', iconImg: '/miniapp/images/happ.png', storeUrl: 'https://play.google.com/store/apps/details?id=com.happproxy' },
        { id: 'hiddify', name: 'Hiddify', iconImg: '/miniapp/images/hiddify.png', storeUrl: 'https://play.google.com/store/apps/details?id=app.hiddify.com' },
        { id: 'v2raytun', name: 'V2RayTun', iconImg: '/miniapp/images/v2raytun.png', storeUrl: 'https://play.google.com/store/apps/details?id=com.v2raytun.android' }
      ],
      windows: [
        { id: 'happ', name: 'Happ', iconImg: '/miniapp/images/happ.png', storeUrl: 'https://github.com/Happ-proxy/happ-desktop/releases/download/2.4.0/setup-Happ.x64.exe' },
        { id: 'hiddify', name: 'Hiddify', iconImg: '/miniapp/images/hiddify.png', storeUrl: 'https://github.com/hiddify/hiddify-app/releases' },
        { id: 'v2rayn', name: 'V2RayN', iconImg: '/miniapp/images/v2raytun.png', storeUrl: 'https://github.com/2dust/v2rayN/releases' }
      ],
      mac: [
        { id: 'happ', name: 'Happ', iconImg: '/miniapp/images/happ.png', storeUrl: 'https://apps.apple.com/kz/app/happ-proxy-utility/id6504287215' },
        { id: 'hiddify', name: 'Hiddify', iconImg: '/miniapp/images/hiddify.png', storeUrl: 'https://github.com/hiddify/hiddify-app/releases' },
        { id: 'v2raytun', name: 'V2RayTun', iconImg: '/miniapp/images/v2raytun.png', storeUrl: 'https://apps.apple.com/app/v2raytun/id6476628951' }
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
        showScreen(productHomeScreenId());
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

