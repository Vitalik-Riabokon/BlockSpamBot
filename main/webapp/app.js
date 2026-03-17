(() => {
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  const CATEGORY_LABELS = {
    suspect: "Проблемна",
    pending: "Не санкціонована",
    confirmed: "Легалізовані",
    blocked: "Заблоковані",
  };

  const state = {
    currentUserId: null,
    queryAdId: null,
    groups: [],
    groupId: "",
    groupMeta: null,
    section: "ads",
    category: "suspect",
    counts: { suspect: 0, pending: 0, confirmed: 0, blocked: 0 },
    swipeRequiresConfirm: true,
    ads: [],
    adsOffset: 0,
    adsLimit: 5,
    adsTotal: 0,
    selectedAdId: null,
    shouldScrollToSelectedAd: false,
    users: [],
    usersOffset: 0,
    usersLimit: 20,
    usersTotal: 0,
    usersSearch: "",
    usersFilter: "all",
    selectedUserId: null,
    moderators: [],
    moderatorSearch: "",
    triggerType: "word",
    triggerEditId: null,
    triggers: [],
    triggerSearch: "",
    triggerSort: "alpha",
    debounce: null,
    collapsed: {
      stats: false,
      adsQueue: false,
      users: false,
      settings: false,
    },
  };

  const els = {
    groupSelect: document.getElementById("groupSelect"),
    identityBar: document.getElementById("identityBar"),
    myIdText: document.getElementById("myIdText"),
    copyMyIdBtn: document.getElementById("copyMyIdBtn"),
    emptyAccessState: document.getElementById("emptyAccessState"),
    settingModeration: document.getElementById("settingModeration"),
    settingPending: document.getElementById("settingPending"),
    settingBlockedSound: document.getElementById("settingBlockedSound"),
    modeAdsBtn: document.getElementById("modeAdsBtn"),
    modeUsersBtn: document.getElementById("modeUsersBtn"),
    modeSettingsBtn: document.getElementById("modeSettingsBtn"),
    adsView: document.getElementById("adsView"),
    usersView: document.getElementById("usersView"),
    settingsView: document.getElementById("settingsView"),
    statsGrid: document.getElementById("statsGrid"),
    adsQueuePanel: document.getElementById("adsQueuePanel"),
    queueTitle: document.getElementById("queueTitle"),
    queueMeta: document.getElementById("queueMeta"),
    bulkActions: document.getElementById("bulkActions"),
    adsList: document.getElementById("adsList"),
    prevAdsBtn: document.getElementById("prevAdsBtn"),
    nextAdsBtn: document.getElementById("nextAdsBtn"),
    usersMeta: document.getElementById("usersMeta"),
    usersList: document.getElementById("usersList"),
    prevUsersBtn: document.getElementById("prevUsersBtn"),
    nextUsersBtn: document.getElementById("nextUsersBtn"),
    userSearchInput: document.getElementById("userSearchInput"),
    usersFilterSelect: document.getElementById("usersFilterSelect"),
    settingsCards: document.getElementById("settingsCards"),
    moderatorsModal: document.getElementById("moderatorsModal"),
    closeModeratorsModalBtn: document.getElementById("closeModeratorsModalBtn"),
    moderatorSearchInput: document.getElementById("moderatorSearchInput"),
    moderatorsList: document.getElementById("moderatorsList"),
    triggersModal: document.getElementById("triggersModal"),
    closeTriggersModalBtn: document.getElementById("closeTriggersModalBtn"),
    triggerWordsTabBtn: document.getElementById("triggerWordsTabBtn"),
    triggerPhrasesTabBtn: document.getElementById("triggerPhrasesTabBtn"),
    triggerSortSelect: document.getElementById("triggerSortSelect"),
    triggerSearchInput: document.getElementById("triggerSearchInput"),
    triggerInput: document.getElementById("triggerInput"),
    saveTriggerBtn: document.getElementById("saveTriggerBtn"),
    triggersList: document.getElementById("triggersList"),
    statsWrap: document.getElementById("statsWrap"),
    adsQueueWrap: document.getElementById("adsQueueWrap"),
    usersWrap: document.getElementById("usersWrap"),
    settingsWrap: document.getElementById("settingsWrap"),
    toggleStatsBtn: document.getElementById("toggleStatsBtn"),
    toggleAdsQueueBtn: document.getElementById("toggleAdsQueueBtn"),
    toggleUsersBtn: document.getElementById("toggleUsersBtn"),
    toggleSettingsBtn: document.getElementById("toggleSettingsBtn"),
    swipeOverlay: document.getElementById("swipeOverlay"),
    scrollTopBtn: document.getElementById("scrollTopBtn"),
  };

  function initDataHeader() {
    return tg?.initData || "";
  }

  async function apiGet(path) {
    const response = await fetch(path, {
      headers: { "X-Telegram-Init-Data": initDataHeader() },
    });
    if (!response.ok) {
      const body = await response.text();
      throw new Error(body || `HTTP ${response.status}`);
    }
    return await response.json();
  }

  async function apiPost(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": initDataHeader(),
      },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const body = await response.text();
      throw new Error(body || `HTTP ${response.status}`);
    }
    return await response.json();
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function shortText(value, limit = 170) {
    const text = String(value || "").trim();
    if (text.length <= limit) return text;
    return `${text.slice(0, limit - 1)}…`;
  }

  function userLabel(item) {
    return item.full_name || item.at_username || (item.username ? `@${item.username}` : `ID ${item.user_id}`);
  }

  function usernameLabel(item) {
    return item.at_username || (item.username ? `@${item.username}` : `ID ${item.user_id}`);
  }

  function categoryClass(category) {
    if (category === "suspect") return "red";
    if (category === "pending") return "amber";
    if (category === "confirmed") return "green";
    return "blue";
  }

  function triggerPlaceholder() {
    return state.triggerType === "word"
      ? "Введіть слово або слова через пробіл"
      : "Введіть речення";
  }

  function badgeClass(category) {
    if (category === "suspect") return "problem";
    if (category === "pending") return "pending";
    if (category === "confirmed") return "confirmed";
    return "blocked";
  }

  function setSection(section) {
    if (!state.groups.length) return;
    state.section = section;
    els.modeAdsBtn.classList.toggle("active", section === "ads");
    els.modeUsersBtn.classList.toggle("active", section === "users");
    els.modeSettingsBtn.classList.toggle("active", section === "settings");
    els.adsView.hidden = section !== "ads";
    els.usersView.hidden = section !== "users";
    els.settingsView.hidden = section !== "settings";
    syncUrl();
  }

  function renderCollapsedState() {
    els.statsWrap.hidden = !!state.collapsed.stats;
    els.adsQueueWrap.hidden = !!state.collapsed.adsQueue;
    els.usersWrap.hidden = !!state.collapsed.users;
    els.settingsWrap.hidden = !!state.collapsed.settings;
    els.toggleStatsBtn.textContent = state.collapsed.stats ? "˅" : "˄";
    els.toggleAdsQueueBtn.textContent = state.collapsed.adsQueue ? "˅" : "˄";
    els.toggleUsersBtn.textContent = state.collapsed.users ? "˅" : "˄";
    els.toggleSettingsBtn.textContent = state.collapsed.settings ? "˅" : "˄";
    els.toggleStatsBtn.setAttribute("aria-label", state.collapsed.stats ? "Розгорнути блок" : "Згорнути блок");
    els.toggleAdsQueueBtn.setAttribute("aria-label", state.collapsed.adsQueue ? "Розгорнути блок" : "Згорнути блок");
    els.toggleUsersBtn.setAttribute("aria-label", state.collapsed.users ? "Розгорнути блок" : "Згорнути блок");
    els.toggleSettingsBtn.setAttribute("aria-label", state.collapsed.settings ? "Розгорнути блок" : "Згорнути блок");
  }

  function renderAccessState() {
    const hasGroups = state.groups.length > 0;
    els.groupSelect.hidden = !hasGroups;
    document.querySelector(".current-settings")?.toggleAttribute("hidden", !hasGroups);
    document.querySelector(".top-tabs")?.toggleAttribute("hidden", !hasGroups);
    els.emptyAccessState.hidden = hasGroups;
    els.adsView.hidden = !hasGroups || state.section !== "ads";
    els.usersView.hidden = !hasGroups || state.section !== "users";
    els.settingsView.hidden = !hasGroups || state.section !== "settings";
  }

  function syncUrl() {
    const params = new URLSearchParams();
    if (state.groupId) params.set("group_id", state.groupId);
    params.set("section", state.section);
    if (state.section === "ads") params.set("category", state.category);
    const nextUrl = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState({}, "", nextUrl);
  }

  function scrollToElement(element) {
    if (!(element instanceof HTMLElement)) return;
    element.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function scrollToSelectedAdCard() {
    if (!state.selectedAdId) return;
    const card = els.adsList.querySelector(`[data-ad-id="${state.selectedAdId}"]`);
    if (!(card instanceof HTMLElement)) return;
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function isNearViewportTop(element, threshold = 72) {
    if (!(element instanceof HTMLElement)) return false;
    const rect = element.getBoundingClientRect();
    return rect.top <= threshold;
  }

  function isElementVisible(element) {
    if (!(element instanceof HTMLElement) || element.hidden) return false;
    const rect = element.getBoundingClientRect();
    return rect.bottom > 96 && rect.top < window.innerHeight - 24;
  }

  function scrollStepUp() {
    const hero = document.querySelector(".hero");
    if (!(hero instanceof HTMLElement)) {
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }

    const currentY = window.scrollY + 110;
    const heroTop = hero.offsetTop;

    if (state.section === "ads") {
      const queuePanel = els.adsQueuePanel;
      const statsPanel = els.statsWrap?.closest(".panel");
      const queueTop = queuePanel instanceof HTMLElement ? queuePanel.offsetTop : Number.POSITIVE_INFINITY;
      const statsTop = statsPanel instanceof HTMLElement ? statsPanel.offsetTop : Number.POSITIVE_INFINITY;

      if (currentY >= queueTop - 8 && statsPanel instanceof HTMLElement) {
        scrollToElement(statsPanel);
        return;
      }

      if (currentY >= statsTop - 8) {
        scrollToElement(hero);
        return;
      }

      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }

    if (state.section === "users") {
      const usersPanel = els.usersWrap?.closest(".panel");
      const usersTop = usersPanel instanceof HTMLElement ? usersPanel.offsetTop : Number.POSITIVE_INFINITY;
      if (currentY >= usersTop - 8) {
        scrollToElement(hero);
        return;
      }
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }

    if (state.section === "settings") {
      const settingsPanel = els.settingsWrap?.closest(".panel");
      const settingsTop = settingsPanel instanceof HTMLElement ? settingsPanel.offsetTop : Number.POSITIVE_INFINITY;
      if (currentY >= settingsTop - 8) {
        scrollToElement(hero);
        return;
      }
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }

    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function loadGroups() {
    const data = await apiGet("/webapp/api/groups");
    state.currentUserId = Number(data.user_id || 0) || null;
    state.groups = data.groups || [];
    els.groupSelect.innerHTML = "";
    for (const group of state.groups) {
      const option = document.createElement("option");
      option.value = String(group.group_id);
      option.textContent = group.title;
      els.groupSelect.appendChild(option);
    }

    const params = new URLSearchParams(window.location.search);
    const queryGroup = params.get("group_id");
    const querySection = params.get("section");
    const queryCategory = params.get("category");
    const queryAdId = params.get("ad_id");
    const storedGroupId = window.localStorage.getItem("bayreuth:selectedGroupId");

    if (queryGroup && state.groups.some((item) => String(item.group_id) === queryGroup)) {
      state.groupId = queryGroup;
    } else if (storedGroupId && state.groups.some((item) => String(item.group_id) === storedGroupId)) {
      state.groupId = storedGroupId;
    } else if (data.selected_group_id && state.groups.some((item) => String(item.group_id) === String(data.selected_group_id))) {
      state.groupId = String(data.selected_group_id);
    } else if (state.groups[0]) {
      state.groupId = String(state.groups[0].group_id);
    }

    if (["ads", "users", "settings"].includes(querySection)) state.section = querySection;
    if (queryCategory && CATEGORY_LABELS[queryCategory]) state.category = queryCategory;
    state.queryAdId = queryAdId && /^\d+$/.test(queryAdId) ? Number(queryAdId) : null;

    if (state.currentUserId) {
      els.myIdText.textContent = `Ваш id: ${state.currentUserId}`;
    }
    if (state.groupId) {
      els.groupSelect.value = state.groupId;
      window.localStorage.setItem("bayreuth:selectedGroupId", state.groupId);
      setSection(state.section);
    }
    renderAccessState();
  }

  async function loadOverview() {
    if (!state.groupId) return;
    const data = await apiGet(`/webapp/api/overview?group_id=${state.groupId}`);
    state.groupMeta = data.group;
    state.counts = data.counts || state.counts;
    renderCurrentSettings();
    renderStats();
    renderSettingsCards();
    renderCollapsedState();
  }

  function renderCurrentSettings() {
    const group = state.groupMeta;
    if (!group) return;
    state.swipeRequiresConfirm = group.swipe_requires_confirm !== false;
    els.settingModeration.textContent = `Модерація: ${group.is_paused ? "призупинена" : "активна"}`;
    els.settingPending.textContent = `Не санкціоновані: ${group.notify_pending ? "сповіщення увімкнено" : "сповіщення вимкнено"}`;
    els.settingBlockedSound.textContent = `Звук авто-блоків: ${group.blocked_alert_sound ? "увімкнено" : "вимкнено"}`;
  }

  function renderStats() {
    const items = ["suspect", "pending", "confirmed", "blocked"];
    els.statsGrid.innerHTML = items.map((category) => {
      const count = state.counts[category] || 0;
      const foot = category === "suspect"
        ? "Потрібне рішення"
        : category === "pending"
          ? "Чекають підтвердження"
          : category === "confirmed"
            ? "Від легалізованих"
            : "Автоблоки";
      return `
        <button class="metric-card ${categoryClass(category)} ${state.category === category ? "active" : ""}" data-category="${category}" type="button">
          <span class="stat-label">${escapeHtml(CATEGORY_LABELS[category])}</span>
          <span class="value">${count}</span>
          <span class="foot">${escapeHtml(foot)}</span>
        </button>
      `;
    }).join("");

    els.statsGrid.querySelectorAll("[data-category]").forEach((button) => {
      button.addEventListener("click", async () => {
        state.category = button.dataset.category;
        state.adsOffset = 0;
        state.selectedAdId = null;
        setSection("ads");
        await loadAds();
        els.adsQueuePanel.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  }

  function renderBulkActions() {
    const count = state.counts[state.category] || 0;
    const allowed = ["blocked", "pending", "confirmed"].includes(state.category) && count > 0;
    if (!allowed) {
      els.bulkActions.innerHTML = "";
      return;
    }
    if (state.groupMeta?.is_paused) {
      els.bulkActions.innerHTML = `<button class="btn dark" type="button" disabled>Модерація призупинена</button>`;
      return;
    }
    els.bulkActions.innerHTML = `<button id="confirmAllBtn" class="btn dark" type="button">Підтвердити всі (${count})</button>`;
    document.getElementById("confirmAllBtn")?.addEventListener("click", async () => {
      const ok = window.confirm(`Підтвердити всі кейси в категорії "${CATEGORY_LABELS[state.category]}"?`);
      if (!ok) return;
      els.bulkActions.innerHTML = `<button class="btn dark" type="button" disabled>Оновлення...</button>`;
      els.adsList.innerHTML = `<div class="empty-state">Оновлення списку...</div>`;
      await apiPost("/webapp/api/ads/confirm-all", {
        group_id: Number(state.groupId),
        category: state.category,
      });
      state.selectedAdId = null;
      await Promise.all([loadOverview(), loadAds()]);
      if (state.adsTotal === 0) {
        const statsPanel = els.statsWrap?.closest(".panel");
        scrollToElement(statsPanel);
      }
    });
  }

  async function loadAds() {
    if (!state.groupId) return;
    const params = new URLSearchParams({
      group_id: state.groupId,
      category: state.category,
      offset: String(state.adsOffset),
      limit: String(state.adsLimit),
    });
    const data = await apiGet(`/webapp/api/ads?${params.toString()}`);
    state.ads = data.items || [];
    state.adsTotal = Number(data.total || 0);
    state.counts = data.counts || state.counts;
    if (state.queryAdId && state.ads.some((item) => item.ad_id === state.queryAdId)) {
      state.selectedAdId = state.queryAdId;
      state.shouldScrollToSelectedAd = true;
      state.queryAdId = null;
    } else if (!state.selectedAdId || !state.ads.some((item) => item.ad_id === state.selectedAdId)) {
      state.selectedAdId = state.ads[0]?.ad_id || null;
    }
    renderStats();
    renderBulkActions();
    renderAdsList();
    syncUrl();
  }

  function renderAdsList() {
    els.queueTitle.textContent = CATEGORY_LABELS[state.category];
    els.queueMeta.textContent = state.category === "blocked"
      ? `Тут показані автоблоки. Цифра зверху означає лише ті, що ще не підтверджені.`
      : `Невирішених кейсів у цій категорії: ${state.adsTotal}.`;

    if (!state.ads.length) {
      els.adsList.innerHTML = `<div class="empty-state">У цій категорії зараз немає кейсів.</div>`;
      els.prevAdsBtn.disabled = state.adsOffset <= 0;
      els.nextAdsBtn.disabled = true;
      return;
    }

    els.adsList.innerHTML = state.ads.map((item) => {
      const isActive = item.ad_id === state.selectedAdId;
      const swipe = getSwipeActions(item);
      return `
        <article class="queue-card ${isActive ? `active ${item.category}` : ""}" data-ad-id="${item.ad_id}">
          <div class="swipe-shell" data-swipe-surface data-ad-id="${item.ad_id}" data-right-action="${swipe.rightAction}" data-left-action="${swipe.leftAction}">
            <div class="swipe-track">
              <div class="swipe-side left"></div>
              <div class="swipe-side right"></div>
            </div>
            <div class="swipe-card ${isActive ? "swipe-hint" : ""}">
              <div class="card-head">
                <strong>${escapeHtml(userLabel(item))}</strong>
                <span class="badge ${badgeClass(item.category)}">${escapeHtml(item.category_label)}</span>
              </div>
              ${isActive ? renderAdDetails(item) : `<div class="card-text">${escapeHtml(shortText(item.text))}</div>`}
            </div>
          </div>
        </article>
      `;
    }).join("");

    els.adsList.querySelectorAll("[data-ad-action]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const adId = Number(button.dataset.adId);
        const action = button.dataset.adAction;
        await runAdAction(adId, action, true, button.textContent.trim());
      });
    });

    els.adsList.querySelectorAll(".queue-card[data-ad-id]").forEach((card) => {
      card.addEventListener("click", (event) => {
        const target = event.target;
        if (target instanceof HTMLElement && target.closest("[data-ad-action]")) {
          return;
        }
        const adId = Number(card.dataset.adId);
        if (!adId || adId === state.selectedAdId) {
          return;
        }
        state.selectedAdId = adId;
        renderAdsList();
      });
    });

    attachSwipeHandlers();

    if (state.shouldScrollToSelectedAd) {
      state.shouldScrollToSelectedAd = false;
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          scrollToSelectedAdCard();
        });
      });
    }

    els.prevAdsBtn.disabled = state.adsOffset <= 0;
    els.nextAdsBtn.disabled = state.adsOffset + state.adsLimit >= state.adsTotal;
  }

  function renderAdDetails(item) {
    const actions = [];
    const disabledAttr = state.groupMeta?.is_paused ? " disabled" : "";
    if (item.category === "blocked") {
      actions.push(`<button class="btn ok" data-ad-action="unmute" data-ad-id="${item.ad_id}" type="button"${disabledAttr}>Розблокувати</button>`);
      if (item.requires_action) {
        actions.push(`<button class="btn dark" data-ad-action="ack" data-ad-id="${item.ad_id}" type="button"${disabledAttr}>Підтвердити</button>`);
      }
    } else {
      actions.push(`<button class="btn dark" data-ad-action="approve" data-ad-id="${item.ad_id}" type="button"${disabledAttr}>Підтвердити</button>`);
      actions.push(`<button class="btn danger" data-ad-action="block" data-ad-id="${item.ad_id}" type="button"${disabledAttr}>Заблокувати</button>`);
    }

    return `
      <div class="card-detail">
        <div class="card-detail-text">${escapeHtml(item.text)}</div>
        <div class="card-detail-meta">
          <div><strong>Користувач:</strong> ${escapeHtml(usernameLabel(item))}</div>
          <div><strong>ID:</strong> ${item.user_id}</div>
          <div><strong>Телефон:</strong> ${escapeHtml(item.phone || "-")}</div>
          <div><strong>Message ID:</strong> ${item.source_message_id}</div>
        </div>
        ${state.groupMeta?.is_paused ? `<div class="empty-state">Модерація призупинена. Рішення тимчасово недоступні.</div>` : ""}
        <div class="card-actions">${actions.join("")}</div>
      </div>
    `;
  }

  function getSwipeActions(item) {
    if (item.category === "blocked") {
      return {
        leftAction: "unmute",
        rightAction: item.requires_action ? "ack" : "",
      };
    }
    return {
      leftAction: "block",
      rightAction: "approve",
    };
  }

  function setSwipeOverlay(deltaX) {
    if (!(els.swipeOverlay instanceof HTMLElement)) return;
    const intensity = Math.min(1, Math.abs(deltaX) / 150);
    if (intensity <= 0.02) {
      els.swipeOverlay.style.opacity = "0";
      return;
    }
    const alphaStrong = (0.09 + intensity * 0.18).toFixed(3);
    const alphaMid = (0.04 + intensity * 0.09).toFixed(3);
    if (deltaX > 0) {
      els.swipeOverlay.style.background = `linear-gradient(90deg, rgba(32,119,90,0) 0%, rgba(32,119,90,${alphaMid}) 45%, rgba(32,119,90,${alphaStrong}) 100%)`;
    } else {
      els.swipeOverlay.style.background = `linear-gradient(270deg, rgba(212,74,48,0) 0%, rgba(212,74,48,${alphaMid}) 45%, rgba(212,74,48,${alphaStrong}) 100%)`;
    }
    els.swipeOverlay.style.opacity = "1";
  }

  function clearSwipeOverlay() {
    if (!(els.swipeOverlay instanceof HTMLElement)) return;
    els.swipeOverlay.style.opacity = "0";
  }

  async function runAdAction(adId, action, askConfirm = true, label = "") {
    if (!action) return;
    if (state.groupMeta?.is_paused) return;
    const currentIndex = state.ads.findIndex((item) => item.ad_id === adId);
    const nextCandidate = state.ads[currentIndex + 1]?.ad_id || state.ads[currentIndex - 1]?.ad_id || null;
    if (askConfirm) {
      const ok = window.confirm(`Виконати дію "${label || action}"?`);
      if (!ok) return;
    }
    const previousAds = [...state.ads];
    const previousAdsTotal = state.adsTotal;
    const previousCounts = { ...state.counts };
    state.ads = state.ads.filter((item) => item.ad_id !== adId);
    state.adsTotal = Math.max(0, state.adsTotal - 1);
    if (state.counts[state.category] > 0) {
      state.counts[state.category] -= 1;
    }
    state.selectedAdId = nextCandidate && state.ads.some((item) => item.ad_id === nextCandidate)
      ? nextCandidate
      : (state.ads[0]?.ad_id || null);
    renderStats();
    renderBulkActions();
    renderAdsList();
    if (state.adsTotal === 0) {
      const statsPanel = els.statsWrap?.closest(".panel");
      scrollToElement(statsPanel);
    }
    try {
      await apiPost("/webapp/api/ads/action", {
        group_id: Number(state.groupId),
        ad_id: adId,
        action,
      });
      await Promise.all([loadOverview(), loadAds()]);
      if (state.adsTotal === 0) {
        const statsPanel = els.statsWrap?.closest(".panel");
        scrollToElement(statsPanel);
      }
    } catch (error) {
      state.ads = previousAds;
      state.adsTotal = previousAdsTotal;
      state.counts = previousCounts;
      state.selectedAdId = adId;
      renderStats();
      renderBulkActions();
      renderAdsList();
      window.alert(`Помилка дії: ${error.message}`);
    }
  }

  function attachSwipeHandlers() {
    const threshold = 58;
    const intentThreshold = 12;
    if (state.groupMeta?.is_paused) {
      els.adsList.querySelectorAll(".swipe-card").forEach((card) => {
        if (card instanceof HTMLElement) {
          card.style.transform = "translateX(0px)";
          card.classList.remove("ready-left", "ready-right", "swiping");
        }
      });
      clearSwipeOverlay();
      return;
    }
    els.adsList.querySelectorAll("[data-swipe-surface]").forEach((surface) => {
      const adId = Number(surface.dataset.adId);
      if (adId !== state.selectedAdId) {
        return;
      }
      const card = surface.querySelector(".swipe-card");
      if (!(card instanceof HTMLElement)) return;

      let startX = 0;
      let startY = 0;
      let deltaX = 0;
      let deltaY = 0;
      let dragging = false;
      let moved = false;
      let swipeIntent = false;
      let cancelledForScroll = false;

      const reset = () => {
        dragging = false;
        deltaX = 0;
        deltaY = 0;
        moved = false;
        swipeIntent = false;
        cancelledForScroll = false;
        card.style.transform = "translateX(0px)";
        card.classList.remove("ready-left", "ready-right");
        card.classList.remove("swiping");
        clearSwipeOverlay();
      };

      const applyDelta = (rawDelta) => {
        deltaX = Math.max(-220, Math.min(220, rawDelta));
        if (Math.abs(deltaX) > 4) moved = true;
        card.style.transform = `translateX(${deltaX}px)`;
        card.classList.toggle("ready-right", deltaX >= threshold);
        card.classList.toggle("ready-left", deltaX <= -threshold);
        setSwipeOverlay(deltaX);
      };

      const start = (clientX, clientY) => {
        dragging = true;
        startX = clientX;
        startY = clientY;
      };

      const move = (clientX, clientY) => {
        if (!dragging) return;
        deltaX = clientX - startX;
        deltaY = clientY - startY;

        if (!swipeIntent) {
          if (Math.abs(deltaY) > intentThreshold && Math.abs(deltaY) > Math.abs(deltaX)) {
            cancelledForScroll = true;
            return;
          }
          if (Math.abs(deltaX) < intentThreshold || Math.abs(deltaX) <= Math.abs(deltaY) + 6) {
            return;
          }
          swipeIntent = true;
          card.classList.add("swiping");
        }

        if (cancelledForScroll) return;
        applyDelta(deltaX);
      };

      const end = async () => {
        if (!dragging) return;
        dragging = false;
        const rightAction = surface.dataset.rightAction;
        const leftAction = surface.dataset.leftAction;
        const triggeredRight = deltaX >= threshold;
        const triggeredLeft = deltaX <= -threshold;
        const wasMoved = moved;
        const hadSwipeIntent = swipeIntent;
        const blockedByScroll = cancelledForScroll;
        reset();

        if (blockedByScroll) {
          return;
        }

        if (!triggeredRight && !triggeredLeft) {
          if (!wasMoved || !hadSwipeIntent) {
            state.selectedAdId = adId;
            renderAdsList();
          }
          return;
        }

        const action = triggeredRight ? rightAction : leftAction;
        if (!action) return;
        const label = triggeredRight ? "Підтвердити" : (action === "unmute" ? "Розблокувати" : "Заблокувати");
        await runAdAction(adId, action, state.swipeRequiresConfirm, label);
      };

      card.addEventListener("pointerdown", (event) => {
        card.setPointerCapture?.(event.pointerId);
        start(event.clientX, event.clientY);
      });
      card.addEventListener("pointermove", (event) => {
        if (!dragging) return;
        move(event.clientX, event.clientY);
      });
      card.addEventListener("pointerup", () => { void end(); });
      card.addEventListener("pointercancel", reset);
      card.addEventListener("lostpointercapture", reset);
    });
  }

  async function loadUsers() {
    if (!state.groupId) return;
    const params = new URLSearchParams({
      group_id: state.groupId,
      q: state.usersSearch,
      offset: String(state.usersOffset),
      limit: String(state.usersLimit),
    });
    const data = await apiGet(`/webapp/api/users?${params.toString()}`);
    state.users = data.users || [];
    state.usersTotal = Number(data.total || 0);
    const visible = getVisibleUsers();
    if (!state.selectedUserId || !visible.some((item) => item.user_id === state.selectedUserId)) {
      state.selectedUserId = visible[0]?.user_id || null;
    }
    renderUsersList();
  }

  async function loadModerators() {
    if (!state.groupId) return;
    const params = new URLSearchParams({
      group_id: state.groupId,
      q: state.moderatorSearch,
    });
    const data = await apiGet(`/webapp/api/moderators?${params.toString()}`);
    state.moderators = data.items || [];
    renderModeratorsList();
  }

  function renderModeratorsList() {
    if (!state.moderators.length) {
      els.moderatorsList.innerHTML = `<div class="empty-state">Модераторів не знайдено.</div>`;
      return;
    }

    els.moderatorsList.innerHTML = state.moderators.map((item) => `
      <article class="moderator-row">
        <div class="moderator-row-top">
          <strong>${escapeHtml(item.full_name || item.at_username || `ID ${item.user_id}`)}</strong>
          <div class="moderator-badges">
            ${item.is_owner ? '<span class="badge owner">Головний</span>' : ""}
          </div>
        </div>
        <p class="card-meta">${escapeHtml(item.at_username || (item.username ? `@${item.username}` : `ID ${item.user_id}`))}</p>
        <div class="card-detail-meta">
          <div><strong>ID:</strong> ${item.user_id}</div>
          <div><strong>Телефон:</strong> ${escapeHtml(item.phone || "-")}</div>
        </div>
        ${item.can_remove ? `<div class="card-actions"><button class="btn danger" data-remove-moderator="${item.user_id}" type="button">Видалити модератора</button></div>` : ""}
      </article>
    `).join("");

    els.moderatorsList.querySelectorAll("[data-remove-moderator]").forEach((button) => {
      button.addEventListener("click", async () => {
        const userId = Number(button.dataset.removeModerator);
        const ok = window.confirm(`Видалити модератора ${userId}?`);
        if (!ok) return;
        try {
          await apiPost("/webapp/api/moderators/remove", {
            group_id: Number(state.groupId),
            user_id: userId,
          });
          await loadOverview();
          await loadModerators();
        } catch (error) {
          window.alert(error.message);
        }
      });
    });
  }

  function getVisibleUsers() {
    if (state.usersFilter === "allowed") return state.users.filter((item) => item.is_whitelisted);
    if (state.usersFilter === "not_allowed") return state.users.filter((item) => !item.is_whitelisted);
    return state.users;
  }

  function renderUsersList() {
    const visible = getVisibleUsers();
    els.usersMeta.textContent = `Знайдено ${state.usersTotal}. Показано ${visible.length}.`;

    if (!visible.length) {
      els.usersList.innerHTML = `<div class="empty-state">Користувачів не знайдено.</div>`;
      els.prevUsersBtn.disabled = state.usersOffset <= 0;
      els.nextUsersBtn.disabled = true;
      return;
    }

    els.usersList.innerHTML = visible.map((item) => {
      const active = item.user_id === state.selectedUserId;
      return `
        <article class="user-card ${active ? "active" : ""}" data-user-id="${item.user_id}">
          <div class="user-top">
            <strong>${escapeHtml(item.full_name || "Без імені")}</strong>
            <span class="badge ${item.is_whitelisted ? "allowed" : "not-allowed"}">${item.is_whitelisted ? "Легалізовано" : "Не легалізовано"}</span>
          </div>
          <p class="card-meta">${escapeHtml(item.at_username || (item.username ? `@${item.username}` : `ID ${item.user_id}`))}</p>
          ${active ? renderUserDetails(item) : ""}
        </article>
      `;
    }).join("");

    els.usersList.querySelectorAll("[data-user-id]").forEach((card) => {
      card.addEventListener("click", (event) => {
        const target = event.target;
        if (target instanceof HTMLElement && target.closest("button")) return;
        state.selectedUserId = Number(card.dataset.userId);
        renderUsersList();
      });
    });

    els.usersList.querySelectorAll("[data-user-action]").forEach((button) => {
      button.addEventListener("click", async (event) => {
        event.stopPropagation();
        const userId = Number(button.dataset.userId);
        const action = button.dataset.userAction;
        const ok = window.confirm(`${button.textContent.trim()} користувачу ${userId}?`);
        if (!ok) return;
        await apiPost(`/webapp/api/whitelist/${action === "grant" ? "grant" : "revoke"}`, {
          group_id: Number(state.groupId),
          user_id: userId,
        });
        await loadUsers();
      });
    });

    els.prevUsersBtn.disabled = state.usersOffset <= 0;
    els.nextUsersBtn.disabled = state.usersOffset + state.usersLimit >= state.usersTotal;
  }

  function renderUserDetails(item) {
    return `
      <div class="card-detail">
        <div class="card-detail-meta">
          <div><strong>Телефон:</strong> ${escapeHtml(item.phone || "-")}</div>
          <div><strong>Статус:</strong> ${escapeHtml(item.status || "-")}</div>
        </div>
        <div class="card-actions">
          ${item.is_whitelisted
            ? `<button class="btn danger" data-user-action="revoke" data-user-id="${item.user_id}" type="button">Зняти легалізацію</button>`
            : `<button class="btn ok" data-user-action="grant" data-user-id="${item.user_id}" type="button">Надати легалізацію</button>`}
        </div>
      </div>
    `;
  }

  function renderSettingsCards() {
    const group = state.groupMeta;
    if (!group) return;
    els.settingsCards.innerHTML = `
      <article class="setting-card">
        <strong>Модерація групи</strong>
        <p class="panel-meta">Дає змогу повністю призупинити або знову ввімкнути модерацію повідомлень у вибраній групі.</p>
        <div class="card-actions">
          <button class="btn ${group.is_paused ? "ok" : "danger"}" data-setting="is_paused" type="button">${group.is_paused ? "Увімкнути модерацію" : "Призупинити модерацію"}</button>
        </div>
      </article>
      <article class="setting-card">
        <strong>Не санкціоновані реклами</strong>
        <p class="panel-meta">Вмикає або вимикає Telegram-сповіщення про адекватні, але не підтверджені реклами.</p>
        <div class="card-actions">
          <button class="btn ${group.notify_pending ? "danger" : "ok"}" data-setting="notify_pending" type="button">${group.notify_pending ? "Вимкнути сповіщення" : "Увімкнути сповіщення"}</button>
        </div>
      </article>
      <article class="setting-card">
        <strong>Звук від заблокованих</strong>
        <p class="panel-meta">Керує звуком Telegram-сповіщень для авто-блоків.</p>
        <div class="card-actions">
          <button class="btn ${group.blocked_alert_sound ? "danger" : "ok"}" data-setting="blocked_alert_sound" type="button">${group.blocked_alert_sound ? "Вимкнути звук" : "Увімкнути звук"}</button>
        </div>
      </article>
      <article class="setting-card">
        <strong>Підтвердження свайпу</strong>
        <p class="panel-meta">Керує тим, чи треба підтверджувати швидку свайп-дію в деталях реклами.</p>
        <div class="card-actions">
          <button class="btn ${group.swipe_requires_confirm ? "danger" : "ok"}" data-setting="swipe_requires_confirm" type="button">${group.swipe_requires_confirm ? "Вимкнути підтвердження" : "Увімкнути підтвердження"}</button>
        </div>
      </article>
      <article class="setting-card">
        <strong>Підтверджені заблоковані</strong>
        <p class="panel-meta">Приховує в розділі "Заблоковані" ті записи, які вже були підтверджені модератором раніше.</p>
        <div class="card-actions">
          <button class="btn ${group.hide_confirmed_blocked ? "ok" : "danger"}" data-setting="hide_confirmed_blocked" type="button">${group.hide_confirmed_blocked ? "Показувати підтверджені" : "Приховати підтверджені"}</button>
        </div>
      </article>
      <article class="setting-card">
        <strong>Додати модератора</strong>
        <p class="panel-meta">Новий модератор має відкрити застосунок, скопіювати свій id і надіслати його вам.</p>
        <div class="card-actions">
          <button class="btn ghost" data-helper="add_moderator" type="button">Показати кроки</button>
        </div>
        <div class="moderator-add-row">
          <input id="moderatorIdInput" class="search-input" type="text" inputmode="numeric" placeholder="Введіть або вставте user id" />
          <button id="addModeratorBtn" class="btn ok" type="button">Додати</button>
        </div>
      </article>
      <article class="setting-card">
        <strong>Тригери</strong>
        <p class="panel-meta">Додає жорсткі тригери для слів або цілих речень. Працює тільки для вибраної групи.</p>
        <div class="card-actions">
          <button id="openTriggersModalBtn" class="btn ghost" type="button">Відкрити список</button>
        </div>
      </article>
      ${group.moderator_count > 1 ? `
      <article class="setting-card">
        <strong>Видалити модератора</strong>
        <p class="panel-meta">Відкриває список модераторів групи. Головного модератора видалити не можна.</p>
        <div class="card-actions">
          <button id="openModeratorsModalBtn" class="btn ghost" type="button">Відкрити список</button>
        </div>
      </article>` : ""}
    `;

    els.settingsCards.querySelectorAll("[data-setting]").forEach((button) => {
      button.addEventListener("click", async () => {
        const setting = button.dataset.setting;
        await apiPost("/webapp/api/settings/toggle", {
          group_id: Number(state.groupId),
          setting,
        });
        await loadOverview();
        if (state.section === "ads") {
          await loadAds();
        }
      });
    });

    els.settingsCards.querySelectorAll("[data-helper]").forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.dataset.helper;
        if (action === "add_moderator") {
          const text = [
            "Як додати модератора:",
            "1. Користувач має відкрити застосунок бота.",
            "2. Унизу стартового екрана він бачить свій id і копіює його.",
            "3. Ви вставляєте цей id у поле нижче і натискаєте 'Додати'.",
          ].join("\n");
          if (tg?.showAlert) {
            tg.showAlert(text);
          } else {
            window.alert(text);
          }
        }
      });
    });

    document.getElementById("addModeratorBtn")?.addEventListener("click", async () => {
      const input = document.getElementById("moderatorIdInput");
      if (!(input instanceof HTMLInputElement)) return;
      const value = input.value.trim();
      if (!/^\d+$/.test(value)) {
        window.alert("Введіть коректний user id.");
        return;
      }
      await apiPost("/webapp/api/moderators/add", {
        group_id: Number(state.groupId),
        user_id: Number(value),
      });
      input.value = "";
      if (tg?.showAlert) {
        tg.showAlert("Модератора додано.");
      } else {
        window.alert("Модератора додано.");
      }
    });

    document.getElementById("openModeratorsModalBtn")?.addEventListener("click", async () => {
      state.moderatorSearch = "";
      els.moderatorSearchInput.value = "";
      els.moderatorsModal.hidden = false;
      await loadModerators();
    });

    document.getElementById("openTriggersModalBtn")?.addEventListener("click", async () => {
      state.triggerType = "word";
      state.triggerEditId = null;
      state.triggerSearch = "";
      state.triggerSort = "alpha";
      if (els.triggerSearchInput) els.triggerSearchInput.value = "";
      if (els.triggerSortSelect) els.triggerSortSelect.value = "alpha";
      els.triggerInput.value = "";
      els.triggersModal.hidden = false;
      renderTriggerTabs();
      await loadTriggers();
    });
  }

  function schedule(callback) {
    clearTimeout(state.debounce);
    state.debounce = setTimeout(callback, 220);
  }

  function renderTriggerTabs() {
    els.triggerWordsTabBtn?.classList.toggle("active", state.triggerType === "word");
    els.triggerPhrasesTabBtn?.classList.toggle("active", state.triggerType === "phrase");
    if (els.triggerInput) {
      els.triggerInput.placeholder = triggerPlaceholder();
    }
    if (els.saveTriggerBtn) {
      els.saveTriggerBtn.textContent = state.triggerEditId ? "Зберегти" : "Додати";
    }
  }

  async function loadTriggers() {
    const data = await apiGet(
      `/webapp/api/triggers?group_id=${encodeURIComponent(state.groupId)}&type=${encodeURIComponent(state.triggerType)}&q=${encodeURIComponent(state.triggerSearch)}&sort=${encodeURIComponent(state.triggerSort)}`
    );
    state.triggers = data.items || [];
    renderTriggers();
  }

  function renderTriggers() {
    renderTriggerTabs();
    els.triggersList.innerHTML = "";
    if (!state.triggers.length) {
      els.triggersList.innerHTML = `<article class="queue-card empty-card"><strong>Порожньо</strong><p class="panel-meta">Для цього типу тригерів ще нічого не додано.</p></article>`;
      return;
    }

    state.triggers.forEach((item) => {
      const article = document.createElement("article");
      article.className = "queue-card";
      article.innerHTML = `
        <div class="card-topline">
          <strong>${escapeHtml(item.value)}</strong>
          <span class="status-badge ${state.triggerType === "word" ? "blocked" : "pending"}">${state.triggerType === "word" ? "Слово" : "Речення"}</span>
        </div>
        <div class="card-actions">
          <button class="btn ghost" data-trigger-edit="${item.trigger_id}" type="button">Редагувати</button>
          <button class="btn danger" data-trigger-delete="${item.trigger_id}" type="button">Видалити</button>
        </div>
      `;
      els.triggersList.appendChild(article);
    });

    els.triggersList.querySelectorAll("[data-trigger-edit]").forEach((button) => {
      button.addEventListener("click", () => {
        const triggerId = Number(button.dataset.triggerEdit);
        const item = state.triggers.find((entry) => entry.trigger_id === triggerId);
        if (!item || !(els.triggerInput instanceof HTMLInputElement)) return;
        state.triggerEditId = triggerId;
        els.triggerInput.value = item.value;
        renderTriggerTabs();
      });
    });

    els.triggersList.querySelectorAll("[data-trigger-delete]").forEach((button) => {
      button.addEventListener("click", async () => {
        const triggerId = Number(button.dataset.triggerDelete);
        const ok = window.confirm("Видалити тригер?");
        if (!ok) return;
        await apiPost("/webapp/api/triggers/delete", {
          group_id: Number(state.groupId),
          trigger_id: triggerId,
        });
        if (state.triggerEditId === triggerId) {
          state.triggerEditId = null;
          els.triggerInput.value = "";
        }
        await loadTriggers();
      });
    });
  }

  async function refreshCurrentSection() {
    await loadOverview();
    if (state.section === "ads") await loadAds();
    if (state.section === "users") await loadUsers();
    if (state.section === "settings") renderSettingsCards();
  }

  function setUserFilter(filter) {
    state.usersFilter = filter;
    if (els.usersFilterSelect) {
      els.usersFilterSelect.value = filter;
    }
    if (!getVisibleUsers().some((item) => item.user_id === state.selectedUserId)) {
      state.selectedUserId = getVisibleUsers()[0]?.user_id || null;
    }
    renderUsersList();
  }

  els.groupSelect.addEventListener("change", async () => {
    state.groupId = els.groupSelect.value;
    window.localStorage.setItem("bayreuth:selectedGroupId", state.groupId);
    state.adsOffset = 0;
    state.usersOffset = 0;
    state.selectedAdId = null;
    state.selectedUserId = null;
    await refreshCurrentSection();
  });

  els.modeAdsBtn.addEventListener("click", async () => {
    setSection("ads");
    await loadOverview();
    await loadAds();
  });

  els.modeUsersBtn.addEventListener("click", async () => {
    setSection("users");
    await loadUsers();
  });

  els.modeSettingsBtn.addEventListener("click", async () => {
    setSection("settings");
    await loadOverview();
  });

  els.prevAdsBtn.addEventListener("click", () => {
    state.adsOffset = Math.max(0, state.adsOffset - state.adsLimit);
    loadAds();
  });

  els.nextAdsBtn.addEventListener("click", () => {
    state.adsOffset += state.adsLimit;
    loadAds();
  });

  els.userSearchInput.addEventListener("input", () => {
    state.usersSearch = els.userSearchInput.value.trim();
    schedule(() => {
      state.usersOffset = 0;
      state.selectedUserId = null;
      loadUsers();
    });
  });

  els.prevUsersBtn.addEventListener("click", () => {
    state.usersOffset = Math.max(0, state.usersOffset - state.usersLimit);
    loadUsers();
  });

  els.nextUsersBtn.addEventListener("click", () => {
    state.usersOffset += state.usersLimit;
    loadUsers();
  });

  els.usersFilterSelect?.addEventListener("change", () => {
    setUserFilter(els.usersFilterSelect.value);
  });
  els.toggleStatsBtn.addEventListener("click", () => {
    state.collapsed.stats = !state.collapsed.stats;
    renderCollapsedState();
  });
  els.toggleAdsQueueBtn.addEventListener("click", () => {
    state.collapsed.adsQueue = !state.collapsed.adsQueue;
    renderCollapsedState();
  });
  els.toggleUsersBtn.addEventListener("click", () => {
    state.collapsed.users = !state.collapsed.users;
    renderCollapsedState();
  });
  els.toggleSettingsBtn.addEventListener("click", () => {
    state.collapsed.settings = !state.collapsed.settings;
    renderCollapsedState();
  });
  document.querySelectorAll(".section-toggle[data-collapse-key]").forEach((node) => {
    const toggle = () => {
      const key = node.dataset.collapseKey;
      if (!key || !(key in state.collapsed)) return;
      state.collapsed[key] = !state.collapsed[key];
      renderCollapsedState();
    };
    node.addEventListener("click", toggle);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });
  });

  els.scrollTopBtn.addEventListener("click", () => {
    scrollStepUp();
  });

  els.closeModeratorsModalBtn?.addEventListener("click", () => {
    els.moderatorsModal.hidden = true;
  });

  els.closeTriggersModalBtn?.addEventListener("click", () => {
    els.triggersModal.hidden = true;
    state.triggerEditId = null;
    state.triggerSearch = "";
    state.triggerSort = "alpha";
    if (els.triggerInput) els.triggerInput.value = "";
    if (els.triggerSearchInput) els.triggerSearchInput.value = "";
    if (els.triggerSortSelect) els.triggerSortSelect.value = "alpha";
    renderTriggerTabs();
  });

  els.triggerWordsTabBtn?.addEventListener("click", async () => {
    state.triggerType = "word";
    state.triggerEditId = null;
    if (els.triggerInput) els.triggerInput.value = "";
    await loadTriggers();
  });

  els.triggerPhrasesTabBtn?.addEventListener("click", async () => {
    state.triggerType = "phrase";
    state.triggerEditId = null;
    if (els.triggerInput) els.triggerInput.value = "";
    await loadTriggers();
  });

  els.triggerSortSelect?.addEventListener("change", async () => {
    state.triggerSort = els.triggerSortSelect.value;
    await loadTriggers();
  });

  els.triggerSearchInput?.addEventListener("input", () => {
    state.triggerSearch = els.triggerSearchInput.value.trim();
    schedule(() => {
      loadTriggers();
    });
  });

  els.saveTriggerBtn?.addEventListener("click", async () => {
    if (!(els.triggerInput instanceof HTMLInputElement)) return;
    const value = els.triggerInput.value.trim();
    if (!value) {
      window.alert("Введіть значення тригера.");
      return;
    }
    try {
      if (state.triggerEditId) {
        await apiPost("/webapp/api/triggers/update", {
          group_id: Number(state.groupId),
          trigger_id: state.triggerEditId,
          trigger_type: state.triggerType,
          value,
        });
      } else {
        await apiPost("/webapp/api/triggers/add", {
          group_id: Number(state.groupId),
          trigger_type: state.triggerType,
          value,
        });
      }
    } catch (error) {
      const message = String(error?.message || "");
      if (message.includes("bad trigger input")) {
        window.alert(
          state.triggerType === "word"
            ? "У словах дозволені тільки літери, пробіл і апостроф."
            : "У реченнях дозволені тільки літери, пробіл і апостроф."
        );
      } else if (message.includes("empty trigger")) {
        window.alert("Поле тригера порожнє.");
      } else {
        window.alert("Не вдалося зберегти тригер.");
      }
      return;
    }
    state.triggerEditId = null;
    els.triggerInput.value = "";
    await loadTriggers();
  });

  els.moderatorsModal?.addEventListener("click", (event) => {
    if (event.target === els.moderatorsModal) {
      els.moderatorsModal.hidden = true;
    }
  });

  els.moderatorSearchInput?.addEventListener("input", () => {
    state.moderatorSearch = els.moderatorSearchInput.value.trim();
    schedule(() => {
      loadModerators();
    });
  });

  els.copyMyIdBtn?.addEventListener("click", async () => {
    if (!state.currentUserId) return;
    try {
      await navigator.clipboard.writeText(String(state.currentUserId));
      els.copyMyIdBtn.classList.add("copied");
      window.setTimeout(() => els.copyMyIdBtn.classList.remove("copied"), 900);
    } catch (_error) {
      window.prompt("Скопіюйте id вручну:", String(state.currentUserId));
    }
  });

  (async () => {
    try {
      await loadGroups();
      if (state.groups.length) {
        await loadOverview();
        if (state.section === "ads") await loadAds();
        if (state.section === "users") await loadUsers();
        if (state.section === "settings") renderSettingsCards();
      }
    } catch (error) {
      const message = `Помилка: ${error.message}`;
      els.adsList.innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
      els.usersList.innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
    }
  })();
})();
