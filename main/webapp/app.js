(() => {
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  const groupSelect = document.getElementById("groupSelect");
  const searchInput = document.getElementById("searchInput");
  const listEl = document.getElementById("list");
  const metaEl = document.getElementById("meta");
  const prevBtn = document.getElementById("prevBtn");
  const nextBtn = document.getElementById("nextBtn");
  const filterAll = document.getElementById("filterAll");
  const filterAllowed = document.getElementById("filterAllowed");
  const filterNotAllowed = document.getElementById("filterNotAllowed");

  let offset = 0;
  const limit = 20;
  let total = 0;
  let filter = "all";
  let debounce = null;

  function initDataHeader() {
    return tg?.initData || "";
  }

  function setFilter(value) {
    filter = value;
    for (const btn of [filterAll, filterAllowed, filterNotAllowed]) {
      btn.classList.remove("active");
    }
    if (value === "all") filterAll.classList.add("active");
    if (value === "allowed") filterAllowed.classList.add("active");
    if (value === "not_allowed") filterNotAllowed.classList.add("active");
    offset = 0;
    loadUsers();
  }

  async function apiGet(path) {
    const response = await fetch(path, {
      headers: {
        "X-Telegram-Init-Data": initDataHeader(),
      },
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

  function userVisible(user) {
    if (filter === "allowed") return !!user.is_whitelisted;
    if (filter === "not_allowed") return !user.is_whitelisted;
    return true;
  }

  function renderUsers(users) {
    listEl.innerHTML = "";
    const visible = users.filter(userVisible);
    if (!visible.length) {
      listEl.innerHTML = "<p>Нічого не знайдено.</p>";
      return;
    }

    for (const user of visible) {
      const card = document.createElement("article");
      card.className = "user-card";

      const username = user.at_username || (user.username ? `@${user.username}` : "-");
      const phone = user.phone || "-";
      const fullName = user.full_name || "Без імені";
      const statusBadge = user.is_whitelisted
        ? '<span class="badge allowed">Легалізовано</span>'
        : '<span class="badge not-allowed">Не легалізовано</span>';

      card.innerHTML = `
        <div class="user-top">
          <div class="name">${escapeHtml(fullName)}</div>
          ${statusBadge}
        </div>
        <div class="details">
          ID: ${user.user_id}<br/>
          Username: ${escapeHtml(username)}<br/>
          Телефон: ${escapeHtml(phone)}<br/>
          Статус: ${escapeHtml(user.status || "-")}
        </div>
        <div class="actions">
          ${
            user.is_whitelisted
              ? `<button class="btn" data-action="revoke" data-user-id="${user.user_id}">Зняти легалізацію</button>`
              : `<button class="btn primary" data-action="grant" data-user-id="${user.user_id}">Надати легалізацію</button>`
          }
        </div>
      `;

      listEl.appendChild(card);
    }
  }

  function escapeHtml(str) {
    return String(str)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  async function loadGroups() {
    const data = await apiGet("/webapp/api/groups");
    groupSelect.innerHTML = "";
    for (const group of data.groups) {
      const opt = document.createElement("option");
      opt.value = String(group.group_id);
      opt.textContent = `${group.title} (${group.group_id})`;
      groupSelect.appendChild(opt);
    }

    const params = new URLSearchParams(window.location.search);
    const queryGroup = params.get("group_id");
    if (queryGroup) groupSelect.value = queryGroup;
  }

  async function loadUsers() {
    const groupId = groupSelect.value;
    if (!groupId) {
      metaEl.textContent = "Немає доступних груп.";
      listEl.innerHTML = "";
      return;
    }
    const q = encodeURIComponent(searchInput.value.trim());
    const data = await apiGet(
      `/webapp/api/users?group_id=${groupId}&q=${q}&offset=${offset}&limit=${limit}`
    );
    total = Number(data.total || 0);
    metaEl.textContent = `Знайдено: ${total}. Показано ${offset + 1}-${Math.min(
      offset + limit,
      total
    )}`;
    renderUsers(data.users || []);
    prevBtn.disabled = offset <= 0;
    nextBtn.disabled = offset + limit >= total;
  }

  listEl.addEventListener("click", async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLButtonElement)) return;
    const action = target.dataset.action;
    const userId = Number(target.dataset.userId);
    const groupId = Number(groupSelect.value);
    if (!action || !userId || !groupId) return;

    try {
      if (action === "grant") {
        const ok = window.confirm(`Надати легалізацію користувачу ${userId}?`);
        if (!ok) return;
        await apiPost("/webapp/api/whitelist/grant", { group_id: groupId, user_id: userId });
      } else if (action === "revoke") {
        const ok = window.confirm(`Зняти легалізацію у користувача ${userId}?`);
        if (!ok) return;
        await apiPost("/webapp/api/whitelist/revoke", { group_id: groupId, user_id: userId });
      }
      await loadUsers();
    } catch (error) {
      alert(`Помилка: ${error.message}`);
    }
  });

  groupSelect.addEventListener("change", () => {
    offset = 0;
    loadUsers();
  });

  searchInput.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      offset = 0;
      loadUsers();
    }, 220);
  });

  prevBtn.addEventListener("click", () => {
    offset = Math.max(0, offset - limit);
    loadUsers();
  });

  nextBtn.addEventListener("click", () => {
    offset += limit;
    loadUsers();
  });

  filterAll.addEventListener("click", () => setFilter("all"));
  filterAllowed.addEventListener("click", () => setFilter("allowed"));
  filterNotAllowed.addEventListener("click", () => setFilter("not_allowed"));

  (async () => {
    try {
      await loadGroups();
      await loadUsers();
    } catch (error) {
      metaEl.textContent = `Помилка: ${error.message}`;
    }
  })();
})();
