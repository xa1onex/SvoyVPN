/* SvoyVPN miniapp – screen-based, no scroll */
(function () {
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  const state = {
    user: null, subscription: null,
    tariffs: [], paymentMethods: [], servers: [],
    selectedTariff: null, selectedPaymentMethod: null,
  };

  /* ── helpers ── */
  const MW = ['месяц', 'месяца', 'месяцев'];
  function mw(n) {
    if (n % 10 === 1 && n % 100 !== 11) return MW[0];
    if ([2, 3, 4].includes(n % 10) && ![12, 13, 14].includes(n % 100)) return MW[1];
    return MW[2];
  }
  const fp = p => Number(p).toLocaleString('ru-RU', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
  function fmtDate(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' });
  }

  const FLAGS = {
    'us': '🇺🇸', 'usa': '🇺🇸', 'сша': '🇺🇸', 'united states': '🇺🇸',
    'de': '🇩🇪', 'germany': '🇩🇪', 'германия': '🇩🇪',
    'nl': '🇳🇱', 'netherlands': '🇳🇱', 'нидерланды': '🇳🇱', 'голландия': '🇳🇱',
    'fi': '🇫🇮', 'finland': '🇫🇮', 'финляндия': '🇫🇮',
    'ru': '🇷🇺', 'russia': '🇷🇺', 'россия': '🇷🇺',
    'sg': '🇸🇬', 'singapore': '🇸🇬', 'сингапур': '🇸🇬',
    'gb': '🇬🇧', 'uk': '🇬🇧', 'великобритания': '🇬🇧',
    'fr': '🇫🇷', 'france': '🇫🇷', 'франция': '🇫🇷',
    'jp': '🇯🇵', 'japan': '🇯🇵', 'япония': '🇯🇵',
    'ca': '🇨🇦', 'canada': '🇨🇦', 'канада': '🇨🇦',
    'kz': '🇰🇿', 'kazakhstan': '🇰🇿', 'казахстан': '🇰🇿',
    'tr': '🇹🇷', 'turkey': '🇹🇷', 'турция': '🇹🇷',
    'ae': '🇦🇪', 'uae': '🇦🇪', 'оаэ': '🇦🇪',
    'in': '🇮🇳', 'india': '🇮🇳', 'индия': '🇮🇳',
  };
  function flag(name) {
    const n = (name || '').toLowerCase();
    for (const [k, v] of Object.entries(FLAGS)) if (n.includes(k)) return v;
    return '🌍';
  }

  /* ── theme ── */
  function applyTheme() {
    const t = (tg && tg.colorScheme) || (window.matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', t);
    document.body.setAttribute('data-theme', t);
  }

  /* ── navigation ── */
  function showScreen(id) {
    document.querySelectorAll('.svoy-screen').forEach(s => s.classList.remove('svoy-screen--active'));
    document.querySelectorAll('.svoy-tab').forEach(t => t.classList.remove('svoy-tab--active'));
    const el = document.getElementById(id);
    if (el) el.classList.add('svoy-screen--active');

    // highlight tab (plan screen maps to VPN tab)
    const tabId = id === 'screenPlan' ? 'screenVpn' : id;
    const tab = document.querySelector(`.svoy-tab[data-screen="${tabId}"]`);
    if (tab) tab.classList.add('svoy-tab--active');

    // hide/show tab bar on plan screen
    const bar = document.getElementById('tabBar');
    bar.style.display = id === 'screenPlan' ? 'none' : '';

    haptic('light');
  }

  function haptic(style) {
    if (tg && tg.HapticFeedback) try { tg.HapticFeedback.impactOccurred(style); } catch (_) { }
  }

  /* ── render: tariffs ── */
  function renderTariffs() {
    const w = document.getElementById('plansWrapper');
    if (!w) return;
    w.innerHTML = '';
    state.tariffs.forEach(t => {
      const sel = state.selectedTariff && state.selectedTariff.id === t.id;
      const c = document.createElement('div');
      c.className = '_card_p3g9z_1' + (sel ? ' _is-selected_p3g9z_38' : '') + (t.popular ? ' _is-popular_p3g9z_45' : '');
      c.innerHTML = `
        <div class="_titles_p3g9z_23">
          <p class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 _weight_semibold_1hgcm_78">${t.months} ${mw(t.months)}</p>
          ${t.popular ? '<span style="background:var(--accent_text_color);color:var(--button_text_color);padding:2px 6px;border-radius:6px;font-size:10px;font-weight:600;">Хит</span>' : ''}
        </div>
        <p class="_root_1hgcm_29 _size_title3_1hgcm_43 _weight_bold_1hgcm_81">${fp(t.price)} ₽</p>
        ${t.oldPrice ? '<p class="_root_1hgcm_29 _size_subtitle3_1hgcm_63 _is-strikethrough_1hgcm_99 svoy-muted">' + fp(t.oldPrice) + ' ₽</p>' : ''}
        <p class="_root_1hgcm_29 _size_subtitle4_1hgcm_67 svoy-muted">${fp(t.pricePerMonth)} ₽/мес</p>
      `;
      c.addEventListener('click', () => { state.selectedTariff = t; renderTariffs(); updateTotal(); haptic('light'); });
      w.appendChild(c);
    });
  }

  /* ── render: payment methods ── */
  function renderPM() {
    const w = document.getElementById('paymentMethodsWrapper');
    if (!w) return;
    w.innerHTML = '';
    state.paymentMethods.forEach(m => {
      const sel = state.selectedPaymentMethod && state.selectedPaymentMethod.id === m.id;
      const d = document.createElement('div');
      d.className = 'svoy-pm-item' + (sel ? ' svoy-pm-item--selected' : '');
      d.innerHTML = `<span style="font-size:20px;">${m.icon || '💳'}</span>
        <p class="_root_1hgcm_29 _size_subtitle1_1hgcm_55 _weight_semibold_1hgcm_78" style="flex:1;">${m.name}</p>`;
      d.addEventListener('click', () => { state.selectedPaymentMethod = m; renderPM(); updateTotal(); haptic('light'); });
      w.appendChild(d);
    });
  }

  /* ── render: servers ── */
  function renderServers() {
    const w = document.getElementById('serversWrapper');
    if (!w) return;
    if (!state.servers.length) { w.innerHTML = '<p class="_root_1hgcm_29 _size_subtitle3_1hgcm_63 svoy-muted" style="text-align:center;padding:8px;">Загрузка…</p>'; return; }
    w.innerHTML = '';
    state.servers.forEach(s => {
      const d = document.createElement('div');
      d.className = 'svoy-server-card';
      d.innerHTML = `<span style="font-size:20px;">${flag(s.name)}</span><p class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 _weight_medium_1hgcm_75">${s.name}</p>`;
      w.appendChild(d);
    });
  }

  /* ── total ── */
  function updateTotal() {
    const el = document.getElementById('totalPrice');
    const btn = document.getElementById('btnPay');
    if (state.selectedTariff) {
      el.textContent = fp(state.selectedTariff.price) + ' ₽';
      btn.disabled = !state.selectedPaymentMethod;
    } else { el.textContent = '—'; btn.disabled = true; }
  }

  /* ── render: profile + vpn status ── */
  function renderUser() {
    if (state.user) {
      const n = [state.user.firstName, state.user.lastName].filter(Boolean).join(' ') || 'Пользователь';
      document.getElementById('profileName').textContent = n;
    }
    const sub = state.subscription;
    const badge = document.getElementById('vpnSubBadge');
    const until = document.getElementById('vpnSubUntil');
    const status = document.getElementById('vpnStatus');
    const pStatus = document.getElementById('profileStatus');

    if (sub && sub.isActive) {
      badge.textContent = 'Активна'; badge.className = badge.className.replace('svoy-badge-inactive', 'svoy-badge-active');
      until.textContent = fmtDate(sub.endDate);
      status.textContent = 'Подписка активна';
      pStatus.textContent = 'Подписка активна'; pStatus.style.color = 'var(--accent_text_color)';
    } else {
      badge.textContent = 'Неактивна';
      status.textContent = 'Быстрый и приватный VPN';
      pStatus.textContent = 'Подписка неактивна';
    }

    if (sub && sub.subscriptionUrl) {
      document.getElementById('subscriptionUrl').value = sub.subscriptionUrl;
      document.getElementById('profileSubUrl').value = sub.subscriptionUrl;
    }
  }

  /* ── clipboard ── */
  function copy(text) {
    if (navigator.clipboard) navigator.clipboard.writeText(text);
    else { const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select(); document.execCommand('copy'); document.body.removeChild(t); }
    if (tg && tg.HapticFeedback) try { tg.HapticFeedback.notificationOccurred('success'); } catch (_) { }
  }

  /* ── API ── */
  async function api(url, opts) { try { const r = await fetch(url, opts); if (!r.ok) return null; return await r.json(); } catch { return null; } }

  async function loadData() {
    const [tariffs, pm, servers] = await Promise.all([api('/api/tariffs'), api('/api/payment-methods'), api('/api/servers')]);
    if (Array.isArray(tariffs)) { state.tariffs = tariffs; if (tariffs.length) state.selectedTariff = tariffs[0]; renderTariffs(); updateTotal(); }
    if (Array.isArray(pm)) { state.paymentMethods = pm; if (pm.length) state.selectedPaymentMethod = pm[0]; renderPM(); updateTotal(); }
    if (Array.isArray(servers)) { state.servers = servers; renderServers(); }
  }

  async function loadUser() {
    if (!tg || !tg.initData) return;
    const d = await api('/api/user', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData }) });
    if (d && d.user) { state.user = d.user; state.subscription = d.subscription; renderUser(); }
  }

  async function handlePay() {
    if (!state.selectedTariff || !state.selectedPaymentMethod) return;
    if (!tg || !tg.initData) { alert('Оплата доступна только в Telegram.'); return; }
    const d = await api('/api/payment/create', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, tariffId: state.selectedTariff.id, paymentMethod: state.selectedPaymentMethod.id, deviceCount: 1 }) });
    if (d && (d.paymentUrl || d.invoiceUrl)) { const u = d.paymentUrl || d.invoiceUrl; tg.openLink ? tg.openLink(u) : window.open(u, '_blank'); }
  }

  /* ── init ── */
  document.addEventListener('DOMContentLoaded', () => {
    if (tg) { tg.ready(); tg.expand(); }
    applyTheme();

    // Tab bar
    document.querySelectorAll('.svoy-tab').forEach(b => b.addEventListener('click', () => { if (b.dataset.screen) showScreen(b.dataset.screen); }));

    // Choose plan → plan screen
    document.getElementById('btnChoosePlan').addEventListener('click', () => showScreen('screenPlan'));
    document.getElementById('btnBackFromPlan').addEventListener('click', () => showScreen('screenVpn'));

    // Copy
    document.getElementById('btnCopySub').addEventListener('click', () => { const v = document.getElementById('subscriptionUrl').value; if (v) copy(v); });
    document.getElementById('btnCopyProfile').addEventListener('click', () => { const v = document.getElementById('profileSubUrl').value; if (v) copy(v); });

    // Pay
    document.getElementById('btnPay').addEventListener('click', handlePay);

    // Links
    document.getElementById('btnChannel').addEventListener('click', () => { tg && tg.openTelegramLink ? tg.openTelegramLink('https://t.me/SvoyVPN') : window.open('https://t.me/SvoyVPN', '_blank'); });
    document.getElementById('btnSupport').addEventListener('click', () => { tg && tg.openTelegramLink ? tg.openTelegramLink('https://t.me/SvoyVPN_support') : window.open('https://t.me/SvoyVPN_support', '_blank'); });

    loadData();
    loadUser();
  });
})();
