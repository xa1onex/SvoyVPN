/* ═══════════════════════════════════════════
   SvoyVPN Miniapp — App Logic
   ═══════════════════════════════════════════ */
(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  /* ── State ── */
  const S = {
    user: null,
    subscription: null,
    tariffs: [],
    paymentMethods: [],
    servers: [],
    selectedTariff: null,
    selectedPM: null,
  };

  /* ═══════ Helpers ═══════ */
  const MW = ['месяц', 'месяца', 'месяцев'];
  function mw(n) {
    if (n % 10 === 1 && n % 100 !== 11) return MW[0];
    if ([2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100)) return MW[1];
    return MW[2];
  }

  const fmtPrice = (p) =>
    Number(p).toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 0 });

  function fmtDate(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
  }

  const FLAGS = {
    us: '🇺🇸', usa: '🇺🇸', сша: '🇺🇸', 'united states': '🇺🇸',
    de: '🇩🇪', germany: '🇩🇪', германия: '🇩🇪',
    nl: '🇳🇱', netherlands: '🇳🇱', нидерланды: '🇳🇱', голландия: '🇳🇱',
    fi: '🇫🇮', finland: '🇫🇮', финляндия: '🇫🇮',
    ru: '🇷🇺', russia: '🇷🇺', россия: '🇷🇺',
    sg: '🇸🇬', singapore: '🇸🇬', сингапур: '🇸🇬',
    gb: '🇬🇧', uk: '🇬🇧', великобритания: '🇬🇧',
    fr: '🇫🇷', france: '🇫🇷', франция: '🇫🇷',
    jp: '🇯🇵', japan: '🇯🇵', япония: '🇯🇵',
    ca: '🇨🇦', canada: '🇨🇦', канада: '🇨🇦',
    kz: '🇰🇿', kazakhstan: '🇰🇿', казахстан: '🇰🇿',
    tr: '🇹🇷', turkey: '🇹🇷', турция: '🇹🇷',
    ae: '🇦🇪', uae: '🇦🇪', оаэ: '🇦🇪',
    in: '🇮🇳', india: '🇮🇳', индия: '🇮🇳',
  };

  function getFlag(name) {
    const n = (name || '').toLowerCase();
    for (const [k, v] of Object.entries(FLAGS)) if (n.includes(k)) return v;
    return '🌍';
  }

  function haptic(style) {
    try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(style); } catch (_) { }
  }

  /* ═══════ Theme ═══════ */
  function applyTheme() {
    const scheme = (tg && tg.colorScheme) ||
      (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', scheme);
    document.body.setAttribute('data-theme', scheme);
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

  /* ═══════ Navigation ═══════ */
  function showScreen(id) {
    document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));

    const screen = document.getElementById(id);
    if (screen) screen.classList.add('active');

    // Tab highlight — plan screen maps to VPN tab
    const tabId = id === 'screenPlan' ? 'screenVpn' : id;
    const tab = document.querySelector(`.tab[data-screen="${tabId}"]`);
    if (tab) tab.classList.add('active');

    // Hide tab bar on plan screen
    const bar = document.getElementById('tabBar');
    if (bar) bar.style.display = id === 'screenPlan' ? 'none' : '';

    haptic('light');
  }

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
      const orig = btnEl.textContent;
      btnEl.textContent = '✓';
      setTimeout(() => { btnEl.textContent = orig; }, 1500);
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
    S.tariffs.forEach((t) => {
      const sel = S.selectedTariff && S.selectedTariff.id === t.id;
      const el = document.createElement('div');
      el.className = 'tariff-card' + (sel ? ' selected' : '');
      el.innerHTML =
        (t.popular ? '<span class="badge">Хит</span>' : '') +
        `<p class="months">${t.months} ${mw(t.months)}</p>` +
        `<p class="price">${fmtPrice(t.price)} ₽</p>` +
        (t.oldPrice ? `<p class="old-price">${fmtPrice(t.oldPrice)} ₽</p>` : '') +
        `<p class="per-month">${fmtPrice(t.pricePerMonth)} ₽/мес</p>`;
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
      el.innerHTML =
        `<span class="pm-icon">${m.icon || '💳'}</span>` +
        `<span class="pm-name">${m.name}</span>`;
      el.addEventListener('click', () => {
        S.selectedPM = m;
        renderPM();
        updateTotal();
        haptic('light');
      });
      w.appendChild(el);
    });
  }

  /* ═══════ Render: Servers ═══════ */
  function renderServers() {
    const w = document.getElementById('serversWrap');
    if (!w) return;
    if (!S.servers.length) {
      w.innerHTML = '<div class="server-chip text-muted body">Нет серверов</div>';
      return;
    }
    w.innerHTML = '';
    S.servers.forEach((s) => {
      const el = document.createElement('div');
      el.className = 'server-chip';
      el.innerHTML = `<span class="flag">${getFlag(s.name)}</span>${s.name}`;
      w.appendChild(el);
    });
  }

  /* ═══════ Render: Total ═══════ */
  function updateTotal() {
    const el = document.getElementById('totalPrice');
    const btn = document.getElementById('btnPay');
    if (S.selectedTariff) {
      el.textContent = fmtPrice(S.selectedTariff.price) + ' ₽';
      btn.disabled = !S.selectedPM;
    } else {
      el.textContent = '—';
      btn.disabled = true;
    }
  }

  /* ═══════ Render: User & Subscription ═══════ */
  function renderUser() {
    const avatar = document.getElementById('avatar');
    if (S.user) {
      const name = [S.user.firstName, S.user.lastName].filter(Boolean).join(' ') || 'U';
      document.getElementById('profileName').textContent = name;

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
    const badge = document.getElementById('subBadge');
    const until = document.getElementById('subUntil');
    const vpnStatus = document.getElementById('vpnStatus');
    const pStatus = document.getElementById('profileStatus');

    if (sub && sub.isActive) {
      badge.textContent = 'Активна';
      badge.classList.remove('text-danger');
      badge.classList.add('text-accent');
      until.textContent = fmtDate(sub.endDate);
      vpnStatus.textContent = 'Подписка активна';
      pStatus.textContent = 'Подписка активна';
      pStatus.classList.remove('text-muted');
      pStatus.classList.add('text-accent');
    } else {
      badge.textContent = 'Неактивна';
      badge.classList.remove('text-accent');
      badge.classList.add('text-danger');
      vpnStatus.textContent = 'Быстрый и приватный VPN';
      pStatus.textContent = 'Подписка неактивна';
    }

    // Subscription URL
    if (sub && sub.subscriptionUrl) {
      document.getElementById('subUrlSetup').value = sub.subscriptionUrl;
      document.getElementById('subUrlProfile').value = sub.subscriptionUrl;
    }
  }

  /* ═══════ Load Data ═══════ */
  async function loadData() {
    const [tariffs, pm, servers] = await Promise.all([
      api('/api/tariffs'),
      api('/api/payment-methods'),
      api('/api/servers'),
    ]);

    if (Array.isArray(tariffs) && tariffs.length) {
      S.tariffs = tariffs;
      S.selectedTariff = tariffs[0];
      renderTariffs();
      updateTotal();
    }
    if (Array.isArray(pm) && pm.length) {
      S.paymentMethods = pm;
      S.selectedPM = pm[0];
      renderPM();
      updateTotal();
    }
    if (Array.isArray(servers)) {
      S.servers = servers;
      renderServers();
    } else {
      renderServers(); // show "Нет серверов"
    }
  }

  async function loadUser() {
    if (!tg || !tg.initData) return;
    const d = await api('/api/user', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: tg.initData }),
    });
    if (d && d.user) {
      S.user = d.user;
      S.subscription = d.subscription;
      renderUser();
    }
  }

  /* ═══════ Payment ═══════ */
  async function handlePay() {
    if (!S.selectedTariff || !S.selectedPM) return;
    if (!tg || !tg.initData) {
      showToast('Оплата доступна только в Telegram');
      return;
    }
    const d = await api('/api/payment/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        initData: tg.initData,
        tariffId: S.selectedTariff.id,
        paymentMethod: S.selectedPM.id,
        deviceCount: 1,
      }),
    });
    if (d && (d.paymentUrl || d.invoiceUrl)) {
      const url = d.paymentUrl || d.invoiceUrl;
      tg.openLink ? tg.openLink(url) : window.open(url, '_blank');
    } else {
      showToast('Ошибка создания платежа');
    }
  }

  /* ═══════ Init ═══════ */
  document.addEventListener('DOMContentLoaded', () => {
    // Telegram WebApp
    // Apply theme BEFORE anything else
    applyTheme();

    if (tg) {
      tg.ready();
      tg.expand();
      tg.onEvent && tg.onEvent('themeChanged', applyTheme);
      tg.setHeaderColor && tg.setHeaderColor('bg_color');
      tg.setBackgroundColor && tg.setBackgroundColor('bg_color');
    }

    // Load SVG sprite
    loadSprite();

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
      });
    });

    // Choose plan → plan screen
    document.getElementById('btnChoosePlan').addEventListener('click', () => showScreen('screenPlan'));
    document.getElementById('btnBack').addEventListener('click', () => showScreen('screenVpn'));

    // Copy buttons
    document.getElementById('btnCopySetup').addEventListener('click', function () {
      copyText(document.getElementById('subUrlSetup').value, this);
    });
    document.getElementById('btnCopyProfile').addEventListener('click', function () {
      copyText(document.getElementById('subUrlProfile').value, this);
    });

    // Pay
    document.getElementById('btnPay').addEventListener('click', handlePay);

    // Links
    document.getElementById('btnChannel').addEventListener('click', () => {
      tg && tg.openTelegramLink
        ? tg.openTelegramLink('https://t.me/SvoyVPN')
        : window.open('https://t.me/SvoyVPN', '_blank');
    });
    document.getElementById('btnSupport').addEventListener('click', () => {
      tg && tg.openTelegramLink
        ? tg.openTelegramLink('https://t.me/SvoyVPN_support')
        : window.open('https://t.me/SvoyVPN_support', '_blank');
    });

    // Load data
    loadData();
    loadUser();
  });
})();
