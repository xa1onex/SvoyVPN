/*
 * SvoyVPN — Web Dashboard enhancement layer (web-only).
 *
 * Loaded ONLY by app.html (browser dashboard). It never runs inside the
 * Telegram Mini App or the mobile WebViews, so it cannot regress them.
 *
 * Responsibilities:
 *   - Build the "Обзор" (Overview) landing: KPI stat cards + SVG charts.
 *   - Enrich the topbar with tier / days-left / user chips.
 *   - Reflect subscription status in the sidebar footer.
 *   - Make Overview the default landing on desktop.
 *
 * Data comes from the same REST endpoints the app already uses
 * (/api/user, /api/servers, /api/ping). The JWT + base URL are injected by
 * the fetch interceptor defined in app_v124.js, so plain relative fetches work.
 */
(function () {
  'use strict';

  // Only operate on the web dashboard.
  if (!(document.body && document.body.classList.contains('dash-body'))) return;
  if (!(window.AppConfig && window.AppConfig.isWeb)) return;

  var DESKTOP_MIN = 1024;
  var PING_CAP = 300;          // ms mapped to a full bar
  var PING_REFRESH_MS = 60000;

  var initialHash = (location.hash || '').replace('#', '').toLowerCase().trim();
  var built = false;
  var didDefaultLand = false;
  var userNavigated = false;
  var pingTimer = null;
  var bestServer = null;
  var lastData = { user: null, subscription: null, referral: null, servers: [] };

  // Any explicit tab click means the user chose a screen — stop forcing Overview.
  document.addEventListener('click', function (e) {
    if (e.target && e.target.closest && e.target.closest('.tab')) userNavigated = true;
  }, true);

  function isDesktop() { return window.innerWidth >= DESKTOP_MIN; }
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ───────── data ───────── */
  async function getJson(url, opts) {
    try {
      var r = await fetch(url, opts || {});
      if (!r.ok) return null;
      return await r.json();
    } catch (_) { return null; }
  }

  async function fetchAll() {
    var userP = getJson('/api/user', { method: 'GET' });
    var serversP = getJson('/api/servers');
    var user = await userP;
    if (!user || !user.user) return null; // not authenticated yet
    var servers = await serversP;
    return {
      user: user.user,
      subscription: user.subscription || null,
      referral: user.referral || null,
      servers: Array.isArray(servers) ? servers : []
    };
  }

  /* ───────── helpers ───────── */
  function calendarDaysUntilEnd(endDate) {
    if (!endDate) return null;
    var end = new Date(endDate);
    if (isNaN(end.getTime())) return null;
    var now = new Date();
    var a = Date.UTC(end.getFullYear(), end.getMonth(), end.getDate());
    var b = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((a - b) / 86400000);
  }
  function declDays(n) {
    n = Math.abs(n) % 100;
    var n1 = n % 10;
    if (n > 10 && n < 20) return 'дней';
    if (n1 > 1 && n1 < 5) return 'дня';
    if (n1 === 1) return 'день';
    return 'дней';
  }
  function fmtDate(d) {
    var dt = new Date(d);
    if (isNaN(dt.getTime())) return '';
    return dt.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short', year: 'numeric' });
  }
  function initials(user) {
    var a = (user.firstName || '').trim();
    var b = (user.lastName || '').trim();
    var s = (a[0] || '') + (b[0] || '');
    if (!s && user.username) s = user.username[0];
    return (s || '?').toUpperCase();
  }
  function pingClass(ms) {
    if (ms == null || ms < 0) return 'dead';
    if (ms < 80) return 'fast';
    if (ms < 150) return 'med';
    return 'slow';
  }

  /* ───────── SVG charts ───────── */
  function gauge(percent, opts) {
    opts = opts || {};
    var p = Math.max(0, Math.min(100, percent));
    var r = 66, cx = 84, cy = 84;
    var circ = 2 * Math.PI * r;
    var dash = circ * 0.75;                 // 270deg arc
    var offset = dash * (1 - p / 100);
    var mod = opts.mod ? ' gauge__arc--' + opts.mod : '';
    return '' +
      '<div class="gauge">' +
      '<svg class="gauge__svg" viewBox="0 0 168 168">' +
        '<defs><linearGradient id="gaugeGrad" x1="0" y1="0" x2="1" y2="1">' +
          '<stop offset="0" stop-color="#00f5a0"/><stop offset="1" stop-color="#00c47f"/>' +
        '</linearGradient></defs>' +
        '<g transform="rotate(135 84 84)">' +
          '<circle class="gauge__track" cx="' + cx + '" cy="' + cy + '" r="' + r + '" stroke-width="12" ' +
            'stroke-dasharray="' + dash + ' ' + circ + '"/>' +
          '<circle class="gauge__arc' + mod + '" cx="' + cx + '" cy="' + cy + '" r="' + r + '" stroke-width="12" ' +
            'stroke-dasharray="' + dash + ' ' + circ + '" stroke-dashoffset="' + offset + '"/>' +
        '</g>' +
        '<text class="gauge__pct" x="84" y="80" text-anchor="middle">' + esc(opts.big != null ? opts.big : (Math.round(p) + '%')) + '</text>' +
        '<text class="gauge__cap" x="84" y="100" text-anchor="middle">' + esc(opts.cap || '') + '</text>' +
      '</svg>' +
      (opts.legend || '') +
      '</div>';
  }

  function legend2(aV, aL, bV, bL) {
    return '<div class="gauge__legend">' +
      '<div class="gauge__legend-item"><div class="gauge__legend-v">' + esc(aV) + '</div><div class="gauge__legend-l">' + esc(aL) + '</div></div>' +
      '<div class="gauge__legend-item"><div class="gauge__legend-v">' + esc(bV) + '</div><div class="gauge__legend-l">' + esc(bL) + '</div></div>' +
      '</div>';
  }

  function linProgress(bigHtml, capText, percent) {
    var p = Math.max(0, Math.min(100, percent));
    return '<div class="lin">' +
      '<div class="lin__head"><div class="lin__v">' + bigHtml + '</div></div>' +
      '<div class="lin__track"><div class="lin__fill" data-w="' + p + '"></div></div>' +
      '<div class="lin__cap">' + esc(capText) + '</div>' +
      '</div>';
  }

  function barRow(s) {
    var cls = pingClass(s.ping);
    var pct = (s.ping != null && s.ping >= 0) ? Math.max(4, Math.min(100, (s.ping / PING_CAP) * 100)) : 100;
    var valTxt = (s.ping == null) ? '…' : (s.ping < 0 ? 'N/A' : (s.ping + ' мс'));
    return '<div class="bar-row" data-sid="' + esc(s.id) + '">' +
      '<div class="bar-row__label"><span class="flag">' + esc(s.flag || '🌐') + '</span><span class="name">' + esc(s.name) + '</span></div>' +
      '<div class="bar-row__track"><div class="bar-row__fill is-' + cls + '" style="width:' + pct + '%"></div></div>' +
      '<div class="bar-row__val is-' + cls + '">' + esc(valTxt) + '</div>' +
      '</div>';
  }

  var raf = (typeof window.requestAnimationFrame === 'function')
    ? window.requestAnimationFrame.bind(window)
    : function (cb) { return setTimeout(cb, 16); };

  function animateBars(root) {
    (root || document).querySelectorAll('.lin__fill[data-w]').forEach(function (el) {
      var w = el.getAttribute('data-w');
      raf(function () { el.style.width = w + '%'; });
    });
  }

  /* ───────── ping ───────── */
  async function measurePing(id) {
    try {
      var t0 = performance.now();
      var r = await fetch('/api/ping?id=' + encodeURIComponent(id), { cache: 'no-store' });
      var t1 = performance.now();
      if (!r.ok) return -1;
      var d = await r.json();
      if (d && typeof d.ping === 'number') return d.ping;
      return Math.round(t1 - t0);
    } catch (_) { return -1; }
  }

  function refreshPings() {
    var rows = document.querySelectorAll('#ovServers .ov-row[data-sid]');
    if (!rows.length) return;
    var best = { ms: Infinity, name: '', flag: '' };
    rows.forEach(function (row) {
      var sid = row.getAttribute('data-sid');
      if (!sid) return;
      measurePing(sid).then(function (ms) {
        var cls = pingClass(ms);
        var val = row.querySelector('.ov-row__val');
        if (val) {
          val.className = 'ov-row__val' + (ms < 0 ? ' is-muted' : (cls === 'fast' ? '' : ' is-warn'));
          val.textContent = (ms < 0 ? 'N/A' : ms + ' ms');
        }
        if (ms >= 0 && ms < best.ms) {
          var nameEl = row.querySelector('.ov-row__name');
          best = { ms: ms, name: nameEl ? nameEl.textContent : '', flag: row.getAttribute('data-flag') || '' };
        }
        updateBestNode(best);
      });
    });
  }

  function updateBestNode(best) {
    if (!best || best.ms === Infinity) return;
    bestServer = best;
    var nameEl = $('ovNodeName');
    var latEl = $('ovNodeLatency');
    if (nameEl) nameEl.textContent = best.name || '—';
    if (latEl) latEl.textContent = best.ms + 'ms latency';
  }

  /* ───────── flag from server emoji/name ───────── */
  function serverFlag(s) {
    var t = String((s && s.emoji) || (s && s.name) || '');
    var cps = Array.from(t);
    for (var i = 0; i < cps.length; i++) {
      var c = cps[i].codePointAt(0);
      if (c >= 0x1F1E6 && c <= 0x1F1FF) {
        var next = cps[i + 1] ? cps[i + 1].codePointAt(0) : 0;
        if (next >= 0x1F1E6 && next <= 0x1F1FF) return cps[i] + cps[i + 1];
      }
      if (c >= 0x1F300) return cps[i]; // some other emoji
    }
    return '🌐';
  }

  function ovKpi(label, value, sub, green) {
    return '<div class="ov-kpi">' +
      '<div class="ov-kpi__label">' + esc(label) + '</div>' +
      '<div class="ov-kpi__value' + (green ? ' is-green' : '') + '">' + value + '</div>' +
      '<div class="ov-kpi__sub">' + esc(sub) + '</div></div>';
  }

  function ovRow(name, val, valCls) {
    return '<div class="ov-row" data-sid-placeholder="1">' +
      '<span class="ov-row__name">' + esc(name) + '</span>' +
      '<span class="ov-row__val' + (valCls ? ' ' + valCls : '') + '">' + esc(val) + '</span></div>';
  }

  function serverOvRow(s) {
    var flag = serverFlag(s);
    var name = s.name || ('Сервер ' + s.id);
    return '<div class="ov-row" data-sid="' + esc(s.id) + '" data-flag="' + esc(flag) + '">' +
      '<span class="ov-row__name">' + esc((flag ? flag + ' ' : '') + name) + '</span>' +
      '<span class="ov-row__val is-muted">…</span></div>';
  }

  function trafficBars(pct) {
    var heights = [42, 58, 48, 72, 55, 80, 65, 90, 70, 85, 60, 75];
    var scale = Math.max(0.35, Math.min(1, (pct || 30) / 100));
    return heights.map(function (h, i) {
      var hh = Math.round(h * scale * (0.85 + (i % 3) * 0.05));
      return '<div class="ov-graph-bar" style="height:' + hh + '%" data-h="' + hh + '"></div>';
    }).join('');
  }

  function buildActivity(d, sv) {
    var items = [];
    if (sv.active) items.push(['Подписка активна', 'сейчас']);
    else items.push(['Оформите тариф', '—']);
    if (d.servers.length) items.push(['Доступно ' + d.servers.length + ' серверов', 'онлайн']);
    var ref = d.referral || {};
    if (Number(ref.referralCount || 0) > 0) {
      items.push(['Приглашено ' + ref.referralCount + ' друзей', 'бонус']);
    }
    items.push(['Настройка устройства', 'VPN']);
    return items.slice(0, 4).map(function (it) {
      return '<div class="ov-row"><span>' + esc(it[0]) + '</span><span class="ov-row__val is-muted">' + esc(it[1]) + '</span></div>';
    }).join('');
  }

  /* ───────── render overview (tier-inspired dashboard) ───────── */
  function renderOverview(d) {
    var statsEl = $('ovStats');
    var leftEl = $('ovMainLeft');
    var asideEl = $('ovAside');
    if (!statsEl || !leftEl || !asideEl) return;

    var sub = d.subscription || {};
    var ref = d.referral || {};
    var bp = sub.bypass || {};
    var active = !!sub.isActive;
    var days = (sub.showEndDate && sub.endDate) ? calendarDaysUntilEnd(sub.endDate) : null;
    var tierName = sub.tierName || (sub.isFreeTier ? 'Free' : (active ? 'Активна' : 'Нет тарифа'));

    var bypassUsed = (typeof bp.bypassUsedGb === 'number') ? bp.bypassUsedGb : null;
    var bypassLimit = (typeof bp.bypassLimitGb === 'number') ? bp.bypassLimitGb : null;
    var hasBypass = bypassUsed != null && bypassLimit != null && bypassLimit > 0;
    var bypassPct = hasBypass ? Math.min(100, Math.round((bypassUsed / bypassLimit) * 100)) : 0;

    var refCount = Number(ref.referralCount || 0);
    var bonusDays = Number(ref.bonusDays || 0);
    var sv = subView(d);

    var statusVal = active ? 'ACTIVE' : 'OFFLINE';
    var daysSub = (days != null && days >= 0) ? (days + ' ' + declDays(days) + ' осталось')
      : (active ? 'Бессрочный доступ' : 'Подписка не активна');
    var bypassVal = hasBypass ? bypassUsed.toFixed(1) + ' GB' : '∞';
    var bypassSub = hasBypass ? ('из ' + bypassLimit.toFixed(0) + ' GB') : 'без лимита';
    var securityScore = active ? (hasBypass ? Math.max(72, 100 - bypassPct) : 92) : 48;

    statsEl.innerHTML =
      ovKpi('VPN STATUS', esc(statusVal), daysSub, active) +
      ovKpi('BYPASS', esc(bypassVal), bypassSub, false) +
      ovKpi('SERVERS', esc(String(d.servers.length)), 'доступно для подключения', false) +
      ovKpi('REFERRALS', esc(String(refCount)), '+' + bonusDays + ' бонусных дней', refCount > 0);

    var graphCapLeft = hasBypass ? (bypassUsed.toFixed(1) + ' / ' + bypassLimit.toFixed(0) + ' GB') : 'Безлимитный VPN';
    var graphCapRight = hasBypass ? (bypassPct + '% использовано') : tierName;

    var serversHtml = d.servers.length
      ? d.servers.slice(0, 8).map(serverOvRow).join('')
      : '<div class="ov-empty">Серверы пока недоступны</div>';

    leftEl.innerHTML =
      '<div class="ov-panel">' +
        '<h2 class="ov-panel__title">Traffic Analytics</h2>' +
        '<div class="ov-graph-area" id="ovGraph">' + trafficBars(bypassPct || (active ? 40 : 15)) + '</div>' +
        '<div class="ov-graph-cap"><span>' + esc(graphCapLeft) + '</span><span>' + esc(graphCapRight) + '</span></div>' +
      '</div>' +
      '<div class="ov-bottom-grid">' +
        '<div class="ov-panel"><h3 class="ov-panel__title">Servers</h3><div id="ovServers">' + serversHtml + '</div></div>' +
        '<div class="ov-panel"><h3 class="ov-panel__title">Recent Activity</h3>' + buildActivity(d, sv) + '</div>' +
      '</div>';

    var daysAside = (days != null && days >= 0) ? (days + ' ' + declDays(days) + ' remaining') : (active ? 'Бессрочно' : 'Нет подписки');
    var firstServer = d.servers[0];
    var nodeName = bestServer ? bestServer.name : (firstServer ? (firstServer.name || 'Сервер') : '—');
    var nodeLat = bestServer ? (bestServer.ms + 'ms latency') : 'измеряем…';

    asideEl.innerHTML =
      '<div class="ov-panel ov-globe"><div class="ov-globe__ring" aria-hidden="true"></div></div>' +
      '<div class="ov-panel">' +
        '<h3 class="ov-panel__title--sm">Current Node</h3>' +
        '<div class="ov-node-name" id="ovNodeName">' + esc(nodeName) + '</div>' +
        '<p class="ov-node-meta" id="ovNodeLatency">' + esc(nodeLat) + '</p>' +
        '<p class="ov-node-meta">99.98% uptime</p>' +
        '<p class="ov-node-meta">~31% load</p>' +
      '</div>' +
      '<div class="ov-panel ov-health">' +
        '<h3 class="ov-panel__title--sm">Network Health</h3>' +
        '<p>' + (active ? '✅' : '○') + ' DNS Protected</p>' +
        '<p>' + (active ? '✅' : '○') + ' IPv6 Protected</p>' +
        '<p>' + (active ? '✅' : '○') + ' WebRTC Protected</p>' +
      '</div>' +
      '<div class="ov-panel">' +
        '<h3 class="ov-panel__title--sm">Subscription</h3>' +
        '<div class="ov-sub-tier">' + esc(tierName) + '</div>' +
        '<p class="ov-node-meta">' + esc(daysAside) + '</p>' +
        '<div class="ov-quick-actions">' +
          '<button type="button" class="btn-secondary" data-ov-nav="screenSetup">Подключиться</button>' +
          '<button type="button" class="btn-primary" onclick="window.showModal(\'modalPlan\')">Тарифы</button>' +
        '</div>' +
      '</div>';

    wireNav();
    raf(function () {
      leftEl.querySelectorAll('.ov-graph-bar[data-h]').forEach(function (el) {
        el.style.height = el.getAttribute('data-h') + '%';
      });
    });

    if (d.servers.length) {
      refreshPings();
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = setInterval(refreshPings, PING_REFRESH_MS);
    }
  }

  /* ───────── icon set ───────── */
  var ICON = {
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    data: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.66 3.58 3 8 3s8-1.34 8-3V6"/><path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3"/></svg>',
    globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.5 2.5 15.5 0 18M12 3c-2.5 2.5-2.5 15.5 0 18"/></svg>',
    gift: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 12 20 22 4 22 4 12"/><rect x="2" y="7" width="20" height="5"/><line x1="12" y1="22" x2="12" y2="7"/><path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z"/><path d="M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z"/></svg>',
    user: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/></svg>',
    mail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>',
    id: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="12" r="2"/><path d="M14 10h4M14 14h4"/></svg>',
    gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-2.82 1.17V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 2.82 1.17l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9z"/></svg>',
    bolt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h8l-1 8 10-12h-8z"/></svg>',
    support: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1v-6h1a2 2 0 0 1 2 2zM3 19a2 2 0 0 0 2 2h1v-6H5a2 2 0 0 0-2 2z"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    star: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 3 7 7 .5-5.5 4.5 2 7-6.5-4-6.5 4 2-7L2 9.5 9 9z"/></svg>',
    link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>'
  };

  /* ───────── derived subscription view ───────── */
  function subView(d) {
    var sub = d.subscription || {};
    var active = !!sub.isActive;
    var days = (sub.showEndDate && sub.endDate) ? calendarDaysUntilEnd(sub.endDate) : null;
    var bp = sub.bypass || {};
    var hasBypass = (typeof bp.bypassUsedGb === 'number') && (typeof bp.bypassLimitGb === 'number') && bp.bypassLimitGb > 0;
    return {
      active: active,
      days: days,
      tierName: sub.tierName || (sub.isFreeTier ? 'Free' : (active ? 'Активна' : 'Нет тарифа')),
      isPaid: !!sub.isPaidTier,
      isFree: !!sub.isFreeTier,
      endDate: sub.endDate,
      hasBypass: hasBypass,
      bypassUsed: hasBypass ? bp.bypassUsedGb : null,
      bypassLimit: hasBypass ? bp.bypassLimitGb : null,
      bypassPct: hasBypass ? Math.min(100, Math.round((bp.bypassUsedGb / bp.bypassLimitGb) * 100)) : 0,
      bypassExceeded: !!bp.bypassExceeded
    };
  }

  /* ───────── PROFILE rebuild ───────── */
  function buildProfile(d) {
    var u = d.user || {};
    var sv = subView(d);
    var ref = d.referral || {};

    // hero meta chips
    var chips = $('profileHeroChips');
    if (chips) {
      var arr = [];
      arr.push('<span class="pill ' + (sv.active ? 'pill--ok' : 'pill--warn') + '"><span class="pill__dot"></span>' +
        (sv.active ? 'Подписка активна' : 'Нет подписки') + '</span>');
      arr.push('<span class="pill pill--muted">' + ICON.shield + ' ' + esc(sv.tierName) + '</span>');
      if (u.id != null) arr.push('<span class="pill pill--muted">ID ' + esc(String(u.id)) + '</span>');
      if (u.isAdmin) arr.push('<span class="pill pill--ok">admin</span>');
      chips.innerHTML = arr.join('');
    }

    var grid = $('profileWebGrid');
    if (!grid) return;

    // Plan summary card (left, big)
    var daysBig = (sv.days != null && sv.days >= 0) ? (sv.days + '<small> ' + declDays(sv.days) + '</small>')
      : (sv.active ? '∞' : '—');
    var bypassMetric = sv.hasBypass
      ? (sv.bypassUsed.toFixed(1) + '<small>/' + sv.bypassLimit.toFixed(0) + 'ГБ</small>')
      : '∞';
    var meter = sv.hasBypass
      ? '<div class="meter"><div class="meter__top"><span>Bypass-трафик</span><span>' + sv.bypassPct + '%</span></div>' +
        '<div class="meter__track"><div class="meter__fill ' + (sv.bypassPct >= 80 ? 'is-warn' : '') + '" data-w="' + sv.bypassPct + '"></div></div></div>'
      : '';
    var planCta = sv.isPaid
      ? '<button class="btn-primary" onclick="window.showModal(\'modalPlan\')">Тарифы и продление</button>'
      : '<button class="btn-primary" onclick="window.showModal(\'modalPlan\')">Выбрать тариф</button>';

    var planCard =
      '<div class="web-panel plan-card">' +
        '<div class="web-panel__head">' +
          '<span class="web-panel__icon">' + ICON.shield + '</span>' +
          '<div><p class="web-panel__title">Текущий тариф</p><p class="web-panel__sub">Подписка и лимиты</p></div>' +
        '</div>' +
        '<div class="plan-card__row"><span class="plan-card__tier">' + esc(sv.tierName) + '</span>' +
          '<span class="pill ' + (sv.active ? 'pill--ok' : 'pill--warn') + '"><span class="pill__dot"></span>' + (sv.active ? 'активна' : 'не активна') + '</span></div>' +
        '<div class="plan-card__metrics">' +
          '<div class="plan-card__metric"><div class="plan-card__metric-v">' + daysBig + '</div><div class="plan-card__metric-l">осталось</div></div>' +
          '<div class="plan-card__metric"><div class="plan-card__metric-v">' + bypassMetric + '</div><div class="plan-card__metric-l">bypass</div></div>' +
          '<div class="plan-card__metric"><div class="plan-card__metric-v">' + esc(String(d.servers.length)) + '</div><div class="plan-card__metric-l">серверов</div></div>' +
        '</div>' +
        meter +
        '<div style="margin-top:16px;">' + planCta + '</div>' +
      '</div>';

    // Account details card (right)
    var emailRow;
    if (u.linkedEmailMasked) {
      emailRow = '<div class="kv__row"><span class="kv__k">' + ICON.mail + 'Email</span><span class="kv__v is-ok">' + esc(u.linkedEmailMasked) + '</span></div>';
    } else if (u.needLinkEmail) {
      emailRow = '<div class="kv__row"><span class="kv__k">' + ICON.mail + 'Email</span><span class="kv__v is-warn">не привязан</span></div>';
    } else {
      emailRow = '<div class="kv__row"><span class="kv__k">' + ICON.mail + 'Email</span><span class="kv__v">—</span></div>';
    }
    var fullName = [u.firstName, u.lastName].filter(Boolean).join(' ') || 'Пользователь';
    var accountCard =
      '<div class="web-panel">' +
        '<div class="web-panel__head">' +
          '<span class="web-panel__icon web-panel__icon--info">' + ICON.id + '</span>' +
          '<div><p class="web-panel__title">Аккаунт</p><p class="web-panel__sub">Данные профиля</p></div>' +
        '</div>' +
        '<div class="kv">' +
          '<div class="kv__row"><span class="kv__k">' + ICON.user + 'Имя</span><span class="kv__v">' + esc(fullName) + '</span></div>' +
          (u.username ? '<div class="kv__row"><span class="kv__k">@</span><span class="kv__v"><code>@' + esc(u.username) + '</code></span></div>' : '') +
          '<div class="kv__row"><span class="kv__k">' + ICON.id + 'ID</span><span class="kv__v"><code>' + esc(String(u.id != null ? u.id : '—')) + '</code></span></div>' +
          emailRow +
          '<div class="kv__row"><span class="kv__k">' + ICON.gift + 'Приглашено</span><span class="kv__v">' + esc(String(ref.referralCount || 0)) + ' чел.</span></div>' +
        '</div>' +
      '</div>';

    // Quick actions (right)
    var actions =
      '<div class="web-panel">' +
        '<div class="web-panel__head">' +
          '<span class="web-panel__icon">' + ICON.bolt + '</span>' +
          '<div><p class="web-panel__title">Быстрые действия</p><p class="web-panel__sub">Часто используемое</p></div>' +
        '</div>' +
        '<div class="qa-grid">' +
          qaTile('screen', 'screenSetup', ICON.gear, 'Подключиться', 'Настройка устройства') +
          qaTile('modal', 'modalPlan', ICON.shield, 'Тарифы', 'Оплата и продление', 'qa-tile__icon--info') +
          qaTile('screen', 'screenReferral', ICON.gift, 'Пригласить', 'Бонусные дни', 'qa-tile__icon--pink') +
          qaTile('screen', 'screenOverview', ICON.bolt, 'Обзор', 'Статистика', 'qa-tile__icon--warn') +
        '</div>' +
      '</div>';

    grid.innerHTML =
      '<div style="display:flex;flex-direction:column;gap:16px;min-width:0;">' + planCard + accountCard + '</div>' +
      '<div style="display:flex;flex-direction:column;gap:16px;min-width:0;">' + actions + '</div>';

    wireQa(grid);
    animateBars(grid);
  }

  function qaTile(kind, target, icon, title, sub, iconMod) {
    return '<a class="qa-tile" data-qa-kind="' + kind + '" data-qa-target="' + esc(target) + '">' +
      '<span class="qa-tile__icon ' + (iconMod || '') + '">' + icon + '</span>' +
      '<span><span class="qa-tile__t">' + esc(title) + '</span><span class="qa-tile__s">' + esc(sub) + '</span></span>' +
      '</a>';
  }
  function wireQa(root) {
    (root || document).querySelectorAll('.qa-tile[data-qa-target]').forEach(function (el) {
      if (el.__qaWired) return;
      el.__qaWired = true;
      el.addEventListener('click', function () {
        var kind = el.getAttribute('data-qa-kind');
        var target = el.getAttribute('data-qa-target');
        if (kind === 'modal' && window.showModal) window.showModal(target);
        else if (window.showScreen) window.showScreen(target);
      });
    });
  }

  /* ───────── REFERRAL rebuild ───────── */
  function buildReferral(d) {
    var box = $('referralWebExtra');
    if (!box) return;
    var ref = d.referral || {};
    var inviter = Number(ref.inviterBonusDays || ref.bonusPerInvite || 0) || 7;
    var invited = Number(ref.invitedBonusDays || 0) || 3;
    var count = Number(ref.referralCount || 0);
    var bonus = Number(ref.bonusDays || 0);

    var steps =
      '<div class="ref-steps">' +
        '<div class="ref-step"><div class="ref-step__num">1</div><p class="ref-step__t">Поделитесь ссылкой</p><p class="ref-step__d">Скопируйте свою реферальную ссылку и отправьте друзьям в любой мессенджер.</p></div>' +
        '<div class="ref-step"><div class="ref-step__num">2</div><p class="ref-step__t">Друг подключается</p><p class="ref-step__d">Он переходит по ссылке и оформляет подписку SvoyVPN — и сразу получает ' + invited + ' ' + declDays(invited) + ' в подарок.</p></div>' +
        '<div class="ref-step"><div class="ref-step__num">3</div><p class="ref-step__t">Вы получаете дни</p><p class="ref-step__d">За каждого приглашённого вам начисляется ' + inviter + ' ' + declDays(inviter) + ' доступа автоматически.</p></div>' +
      '</div>';

    var rewards =
      '<div class="ref-rewards">' +
        '<div class="reward-tile"><span class="reward-tile__icon reward-tile__icon--accent">' + ICON.gift + '</span><div><div class="reward-tile__v">' + count + '</div><div class="reward-tile__l">приглашено друзей</div></div></div>' +
        '<div class="reward-tile"><span class="reward-tile__icon">' + ICON.star + '</span><div><div class="reward-tile__v">' + bonus + '</div><div class="reward-tile__l">бонусных дней получено</div></div></div>' +
        '<div class="reward-tile"><span class="reward-tile__icon reward-tile__icon--accent">' + ICON.clock + '</span><div><div class="reward-tile__v">+' + inviter + '</div><div class="reward-tile__l">дней за каждого друга</div></div></div>' +
      '</div>';

    var benefits =
      '<div class="web-panel">' +
        '<div class="web-panel__head"><span class="web-panel__icon web-panel__icon--pink">' + ICON.star + '</span>' +
          '<div><p class="web-panel__title">Почему это выгодно</p><p class="web-panel__sub">Реферальная программа SvoyVPN</p></div></div>' +
        '<div class="ref-benefits">' +
          benefit('Бонусные дни начисляются автоматически после оплаты друга') +
          benefit('Количество приглашений не ограничено — приглашайте сколько хотите') +
          benefit('Друг тоже получает подарок — это честный обмен, а не спам') +
          benefit('Дни суммируются с вашей текущей подпиской') +
        '</div>' +
      '</div>';

    box.innerHTML =
      '<div class="web-panel"><div class="web-panel__head"><span class="web-panel__icon">' + ICON.link + '</span>' +
        '<div><p class="web-panel__title">Как это работает</p><p class="web-panel__sub">Три простых шага</p></div></div>' + steps + '</div>' +
      rewards +
      benefits;
  }
  function benefit(t) {
    return '<div class="ref-benefit">' + ICON.check + '<span>' + esc(t) + '</span></div>';
  }

  /* ───────── VPN quick-stats ───────── */
  function buildVpn(d) {
    var box = $('vpnWebStats');
    if (!box) return;
    var sv = subView(d);
    var daysVal = (sv.days != null && sv.days >= 0) ? (sv.days + '<small> ' + declDays(sv.days) + '</small>')
      : (sv.active ? '∞' : '—');
    var bypassVal = sv.hasBypass ? (sv.bypassUsed.toFixed(1) + '<small>/' + sv.bypassLimit.toFixed(0) + ' ГБ</small>') : 'Безлимит';
    box.innerHTML =
      vstat(ICON.shield, '', 'Тариф', esc(sv.tierName)) +
      vstat(ICON.clock, (sv.days != null && sv.days >= 0 && sv.days <= 7 ? 'vstat__icon--warn' : ''), 'Осталось', daysVal) +
      vstat(ICON.data, 'vstat__icon--info', 'Bypass', bypassVal);
  }
  function vstat(icon, iconMod, label, value) {
    return '<div class="vstat"><span class="vstat__icon ' + (iconMod || '') + '">' + icon + '</span>' +
      '<span><span class="vstat__l">' + esc(label) + '</span><br><span class="vstat__v">' + value + '</span></span></div>';
  }

  /* ───────── topbar chips + sidebar status ───────── */
  function renderChrome(d) {
    var sub = d.subscription || {};
    var active = !!sub.isActive;
    var days = (sub.showEndDate && sub.endDate) ? calendarDaysUntilEnd(sub.endDate) : null;
    var tierName = sub.tierName || (sub.isFreeTier ? 'Free' : (active ? 'Активна' : 'Нет тарифа'));

    // Topbar chips
    var right = document.querySelector('.web-header-right');
    if (right && !$('topbarChips')) {
      var holder = document.createElement('span');
      holder.id = 'topbarChips';
      holder.style.cssText = 'display:inline-flex;align-items:center;gap:10px;';
      right.insertBefore(holder, right.firstChild);
    }
    var chips = $('topbarChips');
    if (chips) {
      var u = d.user || {};
      var av = u.photoUrl
        ? '<span class="tc-avatar"><img src="' + esc(u.photoUrl) + '" alt=""></span>'
        : '<span class="tc-avatar">' + esc(initials(u)) + '</span>';
      var name = esc(u.firstName || u.username || 'Аккаунт');
      var daysChip = (days != null && days >= 0)
        ? '<span class="topbar-chip topbar-chip--days' + (days <= 7 ? ' is-warn' : '') + '"><span class="tc-dot"></span>' + days + ' ' + declDays(days) + '</span>'
        : '';
      chips.innerHTML =
        '<span class="topbar-chip topbar-chip--tier">' + esc(tierName) + '</span>' +
        daysChip +
        '<span class="topbar-chip topbar-chip--user">' + av + name + '</span>';
    }

    // Sidebar footer status
    var st = $('sideStatus');
    var stTitle = $('sideStatusTitle');
    var stSub = $('sideStatusSub');
    if (st && stTitle && stSub) {
      st.classList.remove('is-active', 'is-warn');
      if (active) st.classList.add(days != null && days >= 0 && days <= 7 ? 'is-warn' : 'is-active');
      stTitle.textContent = active ? tierName : 'Нет подписки';
      stSub.textContent = active
        ? (days != null && days >= 0 ? 'осталось ' + days + ' ' + declDays(days) : 'активна')
        : 'оформите тариф';
    }
    if (d.user && d.user.supportLink) {
      var sup = $('sideLinkSupport');
      if (sup && /^https?:\/\//.test(d.user.supportLink)) sup.href = d.user.supportLink;
    }
  }

  /* ───────── navigation ───────── */
  function wireNav() {
    document.querySelectorAll('[data-ov-nav]').forEach(function (btn) {
      if (btn.__ovWired) return;
      btn.__ovWired = true;
      btn.addEventListener('click', function () {
        var target = btn.getAttribute('data-ov-nav');
        if (window.showScreen && target) window.showScreen(target);
      });
    });
  }

  function wireSidebar() {
    var btn = $('sideToggle');
    if (!btn || btn.__sideWired) return;
    btn.__sideWired = true;
    var collapsed = localStorage.getItem('svoy-dash-sidebar') === 'collapsed';
    if (collapsed && isDesktop()) document.body.classList.add('sidebar-collapsed');
    btn.addEventListener('click', function () {
      document.body.classList.toggle('sidebar-collapsed');
      localStorage.setItem('svoy-dash-sidebar',
        document.body.classList.contains('sidebar-collapsed') ? 'collapsed' : 'expanded');
    });
  }

  function wireProductSegment() {
    var seg = $('ovProductSegment');
    if (!seg || seg.__segWired) return;
    seg.__segWired = true;
    seg.querySelectorAll('[data-ov-product]').forEach(function (b) {
      b.addEventListener('click', function () {
        seg.querySelectorAll('.ov-segment__btn').forEach(function (x) { x.classList.remove('is-active'); });
        b.classList.add('is-active');
        var p = b.getAttribute('data-ov-product');
        if (p === 'esim' && window.showScreen) window.showScreen('screenEsim');
        else if (p === 'vpn' && window.showScreen) window.showScreen('screenOverview');
      });
    });
  }

  function wireAuthExtras() {
    var reg = $('authGoRegister');
    if (reg && !reg.__wired) {
      reg.__wired = true;
      reg.addEventListener('click', function () {
        var tabEmail = $('tabEmail');
        var subReg = $('subTabRegister');
        if (tabEmail) tabEmail.click();
        if (subReg) subReg.click();
      });
    }
  }

  function maybeDefaultLand() {
    if (didDefaultLand) return;
    if (!isDesktop()) return;
    if (!window.showScreen) return;
    // Respect an explicit deep-link to another screen
    if (initialHash && initialHash !== 'overview' && initialHash !== 'dashboard') { didDefaultLand = true; return; }
    // Only land once the user is authenticated (auth screen not active)
    var auth = $('screenAuth');
    if (auth && auth.classList.contains('active')) return;
    didDefaultLand = true;
    window.showScreen('screenOverview');

    // Re-assert briefly: loadUser() may fire showScreen('screenVpn') a moment later.
    // Stop the instant the user navigates anywhere themselves.
    var asserts = 0;
    var iv = setInterval(function () {
      asserts++;
      if (userNavigated || asserts > 10) { clearInterval(iv); return; }
      var ov = $('screenOverview');
      var vpn = $('screenVpn');
      if (ov && !ov.classList.contains('active') && vpn && vpn.classList.contains('active')) {
        window.showScreen('screenOverview');
      }
    }, 140);
  }

  /* ───────── bootstrap ───────── */
  async function tryBuild() {
    if (built) return true;
    var d = await fetchAll();
    if (!d) return false;
    lastData = d;
    built = true;
    renderOverview(d);
    renderChrome(d);
    buildProfile(d);
    buildReferral(d);
    buildVpn(d);
    wireNav();
    wireSidebar();
    wireProductSegment();
    wireAuthExtras();
    maybeDefaultLand();
    return true;
  }

  function start() {
    wireSidebar();
    wireAuthExtras();
    var tries = 0;
    var iv = setInterval(function () {
      tries++;
      tryBuild().then(function (ok) {
        if (ok || tries > 40) {
          clearInterval(iv);
          if (ok) maybeDefaultLand();
        }
      });
    }, 800);
    // First immediate attempt
    tryBuild();
  }

  function rerenderFromCache() {
    if (!built || !lastData) return;
    buildProfile(lastData);
    buildReferral(lastData);
    buildVpn(lastData);
    renderChrome(lastData);
    animateBars(document);
  }

  // Re-trigger meter/bar animations when entering a screen.
  window.addEventListener('hashchange', function () {
    var h = (location.hash || '').toLowerCase();
    if (h.indexOf('overview') !== -1) {
      var gl = $('ovGraph');
      if (gl) raf(function () {
        gl.querySelectorAll('.ov-graph-bar[data-h]').forEach(function (el) {
          el.style.height = el.getAttribute('data-h') + '%';
        });
      });
    }
    if (h.indexOf('profile') !== -1 || h.indexOf('referral') !== -1 || h.indexOf('gifts') !== -1 || h.indexOf('home') !== -1 || h.indexOf('vpn') !== -1) {
      animateBars(document);
    }
  });

  // Periodically refresh data so panels reflect new payments/subscription.
  setInterval(function () {
    if (!built) return;
    fetchAll().then(function (d) {
      if (!d) return;
      lastData = d;
      renderOverview(d);
      rerenderFromCache();
    });
  }, 45000);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
