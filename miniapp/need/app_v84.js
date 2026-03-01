/* ═══════════════════════════════════════════
   SvoyVPN Miniapp — App Logic
   ═══════════════════════════════════════════ */
(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

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
  function forceHeaderColor() {
    if (!tg) return;
    const scheme = tg.colorScheme || 'dark';
    const bgColor = scheme === 'dark' ? '#18222d' : '#ffffff';
    const secBgColor = scheme === 'dark' ? '#21303f' : '#f7f9fb';
    try { tg.setHeaderColor(bgColor); } catch (_) { }
    try { tg.setBackgroundColor(bgColor); } catch (_) { }
    try { tg.setBottomBarColor(secBgColor); } catch (_) { }
  }

  function applyTheme() {
    const scheme = (tg && tg.colorScheme) ||
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
    const subBlockBox = document.getElementById('subBlockBox');

    let daysLeft = 0;
    if (sub && sub.isActive && sub.endDate) {
      const end = new Date(sub.endDate);
      const now = new Date();
      const diff = end.getTime() - now.getTime();
      daysLeft = Math.ceil(diff / (1000 * 3600 * 24));
    }

    if (subBlockBox) {
      if (sub && sub.isActive) {
        if (vpnStatus) vpnStatus.textContent = 'Подписка активна';
        if (pStatus) {
          pStatus.textContent = 'Подписка активна';
          pStatus.classList.remove('text-muted');
          pStatus.classList.add('text-accent');
        }

        let statusHtml = `
            <div class="card status-card">
              <div class="status-row">
                <span class="subtitle">Статус</span>
                <span class="caption" style="color:var(--accent_text_color, #3aa8fc); font-weight: 600;">Активна</span>
              </div>
              <div class="status-row">
                <span class="body text-muted">Действует до</span>
                <span class="body">${daysLeft > 0 ? 'Осталось ' + daysLeft + ' дн.' : fmtDate(sub.endDate)}</span>
              </div>
            </div>
            <div class="gap-12"></div>
        `;

        statusHtml += `
          <div style="display: flex; gap: 8px; width: 100%;">
            <button class="btn-primary" style="flex:1;" onclick="window.showScreen('screenSetup')">Подключиться</button>
            <button class="btn-secondary" style="flex:1;" onclick="window.showModal('modalPlan')">Продлить</button>
          </div>
        `;
        subBlockBox.innerHTML = statusHtml;

      } else {
        if (vpnStatus) vpnStatus.textContent = 'Быстрый и приватный VPN';
        if (pStatus) {
          pStatus.textContent = 'Подписка неактивна';
          pStatus.classList.remove('text-accent');
          pStatus.classList.add('text-muted');
        }

        if (S.user && S.user.trialAvailable) {
          subBlockBox.innerHTML = `
            <div class="card" style="padding:16px; text-align:center; background: linear-gradient(135deg, rgba(58,168,252,0.1) 0%, rgba(58,168,252,0) 100%); border: 1px dashed var(--accent_text_color, #3aa8fc); border-radius: 12px;">
               <div style="margin-bottom: 8px;">
                 <svg class="gift-anim" viewBox="0 0 24 24" width="36" height="36" fill="none" stroke="var(--accent_text_color, #3aa8fc)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
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
               <p class="subtitle" style="color:var(--accent_text_color, #3aa8fc); margin-bottom:8px; font-weight: 700;">Попробуй бесплатно!</p>
               <p class="body text-muted" style="margin-bottom:16px; font-size:13px; line-height: 1.4;">Тебе доступно <b>${S.user.trialDays} дней</b> бесплатного тест-драйва без привязки карты.</p>
               <button class="btn-primary" id="btnActivateTrial" style="min-height: 48px;">Забрать ${S.user.trialDays} дней</button>
            </div>
            <div class="gap-12"></div>
            <button class="btn-secondary" style="width:100%;" onclick="window.showModal('modalPlan')">Выбрать тариф</button>
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
              showToast('Пробный период активирован!');
              haptic('success');
              await loadUser(); // Reload user state
            } else {
              showToast('Ошибка активации: ' + (d ? d.error : 'Неизвестная ошибка'));
              this.disabled = false;
              this.textContent = `Забрать ${S.user.trialDays} дней`;
            }
          });

        } else {
          subBlockBox.innerHTML = `
            <div class="card status-card">
              <div class="status-row">
                <span class="subtitle">Подписка</span>
                <span class="caption text-danger">Неактивна</span>
              </div>
            </div>
            <div class="gap-12"></div>
            <button class="btn-primary" onclick="window.showModal('modalPlan')">Выбрать тариф</button>
          `;
        }
      }
    }

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
              <div style="margin-bottom: 8px;">
                <svg class="gift-anim" viewBox="0 0 24 24" width="36" height="36" fill="none" stroke="var(--accent_text_color, #3aa8fc)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="20 12 20 22 4 22 4 12"></polyline>
                  <rect x="2" y="7" width="20" height="5"></rect>
                  <line x1="12" y1="22" x2="12" y2="7"></line>
                  <path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z"></path>
                  <path d="M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z"></path>
                </svg>
              </div>
              <p class="body text-muted" style="margin-bottom:16px; font-size:13px;">Или попробуйте бесплатно — заберите пробный период в подарок!</p>
              <button class="btn-primary" style="min-height:40px; font-size:14px; width:100%;" onclick="window.showScreen('screenVpn')">Забрать ${S.user.trialDays} дней</button>
            `;
        } else {
          checkHtml += `
              <button class="btn-primary" style="min-height:40px; font-size:14px; width:100%;" onclick="window.showModal('modalPlan')">Выбрать тариф</button>
            `;
        }
        checkHtml += `</div>`;
        subCheckBlock.innerHTML = checkHtml;
      }
    }

    // Subscription URL
    if (sub && sub.subscriptionUrl) {
      const elSetup = document.getElementById('subUrlSetup');
      const elProfile = document.getElementById('subUrlProfile');
      if (elSetup) elSetup.value = sub.subscriptionUrl;
      if (elProfile) elProfile.value = sub.subscriptionUrl;
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

    function addClick(id, handler) {
      const el = document.getElementById(id);
      if (el) el.addEventListener('click', handler);
      else console.warn('Missing element for click:', id);
    }

    addClick('btnChoosePlan', () => window.showModal('modalPlan'));

    const btnReferral = document.getElementById('btnReferral');
    if (btnReferral) {
      btnReferral.addEventListener('click', () => {
        window.showModal('modalReferral');
        loadReferral();
      });
    }

    let refLink = '';
    async function loadReferral() {
      if (!tg || !tg.initData) return;
      const d = await api('/miniapp/api/referral?initData=' + encodeURIComponent(tg.initData));
      if (d && d.referralCode) {
        refLink = d.refLink;
        const refL = document.getElementById('refLinkText');
        if (refL) refL.textContent = d.refLink;
        const refC = document.getElementById('refCount');
        if (refC) refC.textContent = d.referralCount + ' чел.';
        const refB = document.getElementById('refBonus');
        if (refB) refB.textContent = d.inviterBonusDays + ' дн. за друга';
        const refD = document.getElementById('refDesc');
        if (refD) refD.textContent = `Дарим ${d.inviterBonusDays} дней Вам и ${d.invitedBonusDays} дня другу за каждое успешное приглашение.`;
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

    addClick('btnCopyProfile', function () {
      const el = document.getElementById('subUrlProfile');
      if (el) copyText(el.value, this);
    });

    // Pay
    addClick('btnPay', handlePay);

    // Links
    addClick('btnChannel', () => {
      tg && tg.openTelegramLink
        ? tg.openTelegramLink('https://t.me/SvoyVPN')
        : window.open('https://t.me/SvoyVPN', '_blank');
    });
    addClick('btnSupport', () => {
      tg && tg.openTelegramLink
        ? tg.openTelegramLink('https://t.me/SvoyVPN_support')
        : window.open('https://t.me/SvoyVPN_support', '_blank');
    });

    // Load data
    loadData();
    loadUser();

    // ═══════════════════════════════════════
    //  ONBOARDING CAROUSEL — Setup Screen
    // ═══════════════════════════════════════
    initOnboarding();
  });

  /* ─────────────────────────────────────────
     Onboarding carousel controller
  ───────────────────────────────────────── */
  function initOnboarding() {
    const TOTAL_SLIDES = 5;

    // State
    let currentSlide = 0;
    let selectedDevice = null; // 'ios' | 'android' | 'windows' | 'mac'
    let linkCopied = false;

    // App data per platform — icons loaded from /miniapp/images/ (add files there)
    const APPS = {
      ios: [
        {
          name: 'V2RayTun',
          store: 'App Store',
          iconImg: '/miniapp/images/v2raytun.png',
          url: 'https://apps.apple.com/app/v2raytun/id6476628951',
        },
        {
          name: 'Hiddify',
          store: 'App Store',
          iconImg: '/miniapp/images/hiddify.png',
          url: 'https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532',
        },
      ],
      android: [
        {
          name: 'V2RayTun',
          store: 'Google Play',
          iconImg: '/miniapp/images/v2raytun.png',
          url: 'https://play.google.com/store/apps/details?id=com.v2raytun.android',
        },
        {
          name: 'Hiddify',
          store: 'Google Play',
          iconImg: '/miniapp/images/hiddify.png',
          url: 'https://play.google.com/store/apps/details?id=app.hiddify.com',
        },
      ],
      windows: null,
      mac: [
        {
          name: 'V2RayTun',
          store: 'App Store',
          iconImg: '/miniapp/images/v2raytun.png',
          url: 'https://apps.apple.com/kz/app/v2raytun/id6476628951',
        },
        {
          name: 'Hiddify',
          store: 'App Store',
          iconImg: '/miniapp/images/hiddify.png',
          url: 'https://apps.apple.com/kz/app/hiddify-proxy-vpn/id6596777532',
        },
      ],
    };

    const PC_STEPS = {
      windows: [
        'Скачайте <strong>Hiddify</strong> с <a href="https://github.com/hiddify/hiddify-app/releases" target="_blank" style="color:var(--accent_text_color,#3aa8fc)">GitHub</a> или <strong>Nekoray</strong> с официального сайта.',
        'Установите приложение и запустите его.',
        'Нажмите «+» или «Добавить подписку» → вставьте скопированную ссылку.',
        'Нажмите «Обновить» — серверы появятся автоматически.',
      ],
    };

    // DOM refs
    const track = document.getElementById('obTrack');
    const btnNext = document.getElementById('obBtnNext');
    const btnBack = document.getElementById('obBtnBack');
    const dots = [0, 1, 2, 3, 4].map(i => document.getElementById('obDot' + i));
    const obActionRow = document.getElementById('obActionRow');
    const obBtnCopied = document.getElementById('obBtnCopied');
    const obAppContent = document.getElementById('obAppContent');

    if (!track || !btnNext || !btnBack) return;

    /* ── Slide navigation ── */
    function goToSlide(idx, direction) {
      if (idx < 0 || idx >= TOTAL_SLIDES) return;

      // Animate the outgoing slide
      const animClass = direction === 'forward' ? 'anim-in' : 'anim-back';
      const slide = document.getElementById('obSlide' + idx);
      if (slide) {
        slide.classList.remove('anim-in', 'anim-back');
        // Force reflow
        void slide.offsetWidth;
        slide.classList.add(animClass);
        setTimeout(() => slide.classList.remove(animClass), 400);
      }

      currentSlide = idx;
      track.style.transform = `translateX(-${idx * 100}%)`;

      // Update dots
      dots.forEach((d, i) => d.classList.toggle('active', i === idx));

      // Update buttons
      updateButtons();
      haptic('light');
    }

    function updateButtons() {
      const isLast = currentSlide === TOTAL_SLIDES - 1;
      const subActive = S.subscription && S.subscription.isActive;

      if (currentSlide === 0) {
        // Slide 0: Activation Check
        btnNext.textContent = 'Далее →';
        btnNext.disabled = !subActive;
        if (obActionRow) obActionRow.classList.remove('split');
      } else if (currentSlide === 1) {
        // Slide 1: Copy Link
        if (!linkCopied) {
          btnNext.textContent = 'Скопировать ссылку';
          btnNext.disabled = !subActive;
          if (obActionRow) obActionRow.classList.remove('split');
        } else {
          btnNext.textContent = 'Далее →';
          btnNext.disabled = false;
          if (obActionRow) obActionRow.classList.add('split');
          if (obBtnCopied) {
            obBtnCopied.textContent = '✓ Скопировано';
            obBtnCopied.className = 'ob-btn-copied';
          }
        }
      } else {
        // Other slides
        btnNext.textContent = isLast ? 'Готово ✓' : 'Далее →';
        if (obActionRow) obActionRow.classList.add('split');
        if (obBtnCopied) {
          obBtnCopied.textContent = '← Назад';
          obBtnCopied.className = 'ob-btn-copied is-back';
        }
        switch (currentSlide) {
          case 2: btnNext.disabled = !selectedDevice; break;
          default: btnNext.disabled = false;
        }
      }
      if (btnBack) btnBack.style.display = 'none';
    }

    /* ── Slide 3 content (app list or PC steps) ── */
    function renderSlide3() {
      if (!obAppContent || !selectedDevice) return;
      obAppContent.innerHTML = '';

      const isPC = selectedDevice === 'windows';
      const platformLabel = { ios: 'iPhone', android: 'Android', windows: 'Windows', mac: 'Mac' }[selectedDevice];

      // Title + desc
      const title = document.createElement('p');
      title.className = 'ob-title';
      title.textContent = isPC ? 'Установите приложение' : 'Откройте приложение';
      obAppContent.appendChild(title);

      const desc = document.createElement('p');
      desc.className = 'ob-desc';
      desc.textContent = isPC
        ? 'Следуйте шагам ниже для подключения на ' + platformLabel + ':'
        : 'Выберите приложение и установите его из магазина:';
      obAppContent.appendChild(desc);

      if (isPC) {
        // PC steps
        const stepsWrap = document.createElement('div');
        stepsWrap.className = 'ob-pc-steps';
        const steps = PC_STEPS[selectedDevice] || [];
        steps.forEach((text, i) => {
          const row = document.createElement('div');
          row.className = 'ob-pc-step';
          row.innerHTML =
            '<div class="ob-pc-num">' + (i + 1) + '</div>' +
            '<div class="ob-pc-text">' + text + '</div>';
          stepsWrap.appendChild(row);
        });
        obAppContent.appendChild(stepsWrap);
      } else {
        // App links
        const list = document.createElement('div');
        list.className = 'ob-app-list';
        const apps = APPS[selectedDevice] || [];
        apps.forEach(app => {
          const item = document.createElement('a');
          item.className = 'ob-app-item';
          item.href = app.url;
          item.target = '_blank';
          item.rel = 'noopener';
          item.addEventListener('click', e => {
            e.preventDefault();
            haptic('light');
            const tg = window.Telegram && window.Telegram.WebApp;
            tg && tg.openLink ? tg.openLink(app.url) : window.open(app.url, '_blank');
          });
          // Build icon: img tag with fallback to letter avatar
          const iconHtml = '<div class="ob-app-icon ob-app-icon--img">' +
            '<img src="' + app.iconImg + '?v=70" alt="' + app.name + '" ' +
            'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'" />' +
            '<span class="ob-app-icon-fallback" style="display:none;">' + app.name.charAt(0) + '</span>' +
            '</div>';
          item.innerHTML =
            iconHtml +
            '<div class="ob-app-info">' +
            '<p class="ob-app-name">' + app.name + '</p>' +
            '<p class="ob-app-store">Открыть в ' + app.store + '</p>' +
            '</div>' +
            '<span class="ob-app-arrow">›</span>';
          list.appendChild(item);
        });
        obAppContent.appendChild(list);
      }
    }

    /* ── Perform copy + trigger split animation ── */
    function doSlideCopy() {
      const url = document.getElementById('subUrlSetup').value;
      copyText(url, null);
      if (!linkCopied) {
        linkCopied = true;
        if (obActionRow) obActionRow.classList.add('split');
        updateButtons();
      } else {
        haptic('light'); // manual tactile feedback on repeat copy
      }
    }

    if (obBtnCopied) {
      obBtnCopied.addEventListener('click', () => {
        if (currentSlide === 1) {
          doSlideCopy();
        } else {
          goToSlide(currentSlide - 1, 'back');
        }
      });
    }

    /* ── Slide 2: Device picker ── */
    document.querySelectorAll('.ob-device-card').forEach(card => {
      card.addEventListener('click', () => {
        document.querySelectorAll('.ob-device-card').forEach(c => c.classList.remove('selected'));
        card.classList.add('selected');
        selectedDevice = card.dataset.device;
        haptic('light');
        updateButtons();
      });
    });

    /* ── Next/Copy button ── */
    btnNext.addEventListener('click', () => {
      if (currentSlide === 0) {
        if (S.subscription && S.subscription.isActive) {
          goToSlide(1, 'forward');
        } else {
          showToast('Активируйте подписку, чтобы продолжить');
        }
      } else if (currentSlide === 1) {
        if (!linkCopied) {
          doSlideCopy();
        } else {
          goToSlide(2, 'forward');
        }
      } else if (currentSlide === 2) {
        renderSlide3();
        goToSlide(3, 'forward');
      } else if (currentSlide === 3) {
        goToSlide(4, 'forward');
      } else {
        showScreen('screenVpn');
      }
    });

    /* ── Back button ── */
    btnBack.addEventListener('click', () => {
      goToSlide(currentSlide - 1, 'back');
    });

    /* ── Swipe support (horizontal) ── */
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
        // Don't prevent default — allow vertical scroll on device grid if needed
      }, { passive: true });

      carousel.addEventListener('touchend', e => {
        if (!isSwiping) return;
        const dx = e.changedTouches[0].clientX - touchStartX;
        if (Math.abs(dx) < 40) return;

        if (dx < 0 && currentSlide < TOTAL_SLIDES - 1) {
          // swipe left → next (only if allowed)
          const canNext = !btnNext.disabled;
          if (!canNext) { haptic('error'); return; }
          if (currentSlide === 1) renderSlide3();
          goToSlide(currentSlide + 1, 'forward');
        } else if (dx > 0 && currentSlide > 0) {
          goToSlide(currentSlide - 1, 'back');
        }
        isSwiping = false;
      }, { passive: true });
    }

    /* ── Reset carousel ── */
    function resetCarousel() {
      currentSlide = 0;
      selectedDevice = null;
      linkCopied = false;
      track.style.transform = 'translateX(0)';
      dots.forEach((d, i) => d.classList.toggle('active', i === 0));
      document.querySelectorAll('.ob-device-card').forEach(c => c.classList.remove('selected'));
      // Collapse the split animation
      if (obActionRow) obActionRow.classList.remove('split');
      updateButtons();
    }

    // Listen for tab switch away from setup screen — reset on re-entry
    document.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.dataset.screen !== 'screenSetup') {
          // leaving setup: full reset so next visit starts fresh
          setTimeout(resetCarousel, 400);
        }
      });
    });

    /* ── Icon copy button in copy-field also triggers the split ── */
    const btnCopySetupIcon = document.getElementById('btnCopySetup');
    if (btnCopySetupIcon) {
      btnCopySetupIcon.addEventListener('click', function () {
        if (document.getElementById('subUrlSetup').value && !linkCopied) {
          doSlideCopy();
        }
      });
    }

    // Initial state (slide 0, next disabled)
    updateButtons();
  }
})();

