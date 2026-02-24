/* SvoyVPN miniapp – landing page style, using Need CSS + SvoyVPN bot API (/api/*). */
(function () {
  /** @type {any} */
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

  const state = {
    user: null,
    subscription: null,
    tariffs: /** @type {Array<any>} */ ([]),
    paymentMethods: /** @type {Array<any>} */ ([]),
    selectedTariffId: /** @type {string|null} */ (null),
    selectedPaymentMethodId: /** @type {string|null} */ (null),
  };

  const el = {
    spriteHost: /** @type {HTMLElement|null} */ (null),
    plansWrapper: /** @type {HTMLElement|null} */ (null),
    paymentMethodsWrapper: /** @type {HTMLElement|null} */ (null),
    totalPrice: /** @type {HTMLElement|null} */ (null),
    btnPay: /** @type {HTMLButtonElement|null} */ (null),
    btnCta: /** @type {HTMLButtonElement|null} */ (null),
    subscriptionUrl: /** @type {HTMLInputElement|null} */ (null),
    btnCopySub: /** @type {HTMLElement|null} */ (null),
    profileName: /** @type {HTMLElement|null} */ (null),
    profileStatus: /** @type {HTMLElement|null} */ (null),
    profileUntil: /** @type {HTMLElement|null} */ (null),
  };

  /* ── Theme ── */

  function setThemeFromTelegram() {
    const scheme = tg && tg.colorScheme ? tg.colorScheme : null;
    document.documentElement.setAttribute("data-theme", scheme === "dark" ? "dark" : "light");
  }

  /* ── SVG Sprite ── */

  async function injectSprite() {
    try {
      const res = await fetch("/miniapp/need/assets/sprite.svg", { cache: "force-cache" });
      if (!res.ok) return;
      let text = await res.text();
      text = text.replace(/^<\?xml[^>]*>\s*/i, "");
      if (el.spriteHost) el.spriteHost.innerHTML = text;
    } catch {
      // ignore
    }
  }

  /* ── Formatters ── */

  function formatRub(value) {
    try {
      return new Intl.NumberFormat("ru-RU", { style: "currency", currency: "RUB", maximumFractionDigits: 0 }).format(
        value
      );
    } catch {
      return `${Math.round(value)} ₽`;
    }
  }

  function formatDateIso(dateIso) {
    if (!dateIso) return "";
    try {
      const d = new Date(dateIso);
      return d.toLocaleDateString("ru-RU");
    } catch {
      return String(dateIso);
    }
  }

  /* ── API helper ── */

  async function apiGetJson(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`;
      try {
        const data = await res.json();
        if (data && (data.error || data.message)) msg = data.error || data.message;
      } catch {
        // ignore
      }
      throw new Error(msg);
    }
    return res.json();
  }

  /* ── Data loading ── */

  async function loadUser() {
    if (!tg || !tg.initData) return;
    try {
      const data = await apiGetJson("/api/user", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initData: tg.initData }),
      });
      state.user = data.user || null;
      state.subscription = data.subscription || null;
      renderProfile();
      renderSubscriptionUrl();
    } catch {
      renderProfile();
    }
  }

  async function loadTariffs() {
    try {
      const tariffs = await apiGetJson("/api/tariffs");
      state.tariffs = Array.isArray(tariffs) ? tariffs : [];
      if (!state.selectedTariffId && state.tariffs.length) {
        state.selectedTariffId = state.tariffs.find((t) => t.popular)?.id || state.tariffs[0].id;
      }
      renderTariffs();
      updateTotal();
    } catch {
      // ignore
    }
  }

  async function loadPaymentMethods() {
    try {
      const methods = await apiGetJson("/api/payment-methods");
      state.paymentMethods = Array.isArray(methods) ? methods : [];
      if (!state.selectedPaymentMethodId && state.paymentMethods.length) {
        state.selectedPaymentMethodId = state.paymentMethods[0].id;
      }
      renderPaymentMethods();
    } catch {
      // ignore
    }
  }

  /* ── Rendering ── */

  function renderTariffs() {
    if (!el.plansWrapper) return;
    el.plansWrapper.innerHTML = "";

    for (const t of state.tariffs) {
      const card = document.createElement("div");
      card.className = `_plan-card_q2m4m_12${t.id === state.selectedTariffId ? " _is-active_q2m4m_54" : ""}`;
      card.setAttribute("role", "button");
      card.tabIndex = 0;
      card.addEventListener("click", () => {
        state.selectedTariffId = t.id;
        renderTariffs();
        updateTotal();
      });

      const monthTitle = document.createElement("div");
      monthTitle.className = "_plan-card-section_q2m4m_65";
      const monthWord = t.months === 1 ? "месяц" : t.months >= 2 && t.months <= 4 ? "месяца" : "месяцев";
      monthTitle.innerHTML = `<div class="_month-count-title_q2m4m_58">${t.months}</div>
        <div class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 _weight_medium_1hgcm_75 svoy-muted">${monthWord}</div>`;

      const price = document.createElement("div");
      price.className = "_plan-card-section_q2m4m_65";
      const total = Number(t.price || 0);
      const perMonth = t.pricePerMonth ? Number(t.pricePerMonth) : total / Math.max(1, Number(t.months || 1));
      price.innerHTML = `<div class="_root_1hgcm_29 _size_headline_1hgcm_47 _weight_semibold_1hgcm_78">${formatRub(
        total
      )}</div>
        <div class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 _weight_regular_1hgcm_72 svoy-muted">${formatRub(
          perMonth
        )} / мес</div>`;

      if (t.popular) {
        const badge = document.createElement("div");
        badge.className = "_badge_q2m4m_72";
        badge.innerHTML = `<svg viewBox="0 0 40 40" aria-hidden="true" style="width:100%;height:100%;"><use href="#stars.static"></use></svg>`;
        card.appendChild(badge);
      }

      card.appendChild(monthTitle);
      card.appendChild(price);
      el.plansWrapper.appendChild(card);
    }
  }

  function renderPaymentMethods() {
    if (!el.paymentMethodsWrapper) return;
    el.paymentMethodsWrapper.innerHTML = "";

    for (const m of state.paymentMethods) {
      const row = document.createElement("div");
      row.className = `_cell-provider_mdt1z_85${m.id === state.selectedPaymentMethodId ? " _is-active" : ""}`;
      row.setAttribute("role", "button");
      row.tabIndex = 0;
      row.addEventListener("click", () => {
        state.selectedPaymentMethodId = m.id;
        renderPaymentMethods();
        updateTotal();
      });

      const left = document.createElement("div");
      left.className = "_cell-provider-inner_mdt1z_101";
      left.innerHTML = `<div class="_title-row_mdt1z_116">
          <p class="_root_1hgcm_29 _size_subtitle1_1hgcm_55 _weight_semibold_1hgcm_78">${m.name || m.id}</p>
          ${m.badge ? `<span class="_badge_mdt1z_122"><span class="_root_1hgcm_29 _size_subtitle3_1hgcm_63" style="color: var(--button_text_color);">${m.badge}</span></span>` : ""}
        </div>
        ${m.description ? `<p class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 svoy-muted">${m.description}</p>` : ""}`;

      const check = document.createElement("div");
      check.className = "_cell-checkmark_mdt1z_107";
      check.innerHTML =
        m.id === state.selectedPaymentMethodId
          ? `<svg class="svoy-icon" viewBox="0 0 16 28" aria-hidden="true"><use href="#checkmark"></use></svg>`
          : "";

      row.appendChild(left);
      row.appendChild(check);
      el.paymentMethodsWrapper.appendChild(row);
    }
  }

  function updateTotal() {
    const tariff = state.tariffs.find((t) => t.id === state.selectedTariffId);
    const total = tariff ? Number(tariff.price || 0) : 0;
    if (el.totalPrice) el.totalPrice.textContent = formatRub(total);

    if (el.btnPay) {
      el.btnPay.disabled = !state.selectedTariffId || !state.selectedPaymentMethodId;
      el.btnPay.classList.toggle("_is-disabled_iw2y8_58", el.btnPay.disabled);
    }
  }

  function renderSubscriptionUrl() {
    const url = state.subscription && state.subscription.subscriptionUrl ? state.subscription.subscriptionUrl : "";
    if (el.subscriptionUrl) el.subscriptionUrl.value = url;
  }

  function renderProfile() {
    const user = state.user;
    const sub = state.subscription;

    if (el.profileName) el.profileName.textContent = user?.firstName || user?.username || "User";

    if (sub) {
      if (el.profileStatus) {
        el.profileStatus.textContent = sub.isActive ? "Активна" : "Неактивна";
        el.profileStatus.style.color = sub.isActive ? "var(--accent_text_color)" : "var(--subtitle_text_color)";
      }
      if (el.profileUntil) {
        el.profileUntil.textContent = sub.endDate ? `до ${formatDateIso(sub.endDate)}` : "";
      }
    } else {
      if (el.profileStatus) el.profileStatus.textContent = "Нет подписки";
      if (el.profileUntil) el.profileUntil.textContent = "";
    }
  }

  /* ── Clipboard ── */

  async function copyText(text) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      tg?.HapticFeedback?.notificationOccurred?.("success");
      tg?.showAlert?.("Скопировано");
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      tg?.showAlert?.("Скопировано");
    }
  }

  /* ── Payment ── */

  async function createPayment() {
    if (!state.selectedTariffId || !state.selectedPaymentMethodId) return;

    if (tg) tg.HapticFeedback?.impactOccurred?.("medium");

    try {
      const data = await apiGetJson("/api/payment/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          initData: tg ? tg.initData : "",
          tariffId: state.selectedTariffId,
          paymentMethod: state.selectedPaymentMethodId,
        }),
      });

      if (state.selectedPaymentMethodId === "stars" && data.invoiceUrl) {
        tg?.openTelegramLink?.(data.invoiceUrl);
        return;
      }
      if (data.paymentUrl) {
        tg?.openLink?.(data.paymentUrl);
        return;
      }

      tg?.showAlert?.("Платёж создан");
    } catch (e) {
      tg?.showAlert?.(`Ошибка оплаты: ${e && e.message ? e.message : "unknown"}`);
    }
  }

  /* ── DOM binding ── */

  function bindDom() {
    el.spriteHost = document.getElementById("svgSpriteHost");
    el.plansWrapper = document.getElementById("plansWrapper");
    el.paymentMethodsWrapper = document.getElementById("paymentMethodsWrapper");
    el.totalPrice = document.getElementById("totalPrice");
    el.btnPay = /** @type {HTMLButtonElement|null} */ (document.getElementById("btnPay"));
    el.btnCta = /** @type {HTMLButtonElement|null} */ (document.getElementById("btnCta"));
    el.subscriptionUrl = /** @type {HTMLInputElement|null} */ (document.getElementById("subscriptionUrl"));
    el.btnCopySub = document.getElementById("btnCopySub");
    el.profileName = document.getElementById("profileName");
    el.profileStatus = document.getElementById("profileStatus");
    el.profileUntil = document.getElementById("profileUntil");

    // CTA scrolls to tariff section
    el.btnCta?.addEventListener("click", () => {
      tg?.HapticFeedback?.impactOccurred?.("medium");
      const target = document.getElementById("sectionTariffs");
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    el.btnPay?.addEventListener("click", createPayment);
    el.btnCopySub?.addEventListener("click", () => copyText(el.subscriptionUrl?.value || ""));
  }

  /* ── Init ── */

  document.addEventListener("DOMContentLoaded", async () => {
    bindDom();
    injectSprite();

    if (tg) {
      try {
        tg.ready();
        tg.expand();
      } catch {
        // ignore
      }
    }

    setThemeFromTelegram();

    await Promise.allSettled([loadTariffs(), loadPaymentMethods(), loadUser()]);
    updateTotal();
  });
})();
