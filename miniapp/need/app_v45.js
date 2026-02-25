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

    /* Hardcoded colors matching design system */
    const bgColor = scheme === 'dark' ? '#18222d' : '#ffffff';
    const secBgColor = scheme === 'dark' ? '#21303f' : '#f7f9fb';

    /* Update meta theme-color for WebView header */
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', bgColor);

    /* Telegram WebApp API */
    if (tg) {
      try { tg.setHeaderColor && tg.setHeaderColor(bgColor); } catch (_) { }
      try { tg.setBackgroundColor && tg.setBackgroundColor(bgColor); } catch (_) { }
      try { tg.setBottomBarColor && tg.setBottomBarColor(secBgColor); } catch (_) { }
    }
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

    // Hide tab bar on sub-screens (plan, referral)
    const bar = document.getElementById('tabBar');
    if (bar) {
      if (id === 'screenPlan' || id === 'screenReferral') {
        bar.style.display = 'none';
      } else {
        bar.style.display = '';
      }
    }

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

      let badgeHtml = '';
      if (t.isRenew) {
        badgeHtml = '<span class="badge" style="background:var(--accent_text_color);color:#fff;">Скидка</span>';
      } else if (t.popular) {
        badgeHtml = '<span class="badge">Хит</span>';
      }

      el.innerHTML = badgeHtml +
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

  const SERVERS_PER_PAGE = 4;
  let serverPage = 0;

  function createServerCard(s) {
    const el = document.createElement('div');
    el.className = 'server-card';
    el.setAttribute('data-server-id', s.id);
    el.innerHTML =
      '<div class="server-card__header">' +
      '<span class="server-card__flag">' + getFlag(s.name) + '</span>' +
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
    if (!S.servers.length) {
      w.innerHTML = '<div class="server-card server-card--loading text-muted body">Нет серверов</div>';
      // Remove old nav if exists
      var oldNav = document.getElementById('serverNav');
      if (oldNav) oldNav.remove();
      return;
    }

    var totalPages = Math.ceil(S.servers.length / SERVERS_PER_PAGE);
    if (serverPage >= totalPages) serverPage = totalPages - 1;
    if (serverPage < 0) serverPage = 0;

    var start = serverPage * SERVERS_PER_PAGE;
    var pageServers = S.servers.slice(start, start + SERVERS_PER_PAGE);

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
      var start = serverPage * SERVERS_PER_PAGE;
      var pageServers = S.servers.slice(start, start + SERVERS_PER_PAGE);
      document.querySelectorAll('.server-card[data-server-id]').forEach(function (card, i) {
        if (i >= pageServers.length) return;
        var pingWrap = card.querySelector('.server-card__ping-wrap');
        if (pingWrap) {
          measurePing(pageServers[i].id).then(function (ms) { renderPingBadge(pingWrap, ms); });
        }
      });
    }, 60000);
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

      let daysLeft = 0;
      if (sub.endDate) {
        const end = new Date(sub.endDate);
        const now = new Date();
        const diff = end.getTime() - now.getTime();
        daysLeft = Math.ceil(diff / (1000 * 3600 * 24));
      }

      if (daysLeft > 0) {
        until.textContent = 'Осталось ' + daysLeft + ' дн.';
      } else {
        until.textContent = fmtDate(sub.endDate);
      }

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
      startPingRefresh();
    } else {
      renderServers(); // show "Нет серверов"
    }
  }

  async function loadUser() {
    if (!tg || !tg.initData) return;
    const d = await api('/miniapp/api/user', {
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
    const d = await api('/miniapp/api/payment/create', {
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
    // Telegram WebApp — init FIRST so themeParams is ready
    if (tg) {
      tg.ready();
      tg.expand();
      // Subscribe to future theme changes
      tg.onEvent && tg.onEvent('themeChanged', applyTheme);
    }

    // Apply theme after tg.ready() so themeParams is populated
    applyTheme();
    // Re-apply after a tick in case Telegram populates themeParams async
    setTimeout(applyTheme, 150);

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

    // Referral
    const btnReferral = document.getElementById('btnReferral');
    if (btnReferral) {
      btnReferral.addEventListener('click', () => {
        showScreen('screenReferral');
        loadReferral();
      });
    }
    const btnReferralBack = document.getElementById('btnReferralBack');
    if (btnReferralBack) {
      btnReferralBack.addEventListener('click', () => showScreen('screenProfile'));
    }

    let refLink = '';
    async function loadReferral() {
      if (!tg || !tg.initData) return;
      const d = await api('/miniapp/api/referral?initData=' + encodeURIComponent(tg.initData));
      if (d && d.referralCode) {
        refLink = d.refLink;
        document.getElementById('refLinkText').textContent = d.refLink;
        document.getElementById('refCount').textContent = d.referralCount + ' чел.';
        document.getElementById('refBonus').textContent = d.inviterBonusDays + ' дн. за друга';
        document.getElementById('refDesc').textContent =
          `Дарим ${d.inviterBonusDays} дней Вам и ${d.invitedBonusDays} дня другу за каждое успешное приглашение.`;
      }
    }

    document.getElementById('btnCopyRef').addEventListener('click', function () {
      copyText(refLink, this);
    });
    document.getElementById('btnShareRef').addEventListener('click', function () {
      if (!refLink) return;
      const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(refLink)}&text=${encodeURIComponent('Попробуй этот отличный VPN! Дают бонусные дни при регистрации по ссылке 🎁')}`;
      tg && tg.openTelegramLink ? tg.openTelegramLink(shareUrl) : window.open(shareUrl, '_blank');
    });

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
