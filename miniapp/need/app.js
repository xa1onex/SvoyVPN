/* SvoyVPN miniapp – screen-based navigation with bottom tab bar.
   Uses Need CSS framework + SvoyVPN bot API (/api/*). */
(function () {
  /** @type {any} */
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  const state = {
    user: null,
    subscription: null,
    tariffs: [],
    paymentMethods: [],
    servers: [],
    selectedTariff: null,
    selectedPaymentMethod: null,
    currentScreen: 'screenVpn',
  };

  /* ── helpers ── */
  const MONTH_NAMES = ['месяц', 'месяца', 'месяцев'];
  function monthWord(n) {
    if (n % 10 === 1 && n % 100 !== 11) return MONTH_NAMES[0];
    if ([2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100)) return MONTH_NAMES[1];
    return MONTH_NAMES[2];
  }

  function fmtPrice(p) {
    return Number(p).toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
  }

  function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
  }

  /* Known server names → emoji flag map */
  const FLAG_MAP = {
    'us': '🇺🇸', 'usa': '🇺🇸', 'сша': '🇺🇸', 'united states': '🇺🇸', 'америка': '🇺🇸',
    'de': '🇩🇪', 'germany': '🇩🇪', 'германия': '🇩🇪', 'deutschland': '🇩🇪',
    'nl': '🇳🇱', 'netherlands': '🇳🇱', 'нидерланды': '🇳🇱', 'голландия': '🇳🇱',
    'fi': '🇫🇮', 'finland': '🇫🇮', 'финляндия': '🇫🇮',
    'ru': '🇷🇺', 'russia': '🇷🇺', 'россия': '🇷🇺',
    'sg': '🇸🇬', 'singapore': '🇸🇬', 'сингапур': '🇸🇬',
    'gb': '🇬🇧', 'uk': '🇬🇧', 'англия': '🇬🇧', 'великобритания': '🇬🇧',
    'fr': '🇫🇷', 'france': '🇫🇷', 'франция': '🇫🇷',
    'jp': '🇯🇵', 'japan': '🇯🇵', 'япония': '🇯🇵',
    'ca': '🇨🇦', 'canada': '🇨🇦', 'канада': '🇨🇦',
    'kz': '🇰🇿', 'kazakhstan': '🇰🇿', 'казахстан': '🇰🇿',
    'tr': '🇹🇷', 'turkey': '🇹🇷', 'турция': '🇹🇷', 'türkiye': '🇹🇷',
    'ae': '🇦🇪', 'uae': '🇦🇪', 'оаэ': '🇦🇪',
    'in': '🇮🇳', 'india': '🇮🇳', 'индия': '🇮🇳',
  };

  function serverFlag(name) {
    const n = (name || '').toLowerCase().trim();
    for (const [k, v] of Object.entries(FLAG_MAP)) {
      if (n.includes(k)) return v;
    }
    return '🌍';
  }

  /* ── theme ── */
  function applyTheme() {
    const tgTheme = tg && tg.colorScheme;
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const theme = tgTheme || (prefersDark ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
    document.body.setAttribute('data-theme', theme);
  }

  /* ── screen navigation ── */
  function switchScreen(screenId) {
    document.querySelectorAll('.svoy-screen').forEach(s => s.classList.remove('svoy-screen--active'));
    document.querySelectorAll('.svoy-tab').forEach(t => t.classList.remove('svoy-tab--active'));

    const target = document.getElementById(screenId);
    if (target) target.classList.add('svoy-screen--active');

    const tab = document.querySelector(`.svoy-tab[data-screen="${screenId}"]`);
    if (tab) tab.classList.add('svoy-tab--active');

    state.currentScreen = screenId;

    // Haptic feedback
    if (tg && tg.HapticFeedback) {
      try { tg.HapticFeedback.impactOccurred('light'); } catch (_) { }
    }
  }

  function initTabBar() {
    document.querySelectorAll('.svoy-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        const screen = btn.dataset.screen;
        if (screen) switchScreen(screen);
      });
    });
  }

  /* ── tariff cards ── */
  function renderTariffs() {
    const wrapper = document.getElementById('plansWrapper');
    if (!wrapper) return;

    if (!state.tariffs.length) {
      wrapper.innerHTML = '<p class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 svoy-muted" style="padding:16px;text-align:center;">Загрузка тарифов…</p>';
      return;
    }

    wrapper.innerHTML = '<div class="svoy-tariff-grid"></div>';
    const grid = wrapper.querySelector('.svoy-tariff-grid');

    state.tariffs.forEach(t => {
      const sel = state.selectedTariff && state.selectedTariff.id === t.id;
      const card = document.createElement('div');
      card.className = '_card_p3g9z_1' + (sel ? ' _is-selected_p3g9z_38' : '') + (t.popular ? ' _is-popular_p3g9z_45' : '');
      card.innerHTML = `
        <div class="_titles_p3g9z_23">
          <p class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 _weight_semibold_1hgcm_78">${t.months} ${monthWord(t.months)}</p>
          ${t.popular ? '<span class="_badge_p3g9z_30" style="background-color: var(--accent_text_color); color: var(--button_text_color); padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600;">Популярный</span>' : ''}
        </div>
        <div class="_prices_p3g9z_53">
          <p class="_root_1hgcm_29 _size_title3_1hgcm_43 _weight_bold_1hgcm_81">${fmtPrice(t.price)} ₽</p>
          ${t.oldPrice ? '<p class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 _is-strikethrough_1hgcm_99 svoy-muted">' + fmtPrice(t.oldPrice) + ' ₽</p>' : ''}
        </div>
        <p class="_root_1hgcm_29 _size_subtitle3_1hgcm_63 svoy-muted">${fmtPrice(t.pricePerMonth)} ₽ / мес</p>
      `;
      card.addEventListener('click', () => {
        state.selectedTariff = t;
        renderTariffs();
        updateTotal();
        if (tg && tg.HapticFeedback) try { tg.HapticFeedback.impactOccurred('light'); } catch (_) { }
      });
      grid.appendChild(card);
    });
  }

  /* ── payment methods ── */
  function renderPaymentMethods() {
    const wrapper = document.getElementById('paymentMethodsWrapper');
    if (!wrapper) return;
    wrapper.innerHTML = '';

    state.paymentMethods.forEach(m => {
      const sel = state.selectedPaymentMethod && state.selectedPaymentMethod.id === m.id;
      const row = document.createElement('div');
      row.className = '_item_mdt1z_62' + (sel ? ' _is-selected_mdt1z_72' : '');
      row.innerHTML = `
        <span style="font-size:22px; margin-right:12px;">${m.icon || '💳'}</span>
        <div style="flex:1;min-width:0;">
          <p class="_root_1hgcm_29 _size_subtitle1_1hgcm_55 _weight_semibold_1hgcm_78">${m.name}</p>
          ${m.description ? '<p class="_root_1hgcm_29 _size_subtitle3_1hgcm_63 svoy-muted">' + m.description + '</p>' : ''}
        </div>
        ${m.badge ? '<span style="background-color: var(--accent_text_color); color: var(--button_text_color); padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600;">' + m.badge + '</span>' : ''}
      `;
      row.addEventListener('click', () => {
        state.selectedPaymentMethod = m;
        renderPaymentMethods();
        updateTotal();
        if (tg && tg.HapticFeedback) try { tg.HapticFeedback.impactOccurred('light'); } catch (_) { }
      });
      wrapper.appendChild(row);
    });
  }

  /* ── servers ── */
  function renderServers() {
    const wrapper = document.getElementById('serversWrapper');
    if (!wrapper) return;

    if (!state.servers.length) {
      wrapper.innerHTML = '<p class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 svoy-muted" style="padding:16px;text-align:center;">Серверы загружаются…</p>';
      return;
    }

    let html = '<div class="svoy-servers-grid">';
    state.servers.forEach(s => {
      const f = serverFlag(s.name);
      const badge = s.name.toLowerCase().includes('росси') || s.name.toLowerCase().includes('ru') || s.name.toLowerCase() === 'russia'
        ? '<span style="background-color: var(--accent_text_color); color: var(--button_text_color); padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600;">Белый список</span>'
        : '';
      html += `
        <div class="_card_l4idp_15 svoy-server-card">
          <span style="font-size: 24px;">${f}</span>
          <div class="_inner_l4idp_37" style="flex: 1;">
            <p class="_root_1hgcm_29 _size_subtitle1_1hgcm_55 _weight_semibold_1hgcm_78">${s.name}</p>
          </div>
          ${badge}
        </div>
      `;
    });
    html += '</div>';
    wrapper.innerHTML = html;
  }

  /* ── total / pay button state ── */
  function updateTotal() {
    const el = document.getElementById('totalPrice');
    const btn = document.getElementById('btnPay');
    if (state.selectedTariff) {
      el.textContent = fmtPrice(state.selectedTariff.price) + ' ₽';
      btn.disabled = !state.selectedPaymentMethod;
    } else {
      el.textContent = '—';
      btn.disabled = true;
    }
  }

  /* ── profile ── */
  function renderProfile() {
    const nameEl = document.getElementById('profileName');
    const statusEl = document.getElementById('profileStatus');
    const badgeEl = document.getElementById('profileSubBadge');
    const untilEl = document.getElementById('profileUntil');
    const urlEl = document.getElementById('profileSubUrl');
    const subUrlSetup = document.getElementById('subscriptionUrl');

    if (state.user) {
      const full = [state.user.firstName, state.user.lastName].filter(Boolean).join(' ') || 'Пользователь';
      nameEl.textContent = full;
    }

    if (state.subscription) {
      if (state.subscription.isActive) {
        statusEl.textContent = 'Подписка активна';
        statusEl.style.color = 'var(--accent_text_color)';
        badgeEl.textContent = 'Активна';
        badgeEl.style.color = 'var(--accent_text_color)';
      } else {
        statusEl.textContent = 'Подписка неактивна';
        statusEl.style.color = '';
        badgeEl.textContent = 'Неактивна';
        badgeEl.style.color = 'var(--destructive_text_color, #ff4444)';
      }
      untilEl.textContent = fmtDate(state.subscription.endDate);

      if (state.subscription.subscriptionUrl) {
        urlEl.value = state.subscription.subscriptionUrl;
        subUrlSetup.value = state.subscription.subscriptionUrl;
      }
    }
  }

  /* ── clipboard ── */
  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text);
    } else {
      const t = document.createElement('textarea');
      t.value = text;
      document.body.appendChild(t);
      t.select();
      document.execCommand('copy');
      document.body.removeChild(t);
    }
    if (tg && tg.HapticFeedback) {
      try { tg.HapticFeedback.notificationOccurred('success'); } catch (_) { }
    }
  }

  /* ── API calls ── */
  async function fetchJSON(url, opts) {
    try {
      const r = await fetch(url, opts);
      if (!r.ok) return null;
      return await r.json();
    } catch { return null; }
  }

  async function loadTariffs() {
    const data = await fetchJSON('/api/tariffs');
    if (Array.isArray(data)) {
      state.tariffs = data;
      if (data.length && !state.selectedTariff) state.selectedTariff = data[0];
      renderTariffs();
      updateTotal();
    }
  }

  async function loadPaymentMethods() {
    const data = await fetchJSON('/api/payment-methods');
    if (Array.isArray(data)) {
      state.paymentMethods = data;
      if (data.length && !state.selectedPaymentMethod) state.selectedPaymentMethod = data[0];
      renderPaymentMethods();
      updateTotal();
    }
  }

  async function loadServers() {
    const data = await fetchJSON('/api/servers');
    if (Array.isArray(data)) {
      state.servers = data;
      renderServers();
    }
  }

  async function loadUser() {
    if (!tg || !tg.initData) return;
    const data = await fetchJSON('/api/user', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: tg.initData }),
    });
    if (data && data.user) {
      state.user = data.user;
      state.subscription = data.subscription;
      renderProfile();
    }
  }

  async function handlePay() {
    if (!state.selectedTariff || !state.selectedPaymentMethod) return;
    if (!tg || !tg.initData) {
      alert('Оплата доступна только в Telegram.');
      return;
    }

    const data = await fetchJSON('/api/payment/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        initData: tg.initData,
        tariffId: state.selectedTariff.id,
        paymentMethod: state.selectedPaymentMethod.id,
        deviceCount: 1,
      }),
    });

    if (data && (data.paymentUrl || data.invoiceUrl)) {
      const url = data.paymentUrl || data.invoiceUrl;
      if (tg && tg.openLink) tg.openLink(url);
      else window.open(url, '_blank');
    }
  }

  /* ── init ── */
  document.addEventListener('DOMContentLoaded', () => {
    if (tg) {
      tg.ready();
      tg.expand();
    }
    applyTheme();
    initTabBar();

    // Copy buttons
    const btnCopySub = document.getElementById('btnCopySub');
    const btnCopyProfile = document.getElementById('btnCopyProfile');
    if (btnCopySub) btnCopySub.addEventListener('click', () => {
      const v = document.getElementById('subscriptionUrl').value;
      if (v) copyToClipboard(v);
    });
    if (btnCopyProfile) btnCopyProfile.addEventListener('click', () => {
      const v = document.getElementById('profileSubUrl').value;
      if (v) copyToClipboard(v);
    });

    // Pay
    const btnPay = document.getElementById('btnPay');
    if (btnPay) btnPay.addEventListener('click', handlePay);

    // Profile links
    const btnChannel = document.getElementById('btnChannel');
    if (btnChannel) btnChannel.addEventListener('click', () => {
      if (tg && tg.openTelegramLink) tg.openTelegramLink('https://t.me/SvoyVPN');
      else window.open('https://t.me/SvoyVPN', '_blank');
    });
    const btnSupport = document.getElementById('btnSupport');
    if (btnSupport) btnSupport.addEventListener('click', () => {
      if (tg && tg.openTelegramLink) tg.openTelegramLink('https://t.me/SvoyVPN_support');
      else window.open('https://t.me/SvoyVPN_support', '_blank');
    });

    // Load data
    loadTariffs();
    loadPaymentMethods();
    loadServers();
    loadUser();
  });
})();
