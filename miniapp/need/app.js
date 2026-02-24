/* SvoyVPN miniapp UI styled with the Need example CSS, but using SvoyVPN bot API (/api/*). */
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
    deviceCount: 1,
  };

  const el = {
    spriteHost: /** @type {HTMLElement|null} */ (null),
    tabPlans: /** @type {HTMLElement|null} */ (null),
    tabSetup: /** @type {HTMLElement|null} */ (null),
    tabProfile: /** @type {HTMLElement|null} */ (null),
    plansWrapper: /** @type {HTMLElement|null} */ (null),
    paymentMethodsWrapper: /** @type {HTMLElement|null} */ (null),
    deviceRange: /** @type {HTMLInputElement|null} */ (null),
    deviceCount: /** @type {HTMLElement|null} */ (null),
    totalPrice: /** @type {HTMLElement|null} */ (null),
    btnPay: /** @type {HTMLButtonElement|null} */ (null),
    subscriptionUrl: /** @type {HTMLInputElement|null} */ (null),
    btnCopySub: /** @type {HTMLElement|null} */ (null),
    profileName: /** @type {HTMLElement|null} */ (null),
    profileAvatar: /** @type {HTMLElement|null} */ (null),
    profileStatus: /** @type {HTMLElement|null} */ (null),
    profileUntil: /** @type {HTMLElement|null} */ (null),
    btnRefresh: /** @type {HTMLElement|null} */ (null),
  };

  function setThemeFromTelegram() {
    const scheme = tg && tg.colorScheme ? tg.colorScheme : null;
    document.body.setAttribute("data-theme", scheme === "dark" ? "dark" : "light");
  }

  async function injectSprite() {
    try {
      const res = await fetch("/miniapp/need/assets/sprite.svg", { cache: "force-cache" });
      if (!res.ok) return;
      let text = await res.text();
      // XML header can break HTML parsing in some contexts.
      text = text.replace(/^<\\?xml[^>]*>\\s*/i, "");
      if (el.spriteHost) el.spriteHost.innerHTML = text;
    } catch {
      // ignore
    }
  }

  function showTab(tabName) {
    const tabs = [
      { name: "plans", el: el.tabPlans },
      { name: "setup", el: el.tabSetup },
      { name: "profile", el: el.tabProfile },
    ];
    for (const t of tabs) {
      if (!t.el) continue;
      t.el.classList.toggle("svoy-tab--active", t.name === tabName);
    }

    document.querySelectorAll("[data-tab-button]").forEach((btn) => {
      const name = btn.getAttribute("data-tab-button");
      const isActive = name === tabName;
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
      const svg = btn.querySelector("svg");
      if (svg) svg.style.color = `var(${isActive ? "--accent_text_color" : "--subtitle_text_color"})`;
      const label = btn.querySelector("p");
      if (label) label.style.color = `var(${isActive ? "--accent_text_color" : "--subtitle_text_color"})`;
    });
  }

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
    } catch (e) {
      // For first load, don't spam. Still show something in profile.
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
      monthTitle.innerHTML = `<div class="_month-count-title_q2m4m_58">${t.months}</div>
        <div class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 _weight_medium_1hgcm_75 svoy-muted">${t.months === 1 ? "month" : "months"}</div>`;

      const price = document.createElement("div");
      price.className = "_plan-card-section_q2m4m_65";
      const total = Number(t.price || 0);
      const perMonth = t.pricePerMonth ? Number(t.pricePerMonth) : total / Math.max(1, Number(t.months || 1));
      price.innerHTML = `<div class="_root_1hgcm_29 _size_headline_1hgcm_47 _weight_semibold_1hgcm_78">${formatRub(
        total
      )}</div>
        <div class="_root_1hgcm_29 _size_subtitle2_1hgcm_59 _weight_regular_1hgcm_72 svoy-muted">${formatRub(
          perMonth
        )} / mo</div>`;

      if (t.popular) {
        const badge = document.createElement("div");
        badge.className = "_badge_q2m4m_72";
        badge.innerHTML = `<svg class="svoy-icon" viewBox="0 0 40 40" aria-hidden="true"><use href="#stars.static"></use></svg>`;
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
      });

      const left = document.createElement("div");
      left.className = "_cell-provider-inner_mdt1z_101";
      left.innerHTML = `<div class="_title-row_mdt1z_116">
          <p class="_root_1hgcm_29 _size_subtitle1_1hgcm_55 _weight_semibold_1hgcm_78">${m.name || m.id}</p>
          ${m.badge ? `<span class="_badge_mdt1z_122">${m.badge}</span>` : ""}
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
    const total = (tariff ? Number(tariff.price || 0) : 0) * Math.max(1, Number(state.deviceCount || 1));
    if (el.totalPrice) el.totalPrice.textContent = formatRub(total);

    if (el.btnPay) {
      el.btnPay.disabled = !state.selectedTariffId || !state.selectedPaymentMethodId;
      el.btnPay.classList.toggle("_is-disabled_iw2y8_58", el.btnPay.disabled);
    }
  }

  async function copyText(text) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      tg?.HapticFeedback?.notificationOccurred?.("success");
      tg?.showAlert?.("Скопировано");
    } catch {
      // fallback
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      tg?.showAlert?.("Скопировано");
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
    if (el.profileAvatar) {
      if (user?.photoUrl) {
        el.profileAvatar.style.backgroundImage = `url(${user.photoUrl})`;
        el.profileAvatar.style.backgroundSize = "cover";
        el.profileAvatar.style.backgroundPosition = "center";
      } else {
        el.profileAvatar.style.backgroundImage = "";
      }
    }

    if (el.profileStatus) el.profileStatus.textContent = sub?.isActive ? "Active" : "Inactive";
    if (el.profileUntil) el.profileUntil.textContent = sub?.endDate ? `Until ${formatDateIso(sub.endDate)}` : "";
  }

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
          deviceCount: state.deviceCount,
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

  function bindDom() {
    el.spriteHost = document.getElementById("svgSpriteHost");
    el.tabPlans = document.getElementById("tabPlans");
    el.tabSetup = document.getElementById("tabSetup");
    el.tabProfile = document.getElementById("tabProfile");
    el.plansWrapper = document.getElementById("plansWrapper");
    el.paymentMethodsWrapper = document.getElementById("paymentMethodsWrapper");
    el.deviceRange = /** @type {HTMLInputElement|null} */ (document.getElementById("deviceRange"));
    el.deviceCount = document.getElementById("deviceCount");
    el.totalPrice = document.getElementById("totalPrice");
    el.btnPay = /** @type {HTMLButtonElement|null} */ (document.getElementById("btnPay"));
    el.subscriptionUrl = /** @type {HTMLInputElement|null} */ (document.getElementById("subscriptionUrl"));
    el.btnCopySub = document.getElementById("btnCopySub");
    el.profileName = document.getElementById("profileName");
    el.profileAvatar = document.getElementById("profileAvatar");
    el.profileStatus = document.getElementById("profileStatus");
    el.profileUntil = document.getElementById("profileUntil");
    el.btnRefresh = document.getElementById("btnRefresh");

    document.querySelectorAll("[data-tab-button]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const name = btn.getAttribute("data-tab-button");
        if (name) {
          tg?.HapticFeedback?.impactOccurred?.("medium");
          showTab(name);
        }
      });
    });

    el.deviceRange?.addEventListener("input", () => {
      const v = Math.max(1, Number(el.deviceRange?.value || 1));
      state.deviceCount = v;
      if (el.deviceCount) el.deviceCount.textContent = String(v);
      const max = Math.max(1, Number(el.deviceRange?.max || 1));
      const percent = max === 1 ? 0 : ((v - 1) / (max - 1)) * 100;
      const activeLine = document.getElementById("deviceActiveLine");
      const pointer = document.getElementById("devicePointer");
      if (activeLine) activeLine.style.width = `${percent}%`;
      if (pointer) pointer.style.left = `${percent}%`;
      updateTotal();
    });

    el.btnPay?.addEventListener("click", createPayment);
    el.btnCopySub?.addEventListener("click", () => copyText(el.subscriptionUrl?.value || ""));
    el.btnRefresh?.addEventListener("click", loadUser);
  }

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
    showTab("plans");

    // Sync initial device slider UI.
    if (el.deviceRange) {
      const v = Math.max(1, Number(el.deviceRange.value || 1));
      state.deviceCount = v;
      if (el.deviceCount) el.deviceCount.textContent = String(v);
      const max = Math.max(1, Number(el.deviceRange.max || 1));
      const percent = max === 1 ? 0 : ((v - 1) / (max - 1)) * 100;
      const activeLine = document.getElementById("deviceActiveLine");
      const pointer = document.getElementById("devicePointer");
      if (activeLine) activeLine.style.width = `${percent}%`;
      if (pointer) pointer.style.left = `${percent}%`;
    }

    await Promise.allSettled([loadTariffs(), loadPaymentMethods(), loadUser()]);
    updateTotal();
  });
})();

