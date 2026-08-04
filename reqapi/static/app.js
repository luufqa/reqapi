const state = {
  user: null,
  setupRequired: false,
  registrationMode: false,
  registrationUsername: "",
  adminUsers: [],
  collections: [],
  tabSets: [],
  tabSetRequests: {},
  expandedTabSets: new Set(),
  requestsByCollection: {},
  expandedCollections: new Set(),
  openTabs: [],
  activeTabKey: null,
  nextDraftId: 1,
  currentCollectionId: null,
  currentRequest: null,
  env: {},
  lastResponseText: "",
  hasResponse: false,
  requestRunning: false,
  requestAbortController: null,
  currentExecutionId: null,
  requestRunId: 0,
  contextRequest: null,
  contextCollection: null,
  contextTabKey: null,
  contextTabSet: null,
  contextTabSetRequest: null,
  draggedCollectionId: null,
  draggedRequest: null,
  syncingUrlParams: false,
  workspaceSaveTimer: null,
  workspaceReady: false,
  onboardingSeen: false,
  onboardingActive: false,
  onboardingStep: 0,
  onboardingManual: false,
  onboardingSteps: [],
  activeBodyMode: "none",
  bodyFormData: [],
  bodyUrlencoded: [],
  bodyBinary: {},
  bodyGraphql: { query: "", variables: "{}" },
  deleteRequests: [],
  catalogSnapshot: null,
  syncPollTimer: null,
  syncPollBusy: false,
  requestAutosaveTimer: null,
  requestAutosaveQueue: new Map(),
  requestAutosaveInFlight: false,
  requestAutosaveActiveKey: null,
  requestAutosaveError: null,
  applyingRemoteRequest: false,
};

const $ = (id) => document.getElementById(id);
const MAX_TAB_SET_TABS = 20;
const MAX_FORM_FILE_BYTES = 20 * 1024 * 1024;
const SIDEBAR_WIDTH_KEY = "reqapi.sidebarWidth";
const RESPONSE_HEIGHT_KEY = "reqapi.responseHeight";
const CURL_BLOCKED_HEADERS = new Set([
  "host",
  "content-length",
  "connection",
  "transfer-encoding",
  "upgrade",
]);
const pairTemplate = JSON.stringify([{ key: "", value: "", enabled: true }], null, 2);

function formatCount(value, singular) {
  const count = Number(value) || 0;
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

const methodClassNames = [
  "method-get",
  "method-post",
  "method-put",
  "method-patch",
  "method-delete",
  "method-head",
  "method-options",
];
const onboardingTips = [
  {
    title: "Choose a request",
    text: "Open a collection, then select a request. Play opens and runs it.",
    target: ".sidebar > .section-title",
  },
  {
    title: "Work together",
    text: "Collections and saved requests are shared with everyone. All users can create collections and requests, and edit requests. Only users with the Administrator role can delete them.",
    target: ".collection-row",
  },
  {
    title: "Switch requests",
    text: "Your open requests stay here as personal tabs.",
    target: "#request-tabs",
  },
  {
    title: "Configure the request",
    text: "Set parameters, authorization, body, scripts, and TLS options here.",
    target: ".editor-pane > .tabs",
  },
  {
    title: "Send the request",
    text: "Select Send, or press Enter while editing the URL.",
    target: "#send-btn",
  },
  {
    title: "Automatic saving",
    text: "All changes are saved automatically and synchronized with other users.",
    target: "#request-save-status",
  },
  {
    title: "Mark for delete",
    text: "Right-click a request or collection and choose Mark for delete. This creates a deletion request that users with the Administrator role can review and approve or reject.",
    target: "#delete-requests-btn",
  },
  {
    title: "Save a tab set",
    text: "Keep your own reusable groups of request tabs here.",
    target: ".tab-sets-panel",
  },
];

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...options, headers });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    if (
      res.status === 401 &&
      path !== "/api/login" &&
      path !== "/api/setup" &&
      path !== "/api/register"
    ) {
      state.user = null;
      showAuth(data.error || "Your session has expired. Please sign in again.");
    }
    const error = new Error(data.error || `HTTP ${res.status}`);
    error.status = res.status;
    error.data = data;
    throw error;
  }
  return data;
}

function showAuth(message = "") {
  const setupMode = state.setupRequired && !state.registrationMode;
  const credentialSetupMode = setupMode || state.registrationMode;
  $("auth").classList.remove("hidden");
  $("app").classList.add("hidden");
  $("auth-mode").textContent = setupMode
    ? "Initial administrator setup"
    : state.registrationMode
      ? "New user: create a password"
      : "Sign in to the service";
  $("auth-submit").textContent = setupMode
    ? "Create admin"
    : state.registrationMode
      ? "Create user"
      : "Sign in";
  $("auth-confirm-row").classList.toggle("hidden", !credentialSetupMode);
  $("auth-password").autocomplete = credentialSetupMode ? "new-password" : "current-password";
  $("auth-error").textContent = message;
  $("auth-username").readOnly = setupMode;
  if (setupMode) {
    $("auth-username").value = "admin";
  } else if (state.registrationMode && state.registrationUsername) {
    $("auth-username").value = state.registrationUsername;
  } else if (!$("auth-username").value.trim()) {
    $("auth-username").value = "admin";
  }
  $("auth-password").focus();
}

function showApp() {
  $("auth").classList.add("hidden");
  $("app").classList.remove("hidden");
  $("current-user").textContent = state.user?.username || "";
  updateAdminControls();
}

async function boot() {
  const me = await api("/api/me");
  state.setupRequired = Boolean(me.setup_required);
  state.user = me.user;
  if (!state.user) {
    showAuth();
    return;
  }
  showApp();
  await loadAppData();
}

function parseJsonEditor(id, fallback) {
  const value = $(id).value.trim();
  if (!value) return fallback;
  return JSON.parse(value);
}

function setJsonEditor(id, value) {
  $(id).value = JSON.stringify(value, null, 2);
}

function animateActionButton(button) {
  if (!button) return;
  button.classList.remove("action-pulse");
  void button.offsetWidth;
  button.classList.add("action-pulse");
  window.setTimeout(() => button.classList.remove("action-pulse"), 520);
}

function createExecutionId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function cancelActiveExecution() {
  const executionId = state.currentExecutionId;
  state.currentExecutionId = null;
  if (!executionId) return;
  fetch(`/api/executions/${encodeURIComponent(executionId)}/cancel`, {
    method: "POST",
    credentials: "same-origin",
  }).catch(() => {});
}

function resetResponsePane() {
  state.lastResponseText = "";
  state.hasResponse = false;
  state.requestRunning = false;
  if (state.requestAbortController) {
    cancelActiveExecution();
    state.requestAbortController.abort();
    state.requestAbortController = null;
  }
  if (!$("response-output")) return;
  $("copy-response-btn").disabled = true;
  $("download-response-btn").disabled = true;
  $("response-meta-text").textContent = "Response";
  $("response-output").innerHTML = '<div class="response-empty">Response body will appear here.</div>';
}

function resetUserWorkspaceState(options = {}) {
  const { clearCollections = false, clearEnv = false } = options;
  window.clearTimeout(state.workspaceSaveTimer);
  window.clearInterval(state.syncPollTimer);
  state.workspaceSaveTimer = null;
  state.syncPollTimer = null;
  state.syncPollBusy = false;
  state.tabSets = [];
  state.tabSetRequests = {};
  state.expandedTabSets = new Set();
  state.requestsByCollection = {};
  state.expandedCollections = new Set();
  state.openTabs = [];
  state.activeTabKey = null;
  state.currentCollectionId = null;
  state.currentRequest = null;
  state.contextRequest = null;
  state.contextCollection = null;
  state.contextTabKey = null;
  state.contextTabSet = null;
  state.contextTabSetRequest = null;
  state.draggedCollectionId = null;
  state.draggedRequest = null;
  state.syncingUrlParams = false;
  if (clearCollections) state.collections = [];
  if (clearEnv) state.env = {};
  hideContextMenus();
  closeAdminUsersModal();
  if ($("request-tabs")) renderRequestTabs();
  if ($("collections-list")) renderCollections();
  if ($("tab-sets-list")) renderTabSets();
  resetResponsePane();
}

async function loadAppData() {
  state.workspaceReady = false;
  resetUserWorkspaceState({ clearCollections: true, clearEnv: true });
  const [, , , catalog] = await Promise.all([
    loadCollections({ selectFirst: false }),
    loadEnv(),
    loadTabSets(),
    api("/api/catalog-state"),
    loadDeleteRequests({ render: false }),
  ]);
  state.catalogSnapshot = catalog;
  await loadUserWorkspace();
  state.workspaceReady = true;
  updateTrashButton();
  renderCollections();
  startSharedSync();
  if (!state.onboardingSeen) {
    window.setTimeout(() => startOnboarding(false), 250);
  }
}

async function loadCollections(options = {}) {
  const { selectFirst = true } = options;
  const data = await api("/api/collections");
  state.collections = data.collections;
  renderCollections();
  if (selectFirst && !state.currentCollectionId && state.collections[0]) {
    await expandCollection(state.collections[0].id, true);
  } else if (state.currentCollectionId) {
    await ensureCollectionRequests(state.currentCollectionId);
    renderCollections();
  }
}

function pendingDeletion(targetType, targetId) {
  return state.deleteRequests.find(
    (item) => item.target_type === targetType && String(item.target_id) === String(targetId),
  );
}

async function loadDeleteRequests(options = {}) {
  const { render = true } = options;
  const data = await api("/api/delete-requests");
  state.deleteRequests = data.delete_requests || [];
  updateTrashButton();
  if (render) {
    renderDeleteRequests();
    renderCollections();
    renderRequestTabs();
  }
  return state.deleteRequests;
}

function updateTrashButton() {
  const button = $("delete-requests-btn");
  const badge = $("delete-requests-count");
  if (!button || !badge) return;
  const count = state.deleteRequests.length;
  button.classList.toggle("has-items", count > 0);
  button.setAttribute("aria-label", count ? `Deletion requests: ${count}` : "No deletion requests");
  badge.textContent = String(count);
  badge.classList.toggle("hidden", count === 0);
}

async function submitDeleteRequest(targetType, target) {
  if (!target?.id) return;
  if (pendingDeletion(targetType, target.id)) {
    alert("A deletion request has already been submitted for this item.");
    return;
  }
  const label = target.name || target.url || "this item";
  if (!confirm(`Send a request to delete "${label}" to the administrator?`)) return;
  await api("/api/delete-requests", {
    method: "POST",
    body: JSON.stringify({ target_type: targetType, target_id: target.id }),
  });
  await loadDeleteRequests();
}

function renderDeleteRequests() {
  const container = $("delete-requests-list");
  if (!container) return;
  if (!state.deleteRequests.length) {
    container.innerHTML = '<div class="delete-requests-empty">There are no deletion requests.</div>';
    return;
  }
  container.innerHTML = state.deleteRequests
    .map((item) => {
      const createdAt = item.created_at
        ? new Date(item.created_at).toLocaleString("en-US")
        : "Unknown time";
      const actions = isAdmin()
        ? `<div class="delete-request-actions">
             <button type="button" class="danger approve-delete" data-delete-approve="${item.id}">Delete permanently</button>
             <button type="button" data-delete-dismiss="${item.id}">Dismiss</button>
           </div>`
        : "";
      return `<article class="delete-request-item">
        <div class="delete-request-copy">
          <strong>${escapeHtml(item.target_name || "Deleted item")}</strong>
          <span>${item.target_type === "collection" ? "Collection" : "Request"}</span>
          <small>Requested by ${escapeHtml(item.requester_username || "unknown")} · ${escapeHtml(createdAt)}</small>
        </div>
        ${actions}
      </article>`;
    })
    .join("");
}

async function refreshSharedCatalog(nextCatalog) {
  const validRequestIds = new Set((nextCatalog.requests || []).map((item) => String(item.id)));
  const collectionIds = new Set([
    ...state.expandedCollections,
    ...state.openTabs.map((tab) => tab.request?.collection_id).filter(Boolean),
  ]);
  await loadCollections({ selectFirst: false });
  await Promise.all(
    [...collectionIds].map((id) => ensureCollectionRequests(id, true).catch(() => null)),
  );
  const requestMap = new Map(
    Object.values(state.requestsByCollection)
      .flat()
      .map((request) => [String(request.id), request]),
  );
  const previousActiveKey = state.activeTabKey;
  state.openTabs = state.openTabs
    .filter((tab) => !tab.request?.id || validRequestIds.has(String(tab.request.id)))
    .map((tab) => {
      const fresh = tab.request?.id ? requestMap.get(String(tab.request.id)) : null;
      if (requestSaveIsPending(tab.key)) return tab;
      return fresh ? { ...tab, request: { ...tab.request, ...fresh } } : tab;
    });
  if (!state.openTabs.some((tab) => tab.key === previousActiveKey)) {
    state.activeTabKey = state.openTabs[0]?.key || null;
  }
  if (state.activeTabKey) {
    const active = state.openTabs.find((tab) => tab.key === state.activeTabKey);
    if (active && !requestSaveIsPending(active.key)) {
      state.applyingRemoteRequest = true;
      try {
        populateEditor(active.request);
        setRequestSaveStatus("current", "Up to date");
      } finally {
        state.applyingRemoteRequest = false;
      }
    }
  } else {
    state.currentRequest = null;
    state.currentCollectionId = null;
  }
  state.catalogSnapshot = nextCatalog;
  renderCollections();
  renderRequestTabs();
  scheduleWorkspaceSave();
}

function startSharedSync() {
  window.clearInterval(state.syncPollTimer);
  state.syncPollTimer = window.setInterval(async () => {
    if (state.syncPollBusy || !state.user) return;
    state.syncPollBusy = true;
    try {
      const [catalog, deletionData, meData] = await Promise.all([
        api("/api/catalog-state"),
        api("/api/delete-requests"),
        api("/api/me"),
      ]);
      const nextUser = meData?.user;
      if (nextUser && (
        nextUser.id !== state.user?.id
        || nextUser.role !== state.user?.role
        || nextUser.username !== state.user?.username
      )) {
        state.user = nextUser;
        $("current-user").textContent = state.user?.username || "";
        updateAdminControls();
      }
      if (JSON.stringify(catalog) !== JSON.stringify(state.catalogSnapshot)) {
        await refreshSharedCatalog(catalog);
      }
      const nextRequests = deletionData.delete_requests || [];
      if (JSON.stringify(nextRequests) !== JSON.stringify(state.deleteRequests)) {
        state.deleteRequests = nextRequests;
        updateTrashButton();
        renderDeleteRequests();
        renderCollections();
        renderRequestTabs();
      }
    } catch (error) {
      console.warn("Shared workspace sync failed", error);
    } finally {
      state.syncPollBusy = false;
    }
  }, 2000);
}

async function loadTabSets() {
  const data = await api("/api/tab-sets");
  state.tabSets = data.tab_sets;
  const existingIds = new Set(state.tabSets.map((tabSet) => tabSet.id));
  state.expandedTabSets = new Set([...state.expandedTabSets].filter((id) => existingIds.has(id)));
  renderTabSets();
}

async function loadTabSetRequests(tabSetId, force = false) {
  if (!force && state.tabSetRequests[tabSetId]) {
    return state.tabSetRequests[tabSetId];
  }
  const data = await api(`/api/tab-sets/${tabSetId}/requests`);
  state.tabSetRequests[tabSetId] = data.requests;
  return data.requests;
}

async function loadUserWorkspace() {
  const data = await api("/api/workspace");
  state.onboardingSeen = Boolean(data.onboarding_seen);
  state.openTabs = [];
  state.activeTabKey = null;
  state.currentRequest = null;
  state.currentCollectionId = null;
  const tabs = Array.isArray(data.open_tabs) ? data.open_tabs : [];
  tabs.forEach((request) => {
    const key = request.tabKey || requestKey(request, { forceNew: true });
    state.openTabs.push({ key, request: { ...request, tabKey: key } });
  });
  const collectionIds = [...new Set(state.openTabs.map((tab) => tab.request.collection_id).filter(Boolean))];
  for (const collectionId of collectionIds) {
    state.expandedCollections.add(collectionId);
    await ensureCollectionRequests(collectionId);
  }
  const activeKey = state.openTabs.some((tab) => tab.key === data.active_tab_key)
    ? data.active_tab_key
    : state.openTabs[0]?.key;
  if (activeKey) {
    activateRequestTab(activeKey, { skipSync: true, skipWorkspaceSave: true });
  } else {
    renderRequestTabs();
    renderCollections();
  }
}

function workspaceTabsPayload() {
  return state.openTabs
    .filter((tab) => tab.request?.id)
    .map((tab) => ({
      request_id: tab.request.id,
      tab_key: tab.key,
    }));
}

function scheduleWorkspaceSave() {
  if (!state.user || !state.workspaceReady) return;
  window.clearTimeout(state.workspaceSaveTimer);
  state.workspaceSaveTimer = window.setTimeout(() => {
    saveUserWorkspace().catch((error) => console.warn(error));
  }, 180);
}

async function saveUserWorkspace() {
  await api("/api/workspace", {
    method: "PUT",
    body: JSON.stringify({
      open_tabs: workspaceTabsPayload(),
      active_tab_key: state.activeTabKey || "",
    }),
  });
}

async function flushWorkspaceSave() {
  if (!state.user || !state.workspaceReady) return;
  window.clearTimeout(state.workspaceSaveTimer);
  state.workspaceSaveTimer = null;
  await saveUserWorkspace();
}

function activeCollectionId() {
  return state.currentRequest?.collection_id || state.currentCollectionId;
}

function focusActiveRequestInSidebar(options = {}) {
  const requestId = state.currentRequest?.id;
  const collectionId = state.currentRequest?.collection_id;
  if (!requestId || !collectionId) return;

  const wasExpanded = state.expandedCollections.has(collectionId);
  state.expandedCollections.add(collectionId);
  const item = document.querySelector(`.request-tree-item[data-request-id="${requestId}"]`);
  if (item) {
    const container = $("collections-list");
    if (container) {
      const itemRect = item.getBoundingClientRect();
      const containerRect = container.getBoundingClientRect();
      const topGap = itemRect.top - containerRect.top;
      const bottomGap = itemRect.bottom - containerRect.bottom;
      if (topGap < 12) {
        container.scrollTop += topGap - 12;
      } else if (bottomGap > -12) {
        container.scrollTop += bottomGap + 12;
      }
    }
    item.classList.add("sidebar-focus");
    window.setTimeout(() => item.classList.remove("sidebar-focus"), 900);
    return;
  }

  if (options.skipLoad) return;

  const focusAfterRender = () => {
    renderCollections();
    window.requestAnimationFrame(() => focusActiveRequestInSidebar({ skipLoad: true }));
  };

  if (!state.requestsByCollection[collectionId]) {
    ensureCollectionRequests(collectionId).then(focusAfterRender).catch((error) => console.warn(error));
  } else if (!wasExpanded) {
    focusAfterRender();
  }
}

function renderCollections() {
  $("collections-list").innerHTML = "";
  state.collections.forEach((collection) => {
    const isExpanded = state.expandedCollections.has(collection.id);
    const node = document.createElement("div");
    node.className = "collection-node" + (isExpanded ? " expanded" : "");

    const row = document.createElement("div");
    row.className = "collection-row list-item"
      + (collection.id === activeCollectionId() ? " active" : "")
      + (pendingDeletion("collection", collection.id) ? " pending-delete" : "");
    row.draggable = true;
    row.innerHTML = `
      <div class="collection-main">
        <span class="collection-handle" title="Drag to reorder">::</span>
        <span class="collection-chevron">${isExpanded ? "v" : ">"}</span>
        <span class="collection-copy">
          <strong>${escapeHtml(collection.name)}</strong>
          <span>${formatCount(collection.request_count, "request")}</span>
        </span>
      </div>
      <button class="collection-add" title="New request">+</button>
    `;
    row.onclick = () => toggleCollection(collection.id);
    row.oncontextmenu = (event) => {
      event.preventDefault();
      event.stopPropagation();
      showCollectionContextMenu(event, collection);
    };
    row.ondragstart = (event) => {
      state.draggedCollectionId = collection.id;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(collection.id));
      row.classList.add("dragging");
    };
    row.ondragend = () => {
      state.draggedCollectionId = null;
      row.classList.remove("dragging");
      document.querySelectorAll(".collection-row.drag-over").forEach((el) => el.classList.remove("drag-over"));
    };
    row.ondragover = (event) => {
      if (!state.draggedCollectionId || state.draggedCollectionId === collection.id) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      row.classList.add("drag-over");
    };
    row.ondragleave = () => row.classList.remove("drag-over");
    row.ondrop = async (event) => {
      event.preventDefault();
      row.classList.remove("drag-over");
      if (!state.draggedCollectionId || state.draggedCollectionId === collection.id) return;
      try {
        await moveCollectionBefore(state.draggedCollectionId, collection.id);
      } catch (error) {
        alert(error.message);
      }
    };
    row.querySelector(".collection-add").onclick = async (event) => {
      event.stopPropagation();
      state.currentCollectionId = collection.id;
      state.expandedCollections.add(collection.id);
      await ensureCollectionRequests(collection.id);
      newRequestDraft();
    };
    node.appendChild(row);

    if (isExpanded) {
      const requests = state.requestsByCollection[collection.id] || [];
      const list = document.createElement("div");
      list.className = "collection-requests";
      if (requests.length === 0) {
        const empty = document.createElement("div");
        empty.className = "collection-empty";
        empty.textContent = "No requests";
        list.appendChild(empty);
      }
      requests.forEach((request) => {
        const item = document.createElement("div");
        const method = escapeHtml(request.method || "GET");
        item.className = "request-tree-item"
          + (state.currentRequest?.id === request.id ? " active" : "")
          + (pendingDeletion("request", request.id) ? " pending-delete" : "");
        item.dataset.requestId = request.id;
        item.draggable = true;
        item.innerHTML = `
          <b class="method-badge method-${method.toLowerCase()}">${method}</b>
          <span>${escapeHtml(request.name)}</span>
          <button class="request-play" title="Run request">▶</button>
        `;
        item.onclick = (event) => {
          event.stopPropagation();
          openRequest(request);
        };
        item.oncontextmenu = (event) => {
          event.preventDefault();
          event.stopPropagation();
          showRequestContextMenu(event, request);
        };
        item.ondragstart = (event) => {
          state.draggedRequest = { collectionId: collection.id, requestId: request.id };
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", String(request.id));
          item.classList.add("dragging");
        };
        item.ondragend = () => {
          state.draggedRequest = null;
          item.classList.remove("dragging");
          document.querySelectorAll(".request-tree-item.drag-over").forEach((el) => el.classList.remove("drag-over"));
        };
        item.ondragover = (event) => {
          if (
            !state.draggedRequest ||
            state.draggedRequest.collectionId !== collection.id ||
            state.draggedRequest.requestId === request.id
          ) {
            return;
          }
          event.preventDefault();
          event.dataTransfer.dropEffect = "move";
          item.classList.add("drag-over");
        };
        item.ondragleave = () => item.classList.remove("drag-over");
        item.ondrop = async (event) => {
          event.preventDefault();
          item.classList.remove("drag-over");
          if (
            !state.draggedRequest ||
            state.draggedRequest.collectionId !== collection.id ||
            state.draggedRequest.requestId === request.id
          ) {
            return;
          }
          try {
            await moveRequestBefore(collection.id, state.draggedRequest.requestId, request.id);
          } catch (error) {
            alert(error.message);
          }
        };
        item.querySelector(".request-play").onclick = async (event) => {
          event.stopPropagation();
          try {
            await runRequestFromCollection(request);
          } catch (error) {
            alert(error.message);
          }
        };
        list.appendChild(item);
      });
      node.appendChild(list);
    }

    $("collections-list").appendChild(node);
  });
}

function renderTabSets() {
  const container = $("tab-sets-list");
  container.innerHTML = "";
  if (!state.tabSets.length) {
    const empty = document.createElement("div");
    empty.className = "tab-set-empty";
    empty.textContent = "No saved tab sets";
    container.appendChild(empty);
    return;
  }
  sortedTabSets().forEach((tabSet) => {
    const node = document.createElement("div");
    node.className = "tab-set-node" + (state.expandedTabSets.has(tabSet.id) ? " expanded" : "");

    const row = document.createElement("div");
    row.className = "tab-set-row";
    row.title = `Open ${tabSet.name}`;
    row.innerHTML = `
      <div class="tab-set-main">
        <span class="tab-set-chevron">${state.expandedTabSets.has(tabSet.id) ? "v" : ">"}</span>
        <span>${escapeHtml(tabSet.name)}</span>
        <small>${formatCount(tabSet.request_count, "tab")}</small>
      </div>
      <button class="tab-set-add-workspace" title="Add tabs to workspace" aria-label="Add tabs to workspace">
        <svg class="pin-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M14.2 3.8 20.2 9.8"></path>
          <path d="M9 9l6 6"></path>
          <path d="M4.5 19.5l5.7-5.7"></path>
          <path d="M7.6 6.9c2.4-1.7 5.1-1.5 6.9.3l2.6-2.6 2.3 2.3-2.6 2.6c1.8 1.8 2 4.5.3 6.9L7.6 6.9z"></path>
        </svg>
      </button>
    `;
    row.onclick = () => toggleTabSet(tabSet).catch((error) => alert(error.message));
    row.oncontextmenu = (event) => {
      event.preventDefault();
      event.stopPropagation();
      showTabSetContextMenu(event, tabSet);
    };
    row.querySelector(".tab-set-add-workspace").onclick = (event) => {
      event.stopPropagation();
      addTabSetToWorkspace(tabSet).catch((error) => alert(error.message));
    };
    node.appendChild(row);

    if (state.expandedTabSets.has(tabSet.id)) {
      const list = document.createElement("div");
      list.className = "tab-set-requests";
      const requests = state.tabSetRequests[tabSet.id] || [];
      if (!requests.length) {
        const empty = document.createElement("div");
        empty.className = "tab-set-empty";
        empty.textContent = "No requests";
        list.appendChild(empty);
      }
      requests.forEach((request) => {
        const method = escapeHtml(request.method || "GET");
        const item = document.createElement("div");
        item.className = "tab-set-request-item";
        item.innerHTML = `
          <b class="method-badge method-${method.toLowerCase()}">${method}</b>
          <span>${escapeHtml(requestLabel(request))}</span>
          <button class="request-play" title="Run request">▶</button>
        `;
        item.onclick = (event) => {
          event.stopPropagation();
          openRequest(request);
        };
        item.oncontextmenu = (event) => {
          event.preventDefault();
          event.stopPropagation();
          showTabSetRequestContextMenu(event, tabSet, request);
        };
        item.querySelector(".request-play").onclick = async (event) => {
          event.stopPropagation();
          try {
            await runRequestFromCollection(request);
          } catch (error) {
            alert(error.message);
          }
        };
        list.appendChild(item);
      });
      node.appendChild(list);
    }

    container.appendChild(node);
  });
}

async function toggleTabSet(tabSet) {
  if (state.expandedTabSets.has(tabSet.id)) {
    state.expandedTabSets.delete(tabSet.id);
    renderTabSets();
    return;
  }
  state.expandedTabSets.add(tabSet.id);
  await loadTabSetRequests(tabSet.id);
  renderTabSets();
}

function currentSavedRequestIds() {
  const ids = [];
  if (state.activeTabKey) {
    markRequestChanged();
  }
  state.openTabs.forEach((tab) => {
    const id = tab.request?.id;
    if (id && !ids.includes(id)) {
      ids.push(id);
    }
  });
  return ids;
}

function requestIdsForTabSet() {
  const ids = currentSavedRequestIds();
  if (!ids.length) {
    alert("Only saved requests can be added to a tab set.");
    return null;
  }
  if (ids.length > MAX_TAB_SET_TABS) {
    alert(`A tab set can contain up to ${MAX_TAB_SET_TABS} tabs.`);
    return null;
  }
  return ids;
}

async function createTabSetFromOpenTabs() {
  const requestIds = requestIdsForTabSet();
  if (!requestIds) return;
  const name = prompt("Tab set name");
  const trimmed = name?.trim();
  if (!trimmed) return;
  await api("/api/tab-sets", {
    method: "POST",
    body: JSON.stringify({ name: trimmed, request_ids: requestIds }),
  });
  await loadTabSets();
}

function nextEmptyTabSetName() {
  const baseName = "List";
  const existingNames = new Set(state.tabSets.map((tabSet) => String(tabSet.name || "").toLowerCase()));
  let index = 1;
  while (true) {
    const candidate = index === 1 ? baseName : `${baseName} ${index}`;
    if (!existingNames.has(candidate.toLowerCase())) return candidate;
    index += 1;
  }
}

async function createEmptyTabSet() {
  const name = prompt("Tab set name", nextEmptyTabSetName());
  const trimmed = name?.trim();
  if (!trimmed) return;
  const created = await api("/api/tab-sets", {
    method: "POST",
    body: JSON.stringify({ name: trimmed, request_ids: [] }),
  });
  if (created.tab_set?.id) {
    state.expandedTabSets.add(created.tab_set.id);
  }
  await loadTabSets();
}

async function addTabSetToWorkspace(tabSet) {
  if (!confirm(`Add tab set "${tabSet.name}" to the workspace?`)) return;
  const requests = await loadTabSetRequests(tabSet.id, true);
  if (state.activeTabKey) {
    markRequestChanged();
  }
  requests.forEach((request) => {
    if (!state.openTabs.some((tab) => tab.request?.id === request.id)) {
      openRequest(request, { skipSync: true, skipWorkspaceSave: true });
    }
  });
  renderRequestTabs();
  renderCollections();
  scheduleWorkspaceSave();
}

async function renameTabSet(tabSet) {
  const name = prompt("New tab set name", tabSet.name || "");
  const trimmed = name?.trim();
  if (!trimmed) return;
  await api(`/api/tab-sets/${tabSet.id}`, {
    method: "PUT",
    body: JSON.stringify({ name: trimmed }),
  });
  await loadTabSets();
}

async function saveCurrentTabsToTabSet(tabSet) {
  const requestIds = requestIdsForTabSet();
  if (!requestIds) return;
  await api(`/api/tab-sets/${tabSet.id}/requests`, {
    method: "PUT",
    body: JSON.stringify({ request_ids: requestIds }),
  });
  await loadTabSetRequests(tabSet.id, true);
  await loadTabSets();
}

async function deleteTabSet(tabSet) {
  if (!confirm(`Delete tab set "${tabSet.name}"?`)) return;
  await api(`/api/tab-sets/${tabSet.id}`, { method: "DELETE" });
  delete state.tabSetRequests[tabSet.id];
  state.expandedTabSets.delete(tabSet.id);
  await loadTabSets();
}

async function addRequestToTabSet(request, tabSetId) {
  const requestId = Number(request?.id);
  if (!requestId) {
    alert("Save the request before adding it to a tab set.");
    return;
  }
  const tabSet = state.tabSets.find((item) => item.id === Number(tabSetId));
  if (!tabSet) return;
  const requests = await loadTabSetRequests(tabSet.id, true);
  const requestIds = requests.map((item) => item.id);
  if (requestIds.includes(requestId)) {
    alert(`This request is already in "${tabSet.name}".`);
    return;
  }
  if (requestIds.length >= MAX_TAB_SET_TABS) {
    alert(`A tab set can contain up to ${MAX_TAB_SET_TABS} tabs.`);
    return;
  }
  await api(`/api/tab-sets/${tabSet.id}/requests`, {
    method: "PUT",
    body: JSON.stringify({ request_ids: [...requestIds, requestId] }),
  });
  state.expandedTabSets.add(tabSet.id);
  await loadTabSetRequests(tabSet.id, true);
  await loadTabSets();
}

async function removeRequestFromTabSet(tabSet, requestId) {
  const requests = await loadTabSetRequests(tabSet.id, true);
  const requestIds = requests.map((request) => request.id).filter((id) => id !== requestId);
  await api(`/api/tab-sets/${tabSet.id}/requests`, {
    method: "PUT",
    body: JSON.stringify({ request_ids: requestIds }),
  });
  await loadTabSetRequests(tabSet.id, true);
  await loadTabSets();
}

async function moveCollectionBefore(sourceId, targetId) {
  const sourceIndex = state.collections.findIndex((item) => item.id === sourceId);
  const targetIndex = state.collections.findIndex((item) => item.id === targetId);
  if (sourceIndex === -1 || targetIndex === -1 || sourceIndex === targetIndex) return;
  const next = [...state.collections];
  const [source] = next.splice(sourceIndex, 1);
  next.splice(targetIndex, 0, source);
  state.collections = next;
  renderCollections();
  const data = await api("/api/collections/reorder", {
    method: "PUT",
    body: JSON.stringify({ ordered_ids: next.map((item) => item.id) }),
  });
  state.collections = data.collections;
  renderCollections();
}

async function moveRequestBefore(collectionId, sourceId, targetId) {
  const requests = state.requestsByCollection[collectionId] || [];
  const sourceIndex = requests.findIndex((item) => item.id === sourceId);
  const targetIndex = requests.findIndex((item) => item.id === targetId);
  if (sourceIndex === -1 || targetIndex === -1 || sourceIndex === targetIndex) return;
  const next = [...requests];
  const [source] = next.splice(sourceIndex, 1);
  next.splice(targetIndex, 0, source);
  state.requestsByCollection[collectionId] = next;
  renderCollections();
  const data = await api(`/api/collections/${collectionId}/requests/reorder`, {
    method: "PUT",
    body: JSON.stringify({ ordered_ids: next.map((item) => item.id) }),
  });
  state.requestsByCollection[collectionId] = data.requests;
  renderCollections();
}

async function ensureCollectionRequests(id, force = false) {
  if (!force && state.requestsByCollection[id]) {
    return state.requestsByCollection[id];
  }
  const data = await api(`/api/collections/${id}/requests`);
  state.requestsByCollection[id] = data.requests;
  const collection = state.collections.find((item) => item.id === id);
  if (collection) {
    collection.request_count = data.requests.length;
  }
  return data.requests;
}

async function expandCollection(id, selectFirst = false, options = {}) {
  if (selectFirst || !state.currentRequest) {
    state.currentCollectionId = id;
  }
  state.expandedCollections.add(id);
  const requests = await ensureCollectionRequests(id);
  renderCollections();
  if (selectFirst && requests[0]) {
    openRequest(requests[0], { skipSync: true, skipWorkspaceSave: options.skipWorkspaceSave });
  } else if (selectFirst) {
    newRequestDraft({ skipWorkspaceSave: options.skipWorkspaceSave });
  }
}

async function toggleCollection(id) {
  if (!state.currentRequest) {
    state.currentCollectionId = id;
  }
  if (state.expandedCollections.has(id)) {
    state.expandedCollections.delete(id);
    renderCollections();
    return;
  }
  await expandCollection(id);
}

function requestKey(request, options = {}) {
  if (request.tabKey) {
    return request.tabKey;
  }
  if (request.id && !options.forceNew) {
    return `request-${request.id}`;
  }
  if (!request.tabKey) {
    const prefix = request.id ? `request-${request.id}-tab` : "draft";
    request.tabKey = `${prefix}-${state.nextDraftId++}`;
  }
  return request.tabKey;
}

function requestLabel(request) {
  return request.name || deriveRequestName(request.url) || "New request";
}

function deriveRequestName(url) {
  try {
    const parsed = new URL(url);
    const parts = parsed.pathname.split("/").filter(Boolean);
    return parts[parts.length - 1] || parsed.hostname || "New request";
  } catch (_) {
    return "New request";
  }
}

function splitUrlQuery(rawUrl) {
  const value = String(rawUrl || "").trim();
  const hashIndex = value.indexOf("#");
  const hash = hashIndex >= 0 ? value.slice(hashIndex) : "";
  const withoutHash = hashIndex >= 0 ? value.slice(0, hashIndex) : value;
  const queryIndex = withoutHash.indexOf("?");
  if (queryIndex < 0) {
    return { baseUrl: value, params: [] };
  }
  const baseUrl = withoutHash.slice(0, queryIndex) + hash;
  const query = withoutHash.slice(queryIndex + 1);
  const params = [];
  const search = new URLSearchParams(query);
  search.forEach((value, key) => {
    params.push({ key, value, description: "", enabled: true });
  });
  return { baseUrl, params };
}

function buildUrlWithParams(baseUrl, params) {
  const { baseUrl: cleanBase } = splitUrlQuery(baseUrl);
  const enabledParams = (Array.isArray(params) ? params : [])
    .filter((pair) => pair.enabled !== false && pair.key);
  if (!enabledParams.length) {
    return cleanBase;
  }
  const hashIndex = cleanBase.indexOf("#");
  const hash = hashIndex >= 0 ? cleanBase.slice(hashIndex) : "";
  const withoutHash = hashIndex >= 0 ? cleanBase.slice(0, hashIndex) : cleanBase;
  const search = enabledParams
    .map((pair) => `${pair.key}=${pair.value || ""}`)
    .join("&");
  return `${withoutHash}?${search}${hash}`;
}

function paramsFromEditorOrUrl() {
  const editorParams = readPairs("params-rows");
  const urlParams = splitUrlQuery($("request-url").value).params;
  return editorParams.length ? editorParams : urlParams;
}

function updateUrlFromParams() {
  if (state.syncingUrlParams) return;
  state.syncingUrlParams = true;
  try {
    $("request-url").value = buildUrlWithParams($("request-url").value, readPairs("params-rows"));
    markRequestChanged();
    renderRequestTabs();
  } finally {
    state.syncingUrlParams = false;
  }
}

function updateParamsFromUrl() {
  if (state.syncingUrlParams) return;
  state.syncingUrlParams = true;
  try {
    renderPairs("params-rows", splitUrlQuery($("request-url").value).params);
    markRequestChanged();
    renderRequestTabs();
  } finally {
    state.syncingUrlParams = false;
  }
}

function openRequest(request, options = {}) {
  const requestForTab = { ...request };
  if (options.forceNew) {
    delete requestForTab.tabKey;
  }
  const key = requestKey(requestForTab, options);
  let tab = state.openTabs.find((item) => item.key === key);
  if (!tab) {
    tab = { key, request: { ...requestForTab, tabKey: key } };
    state.openTabs.push(tab);
  }
  activateRequestTab(key, options);
  if (!options.skipWorkspaceSave) {
    scheduleWorkspaceSave();
  }
  return true;
}

function activateRequestTab(key, options = {}) {
  if (!options.skipSync) {
    syncActiveTabFromEditor();
  }
  const tab = state.openTabs.find((item) => item.key === key);
  if (!tab) return;
  state.activeTabKey = key;
  populateEditor(tab.request);
  const failed = state.requestAutosaveError?.key === key;
  const pending = requestSaveIsPending(key);
  setRequestSaveStatus(
    failed ? "error" : pending ? "saving" : "current",
    failed ? "Not saved" : pending ? "Saving..." : "Up to date"
  );
  renderRequestTabs();
  renderCollections();
  focusActiveRequestInSidebar();
  if (!options.skipWorkspaceSave) {
    scheduleWorkspaceSave();
  }
}

function collapseCollectionsWithoutOpenTabs(collectionIds) {
  let changed = false;
  collectionIds.forEach((collectionId) => {
    if (!collectionId) return;
    const hasOpenTab = state.openTabs.some((tab) => tab.request?.collection_id === collectionId);
    if (!hasOpenTab && state.expandedCollections.delete(collectionId)) {
      changed = true;
    }
  });
  return changed;
}

function closeRequestTab(key) {
  const index = state.openTabs.findIndex((item) => item.key === key);
  if (index === -1) return;
  const closedCollectionId = state.openTabs[index].request?.collection_id;
  const wasActive = state.activeTabKey === key;
  state.openTabs.splice(index, 1);
  const collapsedCollections = collapseCollectionsWithoutOpenTabs([closedCollectionId]);
  if (wasActive) {
    const next = state.openTabs[Math.min(index, state.openTabs.length - 1)];
    if (next) {
      activateRequestTab(next.key, { skipSync: true });
    } else {
      state.activeTabKey = null;
      state.currentRequest = null;
      state.currentCollectionId = null;
      renderRequestTabs();
      renderCollections();
    }
  } else {
    renderRequestTabs();
    if (collapsedCollections) {
      renderCollections();
    }
  }
  scheduleWorkspaceSave();
}

function renderRequestTabs() {
  const container = $("request-tabs");
  container.innerHTML = "";
  if (state.openTabs.length === 0) {
    setEditorEmptyState(true);
    return;
  }
  setEditorEmptyState(false);
  state.openTabs.forEach((tab) => {
    const method = escapeHtml(tab.request.method || "GET");
    const label = requestLabel(tab.request);
    const button = document.createElement("button");
    button.className = "request-tab"
      + (tab.key === state.activeTabKey ? " active" : "")
      + (tab.request?.id && pendingDeletion("request", tab.request.id) ? " pending-delete" : "");
    button.dataset.tabKey = tab.key;
    button.dataset.tooltip = label;
    button.innerHTML = `
      <b class="method-badge method-${method.toLowerCase()}">${method}</b>
      <span>${escapeHtml(label)}</span>
      <i title="Close">×</i>
    `;
    button.onclick = () => activateRequestTab(tab.key);
    button.onmouseenter = (event) => showQuickTooltip(label, event);
    button.onmousemove = positionQuickTooltip;
    button.onmouseleave = hideQuickTooltip;
    button.oncontextmenu = (event) => {
      event.preventDefault();
      event.stopPropagation();
      hideQuickTooltip();
      showTabContextMenu(event, tab.key);
    };
    button.querySelector("i").onclick = (event) => {
      event.stopPropagation();
      hideQuickTooltip();
      closeRequestTab(tab.key);
    };
    container.appendChild(button);
  });
  window.requestAnimationFrame(scrollActiveRequestTabIntoView);
}

function scrollActiveRequestTabIntoView() {
  const container = $("request-tabs");
  const active = container?.querySelector(".request-tab.active");
  if (!container || !active) return;

  const containerRect = container.getBoundingClientRect();
  const activeRect = active.getBoundingClientRect();
  const sideGap = 10;
  if (activeRect.left < containerRect.left + sideGap) {
    container.scrollLeft += activeRect.left - containerRect.left - sideGap;
  } else if (activeRect.right > containerRect.right - sideGap) {
    container.scrollLeft += activeRect.right - containerRect.right + sideGap;
  }
}

function closeAllRequestTabs() {
  if (!state.openTabs.length) return;
  if (!confirm("Close all tabs?")) return;
  const closedCollectionIds = new Set(state.openTabs.map((tab) => tab.request?.collection_id).filter(Boolean));
  state.openTabs = [];
  state.activeTabKey = null;
  state.currentRequest = null;
  state.currentCollectionId = null;
  collapseCollectionsWithoutOpenTabs(closedCollectionIds);
  renderRequestTabs();
  renderCollections();
  scheduleWorkspaceSave();
}

function duplicateOpenTab(key) {
  if (state.activeTabKey === key) {
    syncActiveTabFromEditor();
  }
  const tab = state.openTabs.find((item) => item.key === key);
  if (!tab) return;
  openRequest({ ...tab.request, tabKey: undefined }, { forceNew: true });
}

async function editOpenTabName(key) {
  if (state.activeTabKey === key) {
    syncActiveTabFromEditor();
  }
  const tab = state.openTabs.find((item) => item.key === key);
  if (!tab) return;
  const name = prompt("New request name", tab.request?.name || "");
  const trimmed = name?.trim();
  if (!trimmed) return;
  tab.request = { ...tab.request, name: trimmed };
  if (state.activeTabKey === key) {
    state.currentRequest = tab.request;
  }
  renderRequestTabs();
  renderCollections();
  scheduleWorkspaceSave();
  if (!tab.request.id) return;

  const saved = await api(`/api/requests/${tab.request.id}`, {
    method: "PUT",
    body: JSON.stringify(tab.request),
  });
  const savedRequest = { ...saved.request, tabKey: key };
  tab.request = savedRequest;
  if (state.activeTabKey === key) {
    state.currentRequest = savedRequest;
    populateEditor(savedRequest);
  }
  await ensureCollectionRequests(saved.request.collection_id, true);
  renderRequestTabs();
  renderCollections();
}

function syncActiveTabFromEditor() {
  if (!state.activeTabKey || !state.currentRequest || !$("request-method")) {
    return;
  }
  const tab = state.openTabs.find((item) => item.key === state.activeTabKey);
  if (tab) {
    tab.request = collectRequest();
    state.currentRequest = tab.request;
  }
}

function setRequestSaveStatus(mode, label) {
  const status = $("request-save-status");
  if (!status) return;
  status.classList.remove("is-current", "is-saving", "is-error");
  status.classList.add(`is-${mode}`);
  const text = status.querySelector(".request-save-status-label");
  if (text) text.textContent = label;
}

function requestSaveIsPending(key = state.activeTabKey) {
  return Boolean(
    key
    && (state.requestAutosaveQueue.has(key) || state.requestAutosaveActiveKey === key)
  );
}

function markRequestChanged({ immediate = false } = {}) {
  if (state.applyingRemoteRequest) return;
  syncActiveTabFromEditor();
  if (!state.activeTabKey || !state.currentRequest) return;
  state.requestAutosaveQueue.set(state.activeTabKey, {
    key: state.activeTabKey,
    request: { ...collectRequest() },
  });
  if (state.requestAutosaveError?.key === state.activeTabKey) {
    state.requestAutosaveError = null;
  }
  setRequestSaveStatus("saving", "Saving...");
  clearTimeout(state.requestAutosaveTimer);
  state.requestAutosaveTimer = setTimeout(flushRequestAutosave, immediate ? 0 : 450);
}

async function flushRequestAutosave() {
  clearTimeout(state.requestAutosaveTimer);
  state.requestAutosaveTimer = null;
  if (state.requestAutosaveInFlight) return;
  state.requestAutosaveInFlight = true;

  try {
    while (state.requestAutosaveQueue.size) {
      const [queuedKey, queued] = state.requestAutosaveQueue.entries().next().value;
      state.requestAutosaveQueue.delete(queuedKey);
      state.requestAutosaveActiveKey = queuedKey;
      const request = queued.request;
      const isExisting = Boolean(request.id);

      try {
        const saved = await api(isExisting ? `/api/requests/${request.id}` : "/api/requests", {
          method: isExisting ? "PUT" : "POST",
          body: JSON.stringify(request),
        });
        const canonicalKey = `request-${saved.request.id}`;
        const tab = state.openTabs.find((item) => item.key === queuedKey);
        const newer = state.requestAutosaveQueue.get(queuedKey);
        const savedRequest = { ...saved.request };

        if (tab) {
          tab.key = canonicalKey;
          tab.request = newer
            ? { ...savedRequest, ...newer.request, id: saved.request.id }
            : savedRequest;
        }
        if (newer) {
          state.requestAutosaveQueue.delete(queuedKey);
          state.requestAutosaveQueue.set(canonicalKey, {
            key: canonicalKey,
            request: { ...newer.request, id: saved.request.id },
          });
        }
        if (state.activeTabKey === queuedKey) state.activeTabKey = canonicalKey;
        if (state.activeTabKey === canonicalKey && tab) state.currentRequest = tab.request;
        if ([queuedKey, canonicalKey].includes(state.requestAutosaveError?.key)) {
          state.requestAutosaveError = null;
        }

        await ensureCollectionRequests(saved.request.collection_id, true);
        renderRequestTabs();
        renderCollections();
        scheduleWorkspaceSave();
      } catch (error) {
        if (!state.requestAutosaveQueue.has(queuedKey)) {
          state.requestAutosaveQueue.set(queuedKey, queued);
        }
        state.requestAutosaveError = { key: queuedKey, error };
        if (state.activeTabKey === queuedKey) {
          setRequestSaveStatus("error", "Not saved");
        }
        state.requestAutosaveTimer = setTimeout(flushRequestAutosave, 2000);
        break;
      } finally {
        state.requestAutosaveActiveKey = null;
      }
    }
  } finally {
    state.requestAutosaveInFlight = false;
    const activeFailed = state.requestAutosaveError?.key === state.activeTabKey;
    const activePending = requestSaveIsPending(state.activeTabKey);
    if (activeFailed) {
      setRequestSaveStatus("error", "Not saved");
    } else if (activePending) {
      setRequestSaveStatus("saving", "Saving...");
    } else {
      setRequestSaveStatus("current", "Up to date");
    }
    if (state.requestAutosaveQueue.size && !state.requestAutosaveTimer) {
      state.requestAutosaveTimer = setTimeout(flushRequestAutosave, 0);
    }
  }
}

function populateEditor(request) {
  setEditorEmptyState(false);
  state.currentRequest = request;
  state.currentCollectionId = request.collection_id;
  state.expandedCollections.add(request.collection_id);
  $("request-method").value = request.method || "GET";
  updateMethodSelect();
  const existingParams = Array.isArray(request.params) ? request.params : [];
  const urlParts = splitUrlQuery(request.url || "");
  const displayParams = existingParams.length ? existingParams : urlParts.params;
  $("request-url").value = buildUrlWithParams(urlParts.baseUrl, displayParams);
  $("request-auth-type").value = request.auth_type === "basic" ? "basic" : "bearer";
  $("request-auth-token").value = request.auth_token || "";
  $("request-basic-username").value = request.basic_auth_username || "";
  $("request-basic-password").value = request.basic_auth_password || "";
  updateAuthEditor();
  renderPairs("params-rows", displayParams);
  setJsonEditor("headers-editor", request.headers || []);
  setJsonEditor("cookies-editor", request.cookies || []);
  setBodyMode(request.body_type || "none");
  $("body-editor").value = request.body_text || "";
  state.bodyFormData = clonePairs(request.form_data || (request.body_type === "form-data" ? request.form : []));
  state.bodyUrlencoded = clonePairs(request.urlencoded || (request.body_type === "form" ? request.form : []));
  state.bodyBinary = { ...(request.binary || {}) };
  state.bodyGraphql = {
    query: request.graphql?.query || "",
    variables: request.graphql?.variables || "{}",
  };
  state.activeBodyMode = getBodyMode();
  renderActiveBodyMode();
  $("pre-request-script").value = request.pre_request_script || "";
  $("post-response-script").value = request.post_response_script || "";
  $("skip-tls-verification").checked =
    request.skip_tls_verification !== false;
  updateTlsWarning();
  updateBodyEditor();
  updateBodyLineNumbers();
  updateScriptLineNumbers("pre");
  updateScriptLineNumbers("post");
}

function clearEditor() {
  $("request-method").value = "GET";
  updateMethodSelect();
  $("request-url").value = "";
  $("request-auth-type").value = "bearer";
  $("request-auth-token").value = "";
  $("request-basic-username").value = "";
  $("request-basic-password").value = "";
  updateAuthEditor();
  renderPairs("params-rows", []);
  setJsonEditor("headers-editor", []);
  setJsonEditor("cookies-editor", []);
  setBodyMode("none");
  $("body-editor").value = "";
  state.bodyFormData = [];
  state.bodyUrlencoded = [];
  state.bodyBinary = {};
  state.bodyGraphql = { query: "", variables: "{}" };
  state.activeBodyMode = "none";
  renderActiveBodyMode();
  $("pre-request-script").value = "";
  $("post-response-script").value = "";
  $("skip-tls-verification").checked = true;
  updateTlsWarning();
  updateBodyEditor();
  updateBodyLineNumbers();
  updateScriptLineNumbers("pre");
  updateScriptLineNumbers("post");
}

function setEditorEmptyState(isEmpty) {
  $("app")?.querySelector(".editor-pane")?.classList.toggle("no-active-request", isEmpty);
  if ($("download-curl-btn")) {
    $("download-curl-btn").disabled = isEmpty;
  }
  if (isEmpty) {
    clearEditor();
  }
}

function newRequestDraft(options = {}) {
  if (!state.currentCollectionId && state.collections[0]) {
    state.currentCollectionId = state.collections[0].id;
  }
  state.expandedCollections.add(state.currentCollectionId);
  const draft = {
    collection_id: state.currentCollectionId,
    name: "New request",
    method: "GET",
    url: "http://localhost:8000/",
    params: [],
    headers: [],
    cookies: [],
    body_type: "none",
    body_text: "",
    form_data: [],
    urlencoded: [],
    binary: {},
    graphql: { query: "", variables: "{}" },
    pre_request_script: "",
    post_response_script: "",
    use_bearer_token: false,
    auth_type: "bearer",
    auth_token: "",
    basic_auth_username: "",
    basic_auth_password: "",
    skip_tls_verification: true,
  };
  openRequest(draft, options);
}

function collectRequest() {
  captureActiveBodyMode();
  const urlParts = splitUrlQuery($("request-url").value);
  const url = urlParts.baseUrl;
  return {
    ...state.currentRequest,
    collection_id: state.currentRequest?.collection_id || state.currentCollectionId,
    name: state.currentRequest?.name || deriveRequestName(url),
    method: $("request-method").value,
    url,
    params: paramsFromEditorOrUrl(),
    headers: parseJsonEditor("headers-editor", []),
    cookies: parseJsonEditor("cookies-editor", []),
    body_type: getBodyType(),
    body_text: $("body-editor").value,
    form_data: clonePairs(state.bodyFormData),
    urlencoded: clonePairs(state.bodyUrlencoded),
    binary: { ...state.bodyBinary },
    graphql: { ...state.bodyGraphql },
    pre_request_script: $("pre-request-script").value,
    post_response_script: $("post-response-script").value,
    use_bearer_token: false,
    auth_type: $("request-auth-type").value === "basic" ? "basic" : "bearer",
    auth_token: $("request-auth-token").value.trim(),
    basic_auth_username: $("request-basic-username").value,
    basic_auth_password: $("request-basic-password").value,
    skip_tls_verification: $("skip-tls-verification").checked,
  };
}

async function saveRequest() {
  markRequestChanged({ immediate: true });
  await flushRequestAutosave();
}

function requestForAction(request) {
  if (request.id && state.currentRequest?.id === request.id) {
    return collectRequest();
  }
  return { ...request };
}

async function editRequestName(request) {
  const base = requestForAction(request);
  const name = prompt("New request name", base.name || "");
  const trimmed = name?.trim();
  if (!trimmed) return;
  const data = { ...base, name: trimmed };
  const saved = await api(`/api/requests/${request.id}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
  await applySavedRequest(saved.request);
}

async function duplicateRequest(request) {
  const base = requestForAction(request);
  const name = `${base.name || deriveRequestName(base.url)} copy`;
  const data = { ...base, id: undefined, name };
  const saved = await api("/api/requests", {
    method: "POST",
    body: JSON.stringify(data),
  });
  state.currentCollectionId = saved.request.collection_id;
  state.expandedCollections.add(saved.request.collection_id);
  await ensureCollectionRequests(saved.request.collection_id, true);
  openRequest(saved.request);
  scheduleWorkspaceSave();
}

async function applySavedRequest(request) {
  state.currentCollectionId = request.collection_id;
  await ensureCollectionRequests(request.collection_id, true);
  const key = requestKey(request);
  const tab = state.openTabs.find((item) => item.key === key);
  if (tab) {
    tab.request = request;
  }
  if (state.currentRequest?.id === request.id) {
    state.currentRequest = request;
    populateEditor(request);
  }
  renderRequestTabs();
  renderCollections();
  scheduleWorkspaceSave();
}

async function sendRequest() {
  if (state.requestAbortController) {
    cancelActiveExecution();
    state.requestAbortController.abort();
  }
  const controller = new AbortController();
  const executionId = createExecutionId();
  const runId = state.requestRunId + 1;
  state.requestRunId = runId;
  state.requestAbortController = controller;
  state.currentExecutionId = executionId;
  syncActiveTabFromEditor();
  let request = collectRequest();
  setRequestRunning(true, request);
  try {
    const preResult = await runRequestScript(request.pre_request_script, {
      request,
      environment: state.env,
    });
    if (controller.signal.aborted || state.requestRunId !== runId) return;
    if (preResult) {
      request = { ...request, ...preResult.request, headers: preResult.request.headers };
      await applyScriptEnvironment(preResult.environment);
      renderScriptResults("Pre-request", preResult);
    }
    const data = await api("/api/execute", {
      method: "POST",
      body: JSON.stringify({ request, execution_id: executionId }),
      signal: controller.signal,
    });
    if (controller.signal.aborted || state.requestRunId !== runId) return;
    const postResult = await runRequestScript(request.post_response_script, {
      request,
      environment: state.env,
      response: data.result,
    });
    if (controller.signal.aborted || state.requestRunId !== runId) return;
    if (postResult) {
      await applyScriptEnvironment(postResult.environment);
      renderScriptResults("Post-response", postResult);
      data.result.script_tests = postResult.tests;
    }
    renderResponse(data.result);
  } catch (error) {
    if (controller.signal.aborted || error.name === "AbortError" || state.requestRunId !== runId) {
      return;
    }
    renderResponse({
      status: 0,
      reason: "",
      duration_ms: 0,
      response_size: 0,
      response_body: "",
      response_headers: {},
      error: error.message,
    });
    throw error;
  } finally {
    if (state.requestRunId === runId) {
      state.requestAbortController = null;
      if (state.currentExecutionId === executionId) {
        state.currentExecutionId = null;
      }
      setRequestRunning(false);
    }
  }
}

async function applyScriptEnvironment(environment) {
  if (!environment || JSON.stringify(environment) === JSON.stringify(state.env)) return;
  const data = await api("/api/env", {
    method: "PUT",
    body: JSON.stringify({ env: environment }),
  });
  state.env = data.env || environment;
}

function renderScriptResults(stage, result) {
  const tests = Array.isArray(result.tests) ? result.tests : [];
  const logs = Array.isArray(result.logs) ? result.logs : [];
  const passed = tests.filter((test) => test.passed).length;
  const testText = tests.length ? `${passed}/${tests.length} tests passed` : "completed";
  const details = [
    ...tests.map((test) => `${test.passed ? "PASS" : "FAIL"}  ${test.name}${test.error ? `: ${test.error}` : ""}`),
    ...logs.map((line) => `LOG   ${line}`),
  ];
  $("script-results").textContent = `${stage}: ${testText}${details.length ? `\n${details.join("\n")}` : ""}`;
  $("script-results").classList.toggle("has-failures", tests.some((test) => !test.passed));
}

function runRequestScript(script, context) {
  const source = String(script || "").trim();
  if (!source) return Promise.resolve(null);
  const workerSource = `
    self.fetch = undefined;
    self.XMLHttpRequest = undefined;
    self.WebSocket = undefined;
    self.EventSource = undefined;
    self.importScripts = undefined;
    self.onmessage = ({ data }) => {
      const tests = [];
      const logs = [];
      const environment = { ...(data.context.environment || {}) };
      const variables = {};
      const request = JSON.parse(JSON.stringify(data.context.request || {}));
      const response = data.context.response || null;
      const headersApi = (items) => ({
        get(name) {
          const found = items.find((item) => String(item.key).toLowerCase() === String(name).toLowerCase() && item.enabled !== false);
          return found ? found.value : undefined;
        },
        add(value) { items.push({ key: String(value.key || ""), value: String(value.value || ""), enabled: true }); },
        upsert(value) {
          const found = items.find((item) => String(item.key).toLowerCase() === String(value.key).toLowerCase());
          if (found) { found.value = String(value.value || ""); found.enabled = true; }
          else this.add(value);
        },
        remove(name) {
          for (let index = items.length - 1; index >= 0; index -= 1) {
            if (String(items[index].key).toLowerCase() === String(name).toLowerCase()) items.splice(index, 1);
          }
        },
        toObject() { return Object.fromEntries(items.filter((item) => item.enabled !== false).map((item) => [item.key, item.value])); },
      });
      const valueApi = (store) => ({
        get(key) { return store[key]; },
        set(key, value) { store[String(key)] = String(value); },
        unset(key) { delete store[String(key)]; },
        clear() { Object.keys(store).forEach((key) => delete store[key]); },
        toObject() { return { ...store }; },
        has(key) { return Object.prototype.hasOwnProperty.call(store, key); },
      });
      const expect = (actual) => ({
        to: {
          equal(expected) { if (actual !== expected) throw new Error('Expected ' + JSON.stringify(actual) + ' to equal ' + JSON.stringify(expected)); },
          eql(expected) { if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error('Values are not deeply equal'); },
          include(expected) { if (!actual?.includes?.(expected)) throw new Error('Expected value to include ' + JSON.stringify(expected)); },
          have: {
            property(key) { if (actual == null || !(key in actual)) throw new Error('Missing property ' + key); },
          },
          be: {
            above(expected) { if (!(actual > expected)) throw new Error('Expected value to be above ' + expected); },
            below(expected) { if (!(actual < expected)) throw new Error('Expected value to be below ' + expected); },
          },
        },
      });
      const responseHeaders = Object.entries(response?.response_headers || {}).map(([key, value]) => ({ key, value, enabled: true }));
      const pmRequest = { ...request, headers: headersApi(request.headers ||= []) };
      const pm = {
        environment: valueApi(environment),
        variables: valueApi(variables),
        request: pmRequest,
        response: response ? {
          code: response.status || 0,
          status: response.reason || '',
          responseTime: response.duration_ms || 0,
          text: () => response.response_body || '',
          json: () => JSON.parse(response.response_body || 'null'),
          headers: headersApi(responseHeaders),
        } : undefined,
        test(name, callback) {
          try { callback(); tests.push({ name: String(name), passed: true }); }
          catch (error) { tests.push({ name: String(name), passed: false, error: error.message }); }
        },
        expect,
      };
      const console = { log: (...values) => logs.push(values.map((value) => typeof value === 'string' ? value : JSON.stringify(value)).join(' ')) };
      try {
        Function('pm', 'console', '"use strict";\\n' + data.script)(pm, console);
        request.method = pmRequest.method;
        request.url = String(pmRequest.url || request.url || '');
        self.postMessage({ ok: true, request, environment, tests, logs });
      } catch (error) {
        self.postMessage({ ok: false, error: error.message, request, environment, tests, logs });
      }
    };
  `;
  return new Promise((resolve, reject) => {
    const workerUrl = URL.createObjectURL(new Blob([workerSource], { type: "text/javascript" }));
    const worker = new Worker(workerUrl);
    const timer = setTimeout(() => {
      worker.terminate();
      URL.revokeObjectURL(workerUrl);
      reject(new Error("Script exceeded the 1000 ms execution limit."));
    }, 1000);
    worker.onmessage = (event) => {
      clearTimeout(timer);
      worker.terminate();
      URL.revokeObjectURL(workerUrl);
      if (event.data.ok) resolve(event.data);
      else reject(new Error(`Script error: ${event.data.error}`));
    };
    worker.onerror = (event) => {
      clearTimeout(timer);
      worker.terminate();
      URL.revokeObjectURL(workerUrl);
      reject(new Error(`Script error: ${event.message}`));
    };
    worker.postMessage({ script: source, context });
  });
}

async function runRequestFromCollection(request) {
  openRequest(request);
  if (state.currentRequest?.id !== request.id) {
    const key = requestKey(request);
    activateRequestTab(key, { skipSync: true });
  }
  await sendRequest();
}

function renderResponse(result) {
  const status = result.status ? `${result.status} ${result.reason || ""}` : "Error";
  $("response-meta-text").textContent = `${status} - ${result.duration_ms}ms - ${result.response_size || 0} bytes`;
  const bodyView = formatBody(result.response_body, result.response_headers);
  state.lastResponseText = bodyView.text || result.error || "";
  state.hasResponse = true;
  $("copy-response-btn").disabled = false;
  $("download-response-btn").disabled = false;
  $("response-output").innerHTML = `
    <section class="response-section response-section-main">
      <pre class="response-code">${escapeHtml(bodyView.text || "")}</pre>
    </section>
    ${result.error ? `
      <section class="response-section response-error-block">
        <pre class="response-code">${escapeHtml(result.error)}</pre>
      </section>
    ` : ""}
  `;
}

function setRequestRunning(isRunning, request = null) {
  state.requestRunning = isRunning;
  if (isRunning) {
    renderResponseLoading(request);
  }
}

function renderResponseLoading(request) {
  state.hasResponse = false;
  state.lastResponseText = "";
  $("copy-response-btn").disabled = true;
  $("download-response-btn").disabled = true;
  $("response-meta-text").textContent = "Sending request...";
  $("response-output").innerHTML = `
    <div class="response-loading">
      <div class="response-spinner" aria-hidden="true"></div>
      <div>
        <strong>Waiting for response</strong>
        <span>${escapeHtml(requestLabel(request || state.currentRequest || {}))}</span>
      </div>
    </div>
  `;
}

function downloadResponseText() {
  if (!state.hasResponse) return;
  const blob = new Blob([state.lastResponseText], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const name = safeFilename(state.currentRequest?.name || "response");
  const stamp = new Date().toISOString().replaceAll(":", "-").replaceAll(".", "-");
  link.href = url;
  link.download = `${name}-${stamp}.txt`;
  link.click();
  URL.revokeObjectURL(url);
}

function renderCurlTemplate(value, variables = state.env) {
  if (typeof value === "string") {
    return value.replace(
      /\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}/g,
      (_, key) => String(variables?.[key] ?? ""),
    );
  }
  if (Array.isArray(value)) {
    return value.map((item) => renderCurlTemplate(item, variables));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        renderCurlTemplate(item, variables),
      ]),
    );
  }
  return value;
}

function bashQuote(value) {
  return `'${String(value ?? "").replaceAll("'", "'\"'\"'")}'`;
}

function enabledCurlPairs(value) {
  return (Array.isArray(value) ? value : []).filter(
    (pair) => pair?.enabled !== false && String(pair?.key || "").trim(),
  );
}

function upsertCurlHeader(headers, key, value, { preserveExisting = false } = {}) {
  const normalizedKey = String(key || "").trim();
  const normalizedValue = String(value ?? "");
  const index = headers.findIndex(
    (header) => header.key.toLowerCase() === normalizedKey.toLowerCase(),
  );
  if (index >= 0) {
    if (!preserveExisting) headers[index] = { key: normalizedKey, value: normalizedValue };
    return;
  }
  headers.push({ key: normalizedKey, value: normalizedValue });
}

function buildCurlUrl(request) {
  const renderedUrl = String(request.url || "").trim();
  const hashIndex = renderedUrl.indexOf("#");
  const withoutHash = hashIndex >= 0 ? renderedUrl.slice(0, hashIndex) : renderedUrl;
  const queryIndex = withoutHash.indexOf("?");
  const baseUrl = queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash;
  const search = new URLSearchParams(
    queryIndex >= 0 ? withoutHash.slice(queryIndex + 1) : "",
  );
  enabledCurlPairs(request.params).forEach((pair) => {
    search.append(String(pair.key), String(pair.value ?? ""));
  });
  const query = search.toString();
  return `${baseUrl}${query ? `?${query}` : ""}`;
}

function buildCurlScript(request) {
  const prepared = renderCurlTemplate(request);
  const headers = [];
  const cookieParts = [];
  const bodyArguments = [];
  const embeddedFiles = [];
  const method = String(prepared.method || "GET").toUpperCase();
  const bodyType = prepared.body_type || "none";

  enabledCurlPairs(prepared.headers).forEach((pair) => {
    const key = String(pair.key).trim();
    const lowerKey = key.toLowerCase();
    if (CURL_BLOCKED_HEADERS.has(lowerKey) || lowerKey === "authorization") return;
    if (lowerKey === "cookie") {
      if (pair.value) cookieParts.push(String(pair.value));
      return;
    }
    upsertCurlHeader(headers, key, pair.value);
  });

  enabledCurlPairs(prepared.cookies).forEach((pair) => {
    cookieParts.push(`${pair.key}=${pair.value ?? ""}`);
  });

  const authType = prepared.auth_type === "basic" ? "basic" : "bearer";
  const bearerToken = String(prepared.auth_token || "").trim();
  if (authType === "bearer" && bearerToken) {
    const value = bearerToken.toLowerCase().startsWith("bearer ")
      ? bearerToken
      : `Bearer ${bearerToken}`;
    upsertCurlHeader(headers, "Authorization", value);
  }

  function embedFile(file, fallbackName) {
    const encoded = String(file?.file_base64 || "").replace(/\s+/g, "");
    if (!file?.file_name || !encoded) {
      throw new Error(`No file is selected for ${fallbackName}.`);
    }
    const index = embeddedFiles.length + 1;
    const cleanName = safeFilename(file.file_name)
      .replace(/[^A-Za-z0-9._ -]+/g, "-")
      .trim() || fallbackName;
    const fileName = `${index}-${cleanName}`;
    embeddedFiles.push({ fileName, encoded });
    return `\${REQAPI_CURL_TMP}/${fileName}`;
  }

  if (bodyType === "json" || bodyType === "raw") {
    upsertCurlHeader(
      headers,
      "Content-Type",
      bodyType === "json" ? "application/json" : "text/plain; charset=utf-8",
      { preserveExisting: true },
    );
    bodyArguments.push(`--data-raw ${bashQuote(prepared.body_text || "")}`);
  } else if (bodyType === "form") {
    upsertCurlHeader(
      headers,
      "Content-Type",
      "application/x-www-form-urlencoded",
      { preserveExisting: true },
    );
    enabledCurlPairs(prepared.urlencoded || prepared.form).forEach((pair) => {
      bodyArguments.push(
        `--data-urlencode ${bashQuote(`${pair.key}=${pair.value ?? ""}`)}`,
      );
    });
  } else if (bodyType === "form-data") {
    enabledCurlPairs(prepared.form_data || prepared.form).forEach((pair) => {
      if (pair.type === "file") {
        const filePath = embedFile(pair, `form-data field "${pair.key}"`);
        const fileType = String(pair.file_type || "application/octet-stream");
        bodyArguments.push(
          `--form ${bashQuote(`${pair.key}=@`)}"${filePath}"${bashQuote(`;type=${fileType}`)}`,
        );
      } else {
        bodyArguments.push(
          `--form-string ${bashQuote(`${pair.key}=${pair.value ?? ""}`)}`,
        );
      }
    });
  } else if (bodyType === "binary") {
    const binary = prepared.binary || {};
    const filePath = embedFile(binary, "binary body");
    upsertCurlHeader(
      headers,
      "Content-Type",
      binary.file_type || "application/octet-stream",
      { preserveExisting: true },
    );
    bodyArguments.push(`--data-binary "@${filePath}"`);
  } else if (bodyType === "graphql") {
    const graphql = prepared.graphql || {};
    let variables;
    try {
      variables = JSON.parse(String(graphql.variables || "{}").trim() || "{}");
    } catch {
      throw new Error("GraphQL variables must be valid JSON before exporting Curl.");
    }
    if (!variables || Array.isArray(variables) || typeof variables !== "object") {
      throw new Error("GraphQL variables must be a JSON object before exporting Curl.");
    }
    upsertCurlHeader(
      headers,
      "Content-Type",
      "application/json",
      { preserveExisting: true },
    );
    bodyArguments.push(
      `--data-raw ${bashQuote(JSON.stringify({
        query: String(graphql.query || ""),
        variables,
      }))}`,
    );
  }

  const argumentsList = [
    `--request ${bashQuote(method)}`,
    `--url ${bashQuote(buildCurlUrl(prepared))}`,
  ];
  if (prepared.skip_tls_verification) {
    argumentsList.push("--insecure");
  }
  headers.forEach((header) => {
    argumentsList.push(`--header ${bashQuote(`${header.key}: ${header.value}`)}`);
  });
  if (authType === "basic") {
    const username = String(prepared.basic_auth_username || "");
    const password = String(prepared.basic_auth_password || "");
    if (username || password) {
      argumentsList.push(`--user ${bashQuote(`${username}:${password}`)}`);
    }
  }
  if (cookieParts.length) {
    argumentsList.push(`--cookie ${bashQuote(cookieParts.join("; "))}`);
  }
  argumentsList.push(...bodyArguments);

  const lines = [];
  if (embeddedFiles.length) {
    lines.push(
      "reqapi_decode_base64() {",
      "  if base64 --help 2>&1 | grep -q -- \"--decode\"; then",
      "    base64 --decode",
      "  else",
      "    base64 -D",
      "  fi",
      "}",
      "",
      'REQAPI_CURL_TMP="$(mktemp -d)"',
      "trap 'rm -rf \"$REQAPI_CURL_TMP\"' EXIT",
    );
    embeddedFiles.forEach((file, index) => {
      const marker = `REQAPI_FILE_${index + 1}`;
      const encodedLines = file.encoded.match(/.{1,76}/g) || [];
      lines.push(
        "",
        `reqapi_decode_base64 > "$REQAPI_CURL_TMP/${file.fileName}" <<'${marker}'`,
        ...encodedLines,
        marker,
      );
    });
  }
  if (lines.length) {
    lines.push("");
  }
  lines.push(
    "curl \\",
    ...argumentsList.map(
      (argument, index) => `  ${argument}${index < argumentsList.length - 1 ? " \\" : ""}`,
    ),
    "",
  );
  return lines.join("\n");
}

function downloadCurlScript() {
  if (!state.currentRequest) return;
  const request = collectRequest();
  const script = buildCurlScript(request);
  const blob = new Blob([script], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "curl.txt";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
  const button = $("download-curl-btn");
  const original = button.textContent;
  button.textContent = "Downloaded";
  window.setTimeout(() => {
    button.textContent = original;
  }, 1200);
}

async function copyResponseText() {
  if (!state.hasResponse) return;
  await navigator.clipboard.writeText(state.lastResponseText);
  const button = $("copy-response-btn");
  const original = button.textContent;
  button.textContent = "Copied";
  window.setTimeout(() => {
    button.textContent = original;
  }, 1200);
}

async function copyRequestBodyText() {
  const mode = getBodyMode();
  if (mode !== "raw") {
    return;
  }
  const text = $("body-editor").value || "";
  await navigator.clipboard.writeText(text);
  const button = $("copy-body-btn");
  const original = button.textContent;
  button.textContent = "Copied";
  window.setTimeout(() => {
    button.textContent = original;
  }, 1200);
}

function showRequestContextMenu(event, request) {
  state.contextRequest = request;
  state.contextCollection = null;
  state.contextTabKey = null;
  state.contextTabSet = null;
  state.contextTabSetRequest = null;
  renderAddInSetOptions("request-add-set-menu", "request-add-set-options", request);
  showContextMenu("request-context-menu", event);
}

function showCollectionContextMenu(event, collection) {
  state.contextCollection = collection;
  state.contextRequest = null;
  state.contextTabKey = null;
  state.contextTabSet = null;
  state.contextTabSetRequest = null;
  renderAddInSetOptions("request-add-set-menu", "request-add-set-options", null);
  showContextMenu("request-context-menu", event);
}

function isAdmin() {
  return state.user?.role === "admin";
}

async function deleteRequestPermanently(request) {
  if (!request?.id) return;
  if (!isAdmin()) {
    throw new Error("Administrator access is required to delete requests.");
  }

  const requestName = request.name || request.url || "this request";
  if (
    !confirm(
      `Delete request "${requestName}" permanently? This action cannot be undone.`,
    )
  ) {
    return;
  }

  try {
    await api(`/api/requests/${request.id}`, { method: "DELETE" });
    window.location.reload();
  } catch (error) {
    alert(error.message);
  }
}

async function deleteCollectionPermanently(collection) {
  if (!collection?.id) return;
  if (!isAdmin()) {
    throw new Error("Administrator access is required to delete collections.");
  }

  const collectionName = collection.name || "this collection";
  if (
    !confirm(
      `Delete collection "${collectionName}" and all its requests permanently? This action cannot be undone.`,
    )
  ) {
    return;
  }

  try {
    await api(`/api/collections/${collection.id}`, { method: "DELETE" });
    window.location.reload();
  } catch (error) {
    alert(error.message);
  }
}

function updateAdminControls() {
  const sidebarActions = document.querySelector(".sidebar-actions");
  const sidebar = document.querySelector(".sidebar");
  const accountSettings = $("account-settings");
  const admin = isAdmin();
  if (sidebar) {
    sidebar.classList.toggle("is-admin", admin);
    sidebar.classList.toggle("is-user", !admin);
  }
  if (sidebarActions) {
    sidebarActions.classList.toggle("hidden", !admin);
  }
  if (accountSettings) {
    accountSettings.classList.toggle("hidden", !admin);
  }
  document.querySelectorAll(".admin-only").forEach((element) => {
    element.classList.toggle("hidden", !admin);
  });
  if (!admin) {
    closeAdminUsersModal();
  }
}

function closeAdminUsersModal() {
  $("admin-users-modal")?.classList.add("hidden");
  if ($("admin-users-message")) {
    $("admin-users-message").textContent = "";
    $("admin-users-message").classList.remove("error");
  }
}

async function openAdminUsersModal() {
  if (!isAdmin()) return;
  $("admin-users-modal").classList.remove("hidden");
  await loadAdminUsers();
  $("admin-new-password").value = "";
  $("admin-confirm-password").value = "";
}

async function loadAdminUsers() {
  const data = await api("/api/admin/users");
  state.adminUsers = data.users || [];
  renderAdminUsers();
}

function renderAdminUsers() {
  const list = $("admin-users-list");
  const select = $("admin-user-select");
  if (!list || !select) return;
  const selectedUserId = select.value;
  list.innerHTML = "";
  select.innerHTML = "";
  if (!state.adminUsers.length) {
    list.innerHTML = '<div class="admin-empty">No user accounts yet.</div>';
    select.disabled = true;
    $("admin-password-form").querySelector("button[type='submit']").disabled = true;
    return;
  }
  select.disabled = false;
  $("admin-password-form").querySelector("button[type='submit']").disabled = false;
  state.adminUsers.forEach((user) => {
    const option = document.createElement("option");
    option.value = user.id;
    option.textContent = user.username;
    select.appendChild(option);

    const row = document.createElement("div");
    row.className = "admin-user-row";
    row.innerHTML = `
      <div class="admin-user-identity">
        <strong></strong>
        <span></span>
      </div>
      <label class="admin-role-toggle">
        <input type="checkbox">
        <span class="admin-role-switch" aria-hidden="true"></span>
        <span class="admin-role-label">Administrator</span>
      </label>
      <div class="admin-user-actions">
        <button class="admin-user-select" type="button">Select</button>
        ${Number(user.id) === Number(state.user?.id)
          ? ""
          : '<button class="admin-user-delete" type="button">Delete</button>'}
      </div>
    `;
    const identity = row.querySelector(".admin-user-identity");
    identity.querySelector("strong").textContent = user.username;
    identity.querySelector("span").textContent = user.role === "admin"
      ? "Administrator"
      : "Standard user";
    const roleInput = row.querySelector(".admin-role-toggle input");
    roleInput.setAttribute("aria-label", `Administrator access for ${user.username}`);
    roleInput.checked = user.role === "admin";
    roleInput.onchange = () => updateManagedUserRole(
      user.id,
      roleInput.checked ? "admin" : "user",
      roleInput,
    );
    row.querySelector(".admin-user-select").onclick = () => {
      select.value = String(user.id);
      $("admin-new-password").focus();
    };
    const deleteButton = row.querySelector(".admin-user-delete");
    if (deleteButton) {
      deleteButton.onclick = () => deleteManagedUser(user, deleteButton);
    }
    list.appendChild(row);
  });
  if (selectedUserId && state.adminUsers.some((user) => String(user.id) === selectedUserId)) {
    select.value = selectedUserId;
  }
}

async function deleteManagedUser(user, button) {
  if (
    !confirm(
      `Delete account "${user.username}" permanently? This action cannot be undone.`,
    )
  ) {
    return;
  }
  button.disabled = true;
  try {
    await api(`/api/admin/users/${user.id}`, { method: "DELETE" });
    setAdminUsersMessage(`Account "${user.username}" was deleted.`);
    await loadAdminUsers();
  } catch (error) {
    button.disabled = false;
    setAdminUsersMessage(error.message, true);
  }
}

async function updateManagedUserRole(userId, role, checkbox) {
  checkbox.disabled = true;
  try {
    const data = await api(`/api/admin/users/${userId}/role`, {
      method: "PUT",
      body: JSON.stringify({ role }),
    });
    const index = state.adminUsers.findIndex((user) => Number(user.id) === Number(userId));
    if (index >= 0) state.adminUsers[index] = data.user;
    renderAdminUsers();
    setAdminUsersMessage(
      role === "admin"
        ? `${data.user.username} is now an administrator.`
        : `${data.user.username} is now a standard user.`,
    );
  } catch (error) {
    checkbox.checked = role !== "admin";
    checkbox.disabled = false;
    setAdminUsersMessage(error.message, true);
  }
}

function setAdminUsersMessage(message, isError = false) {
  const target = $("admin-users-message");
  target.textContent = message;
  target.classList.toggle("error", isError);
}

function startOnboarding(manual = false) {
  if (!state.user || state.onboardingActive) return;
  closeAdminUsersModal();
  hideContextMenus();
  state.onboardingSteps = onboardingTips.filter((tip) => isOnboardingTipAvailable(tip));
  if (!state.onboardingSteps.length) return;
  state.onboardingActive = true;
  state.onboardingManual = manual;
  state.onboardingStep = 0;
  $("onboarding-overlay").classList.remove("hidden");
  renderOnboarding();
}

function isOnboardingTipAvailable(tip) {
  if (tip.adminOnly && !isAdmin()) return false;
  if (tip.userOnly && isAdmin()) return false;
  const target = document.querySelector(tip.target);
  if (!target) return false;
  const style = window.getComputedStyle(target);
  const rect = target.getBoundingClientRect();
  return (
    style.display !== "none" &&
    style.visibility !== "hidden" &&
    rect.width > 0 &&
    rect.height > 0
  );
}

function renderOnboarding() {
  const steps = state.onboardingSteps;
  const tip = steps[state.onboardingStep];
  if (!tip) {
    finishOnboarding(true);
    return;
  }
  $("onboarding-step").textContent = `${state.onboardingStep + 1} / ${steps.length}`;
  $("onboarding-title").textContent = tip.title;
  $("onboarding-text").textContent = tip.text;
  $("onboarding-next").textContent =
    state.onboardingStep === steps.length - 1 ? "Done" : "Next";
  const target = document.querySelector(tip.target);
  target?.scrollIntoView({ block: "nearest", inline: "nearest" });
  window.requestAnimationFrame(positionOnboarding);
}

function positionOnboarding() {
  if (!state.onboardingActive) return;
  const tip = state.onboardingSteps[state.onboardingStep];
  const target = tip ? document.querySelector(tip.target) : null;
  const card = $("onboarding-card");
  const focus = $("onboarding-focus");
  const connector = $("onboarding-connector");
  if (!target || !card || !focus || !connector) return;

  const targetRect = target.getBoundingClientRect();
  const focusPadding = 6;
  focus.style.left = `${Math.max(4, targetRect.left - focusPadding)}px`;
  focus.style.top = `${Math.max(4, targetRect.top - focusPadding)}px`;
  focus.style.width = `${Math.min(window.innerWidth - 8, targetRect.width + focusPadding * 2)}px`;
  focus.style.height = `${Math.min(window.innerHeight - 8, targetRect.height + focusPadding * 2)}px`;

  card.style.visibility = "hidden";
  const cardRect = card.getBoundingClientRect();
  const viewportMargin = 16;
  const gap = 20;
  const centerX = targetRect.left + targetRect.width / 2;
  const centerY = targetRect.top + targetRect.height / 2;
  const positions = {
    right: { x: targetRect.right + gap, y: centerY - cardRect.height / 2 },
    left: { x: targetRect.left - cardRect.width - gap, y: centerY - cardRect.height / 2 },
    bottom: { x: centerX - cardRect.width / 2, y: targetRect.bottom + gap },
    top: { x: centerX - cardRect.width / 2, y: targetRect.top - cardRect.height - gap },
  };
  let order = ["bottom", "top", "right", "left"];
  if (centerX < window.innerWidth / 3) order = ["right", "bottom", "top", "left"];
  if (centerX > (window.innerWidth * 2) / 3) order = ["left", "bottom", "top", "right"];
  const fits = ({ x, y }) =>
    x >= viewportMargin &&
    y >= viewportMargin &&
    x + cardRect.width <= window.innerWidth - viewportMargin &&
    y + cardRect.height <= window.innerHeight - viewportMargin;
  const position = positions[order.find((name) => fits(positions[name]))] || positions.bottom;
  const cardX = Math.min(
    Math.max(viewportMargin, position.x),
    window.innerWidth - cardRect.width - viewportMargin,
  );
  const cardY = Math.min(
    Math.max(viewportMargin, position.y),
    window.innerHeight - cardRect.height - viewportMargin,
  );
  card.style.left = `${cardX}px`;
  card.style.top = `${cardY}px`;
  card.style.visibility = "visible";

  const startX = cardX + cardRect.width / 2;
  const startY = cardY + cardRect.height / 2;
  const towardCardX = startX - centerX;
  const towardCardY = startY - centerY;
  const halfFocusWidth = targetRect.width / 2 + focusPadding;
  const halfFocusHeight = targetRect.height / 2 + focusPadding;
  const boundaryScale = Math.min(
    towardCardX === 0 ? Number.POSITIVE_INFINITY : halfFocusWidth / Math.abs(towardCardX),
    towardCardY === 0 ? Number.POSITIVE_INFINITY : halfFocusHeight / Math.abs(towardCardY),
  );
  const boundaryX = centerX + towardCardX * boundaryScale;
  const boundaryY = centerY + towardCardY * boundaryScale;
  const toBoundaryX = boundaryX - startX;
  const toBoundaryY = boundaryY - startY;
  const boundaryDistance = Math.hypot(toBoundaryX, toBoundaryY);
  const arrowClearance = Math.min(8, boundaryDistance / 2);
  const directionX = boundaryDistance ? toBoundaryX / boundaryDistance : 0;
  const directionY = boundaryDistance ? toBoundaryY / boundaryDistance : 0;
  const endX = boundaryX - directionX * arrowClearance;
  const endY = boundaryY - directionY * arrowClearance;
  const deltaX = endX - startX;
  const deltaY = endY - startY;
  connector.style.left = `${startX}px`;
  connector.style.top = `${startY}px`;
  connector.style.width = `${Math.hypot(deltaX, deltaY)}px`;
  connector.style.transform = `rotate(${Math.atan2(deltaY, deltaX)}rad)`;
}

async function finishOnboarding(markSeen = true) {
  if (!state.onboardingActive) return;
  state.onboardingActive = false;
  state.onboardingSteps = [];
  $("onboarding-overlay").classList.add("hidden");
  if (markSeen && !state.onboardingManual && !state.onboardingSeen) {
    try {
      const data = await api("/api/onboarding", {
        method: "PUT",
        body: JSON.stringify({ seen: true }),
      });
      state.onboardingSeen = Boolean(data.onboarding_seen);
    } catch (error) {
      console.warn(error);
    }
  }
}

function nextOnboardingStep() {
  if (state.onboardingStep >= state.onboardingSteps.length - 1) {
    finishOnboarding(true);
    return;
  }
  state.onboardingStep += 1;
  renderOnboarding();
}

function renderAddInSetOptions(menuId, optionsId, request) {
  const menu = $(menuId);
  const options = $(optionsId);
  if (!menu || !options) return;
  const canAdd = Boolean(request?.id) && state.tabSets.length > 0;
  menu.classList.toggle("hidden", !canAdd);
  options.innerHTML = "";
  if (!canAdd) return;
  sortedTabSets().forEach((tabSet) => {
    const button = document.createElement("button");
    button.dataset.tabSetId = tabSet.id;
    button.textContent = tabSet.name;
    button.title = tabSet.name;
    options.appendChild(button);
  });
}

function sortedTabSets() {
  return [...state.tabSets].sort((left, right) =>
    String(left.name || "").localeCompare(String(right.name || ""), "en", {
      sensitivity: "base",
    })
  );
}

function showTabContextMenu(event, key) {
  state.contextTabKey = key;
  state.contextRequest = null;
  state.contextCollection = null;
  state.contextTabSet = null;
  state.contextTabSetRequest = null;
  const tab = state.openTabs.find((item) => item.key === key);
  renderAddInSetOptions("tab-add-set-menu", "tab-add-set-options", tab?.request);
  showContextMenu("tab-context-menu", event);
}

function showTabSetContextMenu(event, tabSet) {
  state.contextTabSet = tabSet;
  state.contextRequest = null;
  state.contextCollection = null;
  state.contextTabKey = null;
  state.contextTabSetRequest = null;
  showContextMenu("tab-set-context-menu", event);
}

function showTabSetRequestContextMenu(event, tabSet, request) {
  state.contextTabSet = tabSet;
  state.contextTabSetRequest = request;
  state.contextRequest = null;
  state.contextCollection = null;
  state.contextTabKey = null;
  showContextMenu("tab-set-request-context-menu", event);
}

function showContextMenu(menuId, event) {
  hideContextMenuElements();
  const menu = $(menuId);
  menu.classList.remove("hidden");
  const rect = menu.getBoundingClientRect();
  const left = Math.min(event.clientX, window.innerWidth - rect.width - 8);
  const top = Math.min(event.clientY, window.innerHeight - rect.height - 8);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
}

function hideContextMenuElements() {
  $("request-context-menu").classList.add("hidden");
  $("tab-context-menu").classList.add("hidden");
  $("tab-set-context-menu").classList.add("hidden");
  $("tab-set-request-context-menu").classList.add("hidden");
}

function hideContextMenus() {
  hideContextMenuElements();
  state.contextRequest = null;
  state.contextCollection = null;
  state.contextTabKey = null;
  state.contextTabSet = null;
  state.contextTabSetRequest = null;
}

function parseJsonSequence(text) {
  const values = [];
  let cursor = 0;

  while (cursor < text.length) {
    while (/\s/.test(text[cursor] || "")) cursor += 1;
    if (cursor >= text.length) break;
    if (text[cursor] !== "{" && text[cursor] !== "[") return null;

    const start = cursor;
    let depth = 0;
    let inString = false;
    let escaped = false;
    let complete = false;

    for (; cursor < text.length; cursor += 1) {
      const character = text[cursor];
      if (inString) {
        if (escaped) escaped = false;
        else if (character === "\\") escaped = true;
        else if (character === '"') inString = false;
        continue;
      }
      if (character === '"') {
        inString = true;
      } else if (character === "{" || character === "[") {
        depth += 1;
      } else if (character === "}" || character === "]") {
        depth -= 1;
        if (depth < 0) return null;
        if (depth === 0) {
          try {
            values.push(JSON.parse(text.slice(start, cursor + 1)));
          } catch (_) {
            return null;
          }
          cursor += 1;
          complete = true;
          break;
        }
      }
    }
    if (!complete || inString || depth !== 0) return null;
  }

  return values.length > 1 ? values : null;
}

function formatBody(body, headers = {}) {
  if (!body) {
    return { kind: "", text: "" };
  }
  const rawContentType = Object.entries(headers || {})
    .find(([key]) => key.toLowerCase() === "content-type")?.[1] || "";
  const contentType = Array.isArray(rawContentType)
    ? rawContentType.join("; ")
    : String(rawContentType);
  const trimmed = String(body).trim();
  const looksLikeJson = contentType.includes("json") || trimmed.startsWith("{") || trimmed.startsWith("[");
  if (looksLikeJson) {
    try {
      return { kind: "JSON", text: JSON.stringify(JSON.parse(trimmed), null, 2) };
    } catch (_) {
      const sequence = parseJsonSequence(trimmed);
      if (sequence) {
        return { kind: "JSON", text: JSON.stringify(sequence, null, 2) };
      }
      return { kind: "text", text: String(body) };
    }
  }
  return { kind: "text", text: String(body) };
}

async function loadEnv() {
  const data = await api("/api/env");
  state.env = data.env;
  setJsonEditor("env-editor", state.env);
}

async function saveEnv() {
  const env = parseJsonEditor("env-editor", {});
  const data = await api("/api/env", {
    method: "PUT",
    body: JSON.stringify({ env }),
  });
  state.env = data.env;
  setJsonEditor("env-editor", state.env);
}

async function createCollection() {
  const name = prompt("Collection name");
  if (!name) return;
  const data = await api("/api/collections", {
    method: "POST",
    body: JSON.stringify({ name, description: "" }),
  });
  state.currentCollectionId = data.collection.id;
  state.expandedCollections.add(data.collection.id);
  state.requestsByCollection[data.collection.id] = [];
  await loadCollections();
  newRequestDraft();
}

async function editCollectionName(collection) {
  const name = prompt("New collection name", collection.name || "");
  const trimmed = name?.trim();
  if (!trimmed) return;
  const saved = await api(`/api/collections/${collection.id}`, {
    method: "PUT",
    body: JSON.stringify({ ...collection, name: trimmed }),
  });
  state.currentCollectionId = saved.collection.id;
  await loadCollections();
}

async function duplicateCollection(collection) {
  const requests = await ensureCollectionRequests(collection.id, true);
  const created = await api("/api/collections", {
    method: "POST",
    body: JSON.stringify({
      name: `${collection.name || "Collection"} copy`,
      description: collection.description || "",
    }),
  });
  state.currentCollectionId = created.collection.id;
  state.expandedCollections.add(created.collection.id);
  state.requestsByCollection[created.collection.id] = [];
  for (const request of requests) {
    await api("/api/requests", {
      method: "POST",
      body: JSON.stringify({
        ...request,
        id: undefined,
        collection_id: created.collection.id,
      }),
    });
  }
  await loadCollections();
  await ensureCollectionRequests(created.collection.id, true);
  renderCollections();
}

async function exportCollections() {
  const data = await api("/api/export");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "reqapi-export.json";
  link.click();
  URL.revokeObjectURL(url);
}

async function importCollections(file) {
  const text = await file.text();
  await api("/api/import", { method: "POST", body: text });
  await loadCollections();
}

function clonePairs(pairs) {
  return Array.isArray(pairs) ? pairs.map((pair) => ({ ...pair })) : [];
}

function captureActiveBodyMode() {
  if (state.activeBodyMode === "form-data") {
    state.bodyFormData = readPairs("body-form-rows");
  } else if (state.activeBodyMode === "form") {
    state.bodyUrlencoded = readPairs("body-form-rows").map((pair) => ({ ...pair, type: "text" }));
  } else if (state.activeBodyMode === "graphql") {
    state.bodyGraphql = {
      query: $("graphql-query").value,
      variables: $("graphql-variables").value || "{}",
    };
  }
}

function renderBinaryFile() {
  const binary = state.bodyBinary || {};
  $("binary-file-name").textContent = binary.file_name
    ? `${binary.file_name} · ${formatFileSize(binary.file_size)}`
    : "No file selected";
  $("binary-file-name").title = binary.file_name || "";
  $("choose-binary-file-btn").textContent = binary.file_name ? "Replace file" : "Select file";
  $("remove-binary-file-btn").classList.toggle("is-visible", Boolean(binary.file_name));
}

function renderActiveBodyMode() {
  const mode = getBodyMode();
  if (mode === "form-data") {
    renderPairs("body-form-rows", state.bodyFormData);
  } else if (mode === "form") {
    renderPairs("body-form-rows", state.bodyUrlencoded);
  }
  $("graphql-query").value = state.bodyGraphql.query || "";
  $("graphql-variables").value = state.bodyGraphql.variables || "{}";
  updateGraphqlLineNumbers("query");
  updateGraphqlLineNumbers("variables");
  renderBinaryFile();
}

function switchBodyMode() {
  captureActiveBodyMode();
  state.activeBodyMode = getBodyMode();
  renderActiveBodyMode();
  updateBodyEditor();
}

function updateBodyEditor() {
  const mode = getBodyMode();
  $("body-empty").classList.toggle("body-panel-inactive", mode !== "none");
  $("body-raw-panel").classList.toggle("body-panel-inactive", mode !== "raw");
  $("body-form-panel").classList.toggle("body-panel-inactive", mode !== "form" && mode !== "form-data");
  $("body-binary-panel").classList.toggle("body-panel-inactive", mode !== "binary");
  $("body-graphql-panel").classList.toggle("body-panel-inactive", mode !== "graphql");
  $("body-form-panel").classList.toggle("urlencoded-mode", mode === "form");
  $("body-form-title").textContent = mode === "form-data" ? "Form-data fields" : "Urlencoded fields";
  $("body-format").disabled = mode !== "raw";
  $("body-format").classList.toggle("hidden", mode !== "raw");
  $("copy-body-btn").classList.toggle("hidden", mode !== "raw");
  syncActiveTabFromEditor();
}

function updateTlsWarning() {
  $("tls-warning").classList.toggle("hidden", !$("skip-tls-verification").checked);
}

function updateAuthEditor() {
  const authType = $("request-auth-type").value === "basic" ? "basic" : "bearer";
  $("bearer-auth-panel").classList.toggle("hidden", authType !== "bearer");
  $("basic-auth-panel").classList.toggle("hidden", authType !== "basic");
}

function renderPairs(containerId, pairs) {
  const container = $(containerId);
  container.innerHTML = "";
  const rows = Array.isArray(pairs) && pairs.length ? pairs : [{ key: "", value: "", description: "", enabled: true }];
  rows.forEach((pair) => addPairRow(containerId, pair));
}

function addPairRow(containerId, pair = {}) {
  if (containerId === "body-form-rows") {
    addBodyFormRow(pair);
    return;
  }
  const row = document.createElement("div");
  row.className = "kv-row";
  row.innerHTML = `
    <label class="kv-enabled"><input type="checkbox" ${pair.enabled === false ? "" : "checked"} /></label>
    <input class="kv-key" placeholder="Key" value="${escapeAttribute(pair.key || "")}" />
    <input class="kv-value" placeholder="Value" value="${escapeAttribute(pair.value || "")}" />
    <input class="kv-description" placeholder="Description" value="${escapeAttribute(pair.description || "")}" />
    <button class="kv-delete" title="Delete">×</button>
  `;
  row.querySelector(".kv-delete").onclick = () => {
    row.remove();
    if ($(containerId).children.length === 0) {
      addPairRow(containerId);
    }
    if (containerId === "params-rows") {
      updateUrlFromParams();
    } else if (containerId === "body-form-rows") {
      markRequestChanged();
    }
  };
  if (containerId === "params-rows") {
    row.querySelector(".kv-enabled input").onchange = updateUrlFromParams;
    row.querySelector(".kv-key").oninput = updateUrlFromParams;
    row.querySelector(".kv-value").oninput = updateUrlFromParams;
  }
  $(containerId).appendChild(row);
}

function formatFileSize(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error || new Error("Unable to read the selected file."));
    reader.readAsDataURL(file);
  });
}

function addBodyFormRow(pair = {}) {
  const row = document.createElement("div");
  const initialType = pair.type === "file" ? "file" : "text";
  row.className = "kv-row body-form-row";
  row._filePayload = initialType === "file"
    ? {
        file_name: pair.file_name || "",
        file_type: pair.file_type || "application/octet-stream",
        file_size: Number(pair.file_size) || 0,
        file_base64: pair.file_base64 || "",
      }
    : null;
  row.innerHTML = `
    <label class="kv-enabled"><input type="checkbox" ${pair.enabled === false ? "" : "checked"} /></label>
    <input class="kv-key" placeholder="Key" value="${escapeAttribute(pair.key || "")}" />
    <select class="kv-type kv-type-cell" aria-label="Field type">
      <option value="text" ${initialType === "text" ? "selected" : ""}>Text</option>
      <option value="file" ${initialType === "file" ? "selected" : ""}>File</option>
    </select>
    <div class="kv-value-cell">
      <input class="kv-value" placeholder="Value" value="${escapeAttribute(pair.value || "")}" />
      <div class="kv-file-value">
        <button class="file-picker-btn" type="button">Choose file</button>
        <span class="file-name"></span>
        <input class="kv-file-input" type="file" hidden />
      </div>
    </div>
    <input class="kv-description" placeholder="Description" value="${escapeAttribute(pair.description || "")}" />
    <button class="kv-delete" title="Delete" type="button">×</button>
  `;

  const typeSelect = row.querySelector(".kv-type");
  const textInput = row.querySelector(".kv-value");
  const fileValue = row.querySelector(".kv-file-value");
  const fileInput = row.querySelector(".kv-file-input");
  const fileName = row.querySelector(".file-name");
  const picker = row.querySelector(".file-picker-btn");

  const renderValueType = () => {
    const isFile = typeSelect.value === "file" && getBodyMode() === "form-data";
    textInput.classList.toggle("hidden", isFile);
    fileValue.classList.toggle("hidden", !isFile);
    const payload = row._filePayload;
    fileName.textContent = payload?.file_name
      ? `${payload.file_name} · ${formatFileSize(payload.file_size)}`
      : "No file selected";
    fileName.title = payload?.file_name || "";
  };

  picker.onclick = () => fileInput.click();
  fileInput.onchange = async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    if (file.size > MAX_FORM_FILE_BYTES) {
      fileInput.value = "";
      alert(`The selected file is larger than ${formatFileSize(MAX_FORM_FILE_BYTES)}.`);
      return;
    }
    picker.disabled = true;
    picker.textContent = "Reading...";
    try {
      row._filePayload = {
        file_name: file.name,
        file_type: file.type || "application/octet-stream",
        file_size: file.size,
        file_base64: await fileToBase64(file),
      };
      renderValueType();
      markRequestChanged();
    } catch (error) {
      alert(error.message);
    } finally {
      picker.disabled = false;
      picker.textContent = row._filePayload?.file_name ? "Replace file" : "Choose file";
      fileInput.value = "";
    }
  };
  typeSelect.onchange = () => {
    if (typeSelect.value === "file" && !row._filePayload) {
      row._filePayload = {
        file_name: "",
        file_type: "application/octet-stream",
        file_size: 0,
        file_base64: "",
      };
    }
    renderValueType();
    markRequestChanged();
  };
  row.querySelector(".kv-delete").onclick = () => {
    row.remove();
    if ($("body-form-rows").children.length === 0) addBodyFormRow();
    markRequestChanged();
  };
  row.querySelector(".kv-enabled input").onchange = markRequestChanged;
  row.querySelector(".kv-key").oninput = markRequestChanged;
  textInput.oninput = markRequestChanged;
  row.querySelector(".kv-description").oninput = markRequestChanged;
  renderValueType();
  if (row._filePayload?.file_name) picker.textContent = "Replace file";
  $("body-form-rows").appendChild(row);
}

function readPairs(containerId) {
  return Array.from($(containerId).querySelectorAll(".kv-row"))
    .map((row) => {
      const pair = {
        enabled: row.querySelector(".kv-enabled input").checked,
        key: row.querySelector(".kv-key").value.trim(),
        value: row.querySelector(".kv-value").value,
        description: row.querySelector(".kv-description").value.trim(),
      };
      if (containerId === "body-form-rows") {
        pair.type = row.querySelector(".kv-type").value === "file" ? "file" : "text";
        if (pair.type === "file") Object.assign(pair, row._filePayload || {});
      }
      return pair;
    })
    .filter((pair) => pair.key || pair.value || pair.description || pair.file_name);
}

function setBodyMode(bodyType) {
  const directModes = ["none", "form", "form-data", "binary", "graphql"];
  const mode = directModes.includes(bodyType) ? bodyType : "raw";
  const radio = document.querySelector(`input[name="body-mode"][value="${mode}"]`);
  if (radio) radio.checked = true;
  $("body-format").value = bodyType === "json" ? "json" : "text";
}

function getBodyMode() {
  return document.querySelector('input[name="body-mode"]:checked')?.value || "none";
}

function getBodyType() {
  const mode = getBodyMode();
  if (mode === "raw") {
    return $("body-format").value === "json" ? "json" : "raw";
  }
  if (["form", "form-data", "binary", "graphql"].includes(mode)) {
    return mode;
  }
  return mode;
}

function updateMethodSelect() {
  const select = $("request-method");
  select.classList.remove(...methodClassNames);
  select.classList.add(`method-${select.value.toLowerCase()}`);
}

function updateBodyLineNumbers() {
  const lines = Math.max(1, $("body-editor").value.split("\n").length);
  $("body-line-numbers").textContent = Array.from({ length: lines }, (_, index) => index + 1).join("\n");
  syncBodyLineScroll();
}

function syncBodyLineScroll() {
  $("body-line-numbers").scrollTop = $("body-editor").scrollTop;
}

function updateScriptLineNumbers(type) {
  const textarea = type === "post" ? $("post-response-script") : $("pre-request-script");
  const panel = type === "post" ? $("post-response-script-panel") : $("pre-request-script-panel");
  const lineNumbers = panel.querySelector(".line-numbers");
  const lines = Math.max(1, textarea.value.split("\n").length);
  lineNumbers.textContent = Array.from({ length: lines }, (_, index) => index + 1).join("\n");
  lineNumbers.scrollTop = textarea.scrollTop;
}

function updateGraphqlLineNumbers(type) {
  const textarea = type === "variables" ? $("graphql-variables") : $("graphql-query");
  const lineNumbers = $(type === "variables" ? "graphql-variables-line-numbers" : "graphql-query-line-numbers");
  const lines = Math.max(1, textarea.value.split("\n").length);
  lineNumbers.textContent = Array.from({ length: lines }, (_, index) => index + 1).join("\n");
  lineNumbers.scrollTop = textarea.scrollTop;
}

function switchScriptTab(type) {
  const activeType = type === "post" ? "post" : "pre";
  document.querySelectorAll(".script-subtab").forEach((button) => {
    button.classList.toggle("active", button.dataset.scriptTab === activeType);
  });
  $("pre-request-script-panel").classList.toggle("hidden", activeType !== "pre");
  $("post-response-script-panel").classList.toggle("hidden", activeType !== "post");
}

function switchTab(tab) {
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === tab));
  document.querySelectorAll(".tab-body").forEach((el) => el.classList.add("hidden"));
  $(`tab-${tab}`).classList.remove("hidden");
}

function quickTooltip() {
  let tooltip = $("quick-tooltip");
  if (!tooltip) {
    tooltip = document.createElement("div");
    tooltip.id = "quick-tooltip";
    tooltip.className = "quick-tooltip hidden";
    document.body.appendChild(tooltip);
  }
  return tooltip;
}

function showQuickTooltip(text, event) {
  const tooltip = quickTooltip();
  tooltip.textContent = text || "";
  tooltip.classList.toggle("hidden", !text);
  positionQuickTooltip(event);
}

function positionQuickTooltip(event) {
  const tooltip = $("quick-tooltip");
  if (!tooltip || tooltip.classList.contains("hidden") || typeof event.clientX !== "number") return;
  const gap = 12;
  let left = event.clientX + gap;
  let top = event.clientY + gap;
  const rect = tooltip.getBoundingClientRect();
  if (left + rect.width > window.innerWidth - 8) {
    left = window.innerWidth - rect.width - 8;
  }
  if (top + rect.height > window.innerHeight - 8) {
    top = event.clientY - rect.height - gap;
  }
  tooltip.style.left = `${Math.max(8, left)}px`;
  tooltip.style.top = `${Math.max(8, top)}px`;
}

function hideQuickTooltip() {
  $("quick-tooltip")?.classList.add("hidden");
}

function setupRequestTabDragScroll() {
  const rail = $("request-tabs");
  if (!rail) return;
  let pointerId = null;
  let startX = 0;
  let startScrollLeft = 0;
  let dragged = false;
  let blockNextClick = false;
  let clearClickBlockTimer = null;
  let pendingClickTabKey = null;

  const releasePointer = (releasedPointerId) => {
    if (releasedPointerId === null) return;
    try {
      rail.releasePointerCapture?.(releasedPointerId);
    } catch (_) {
      // Pointer capture may already be released by the browser.
    }
  };

  const resetDrag = (keepClickBlock = false) => {
    const releasedPointerId = pointerId;
    pointerId = null;
    dragged = false;
    pendingClickTabKey = null;
    rail.classList.remove("dragging");
    releasePointer(releasedPointerId);
    window.clearTimeout(clearClickBlockTimer);
    clearClickBlockTimer = window.setTimeout(() => {
      blockNextClick = false;
    }, keepClickBlock ? 120 : 0);
  };

  rail.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest(".request-tab i")) return;
    if (rail.scrollWidth <= rail.clientWidth) return;
    resetDrag(false);
    hideQuickTooltip();
    pointerId = event.pointerId;
    startX = event.clientX;
    startScrollLeft = rail.scrollLeft;
    dragged = false;
    pendingClickTabKey = event.target.closest(".request-tab")?.dataset.tabKey || null;
  });

  rail.addEventListener("pointermove", (event) => {
    if (pointerId !== event.pointerId) return;
    if ((event.buttons & 1) === 0) {
      resetDrag(false);
      return;
    }
    const delta = event.clientX - startX;
    if (Math.abs(delta) > 4) {
      dragged = true;
      blockNextClick = true;
      pendingClickTabKey = null;
      rail.classList.add("dragging");
      try {
        rail.setPointerCapture?.(pointerId);
      } catch (_) {
        // Some browsers can reject capture during fast pointer transitions.
      }
    }
    if (dragged) {
      rail.scrollLeft = startScrollLeft - delta;
      event.preventDefault();
    }
  });

  const finishDrag = (event) => {
    if (pointerId !== event.pointerId) return;
    const tabKey = !dragged ? pendingClickTabKey : null;
    if (tabKey) {
      blockNextClick = true;
    }
    resetDrag(blockNextClick);
    if (tabKey) {
      activateRequestTab(tabKey);
    }
  };

  rail.addEventListener("pointerup", finishDrag);
  rail.addEventListener("pointercancel", finishDrag);
  rail.addEventListener("lostpointercapture", () => resetDrag(blockNextClick));
  rail.addEventListener("dragstart", (event) => event.preventDefault());
  window.addEventListener("mouseup", () => resetDrag(blockNextClick));
  window.addEventListener("blur", () => resetDrag(false));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) resetDrag(false);
  });
  rail.addEventListener("click", (event) => {
    if (!blockNextClick) return;
    event.preventDefault();
    event.stopPropagation();
    blockNextClick = false;
  }, true);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

function safeFilename(value) {
  const normalized = String(value || "response").trim().replace(/[\\/:*?"<>|]+/g, "-");
  return normalized || "response";
}

function clampNumber(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function savedNumber(key, fallback) {
  const value = Number(localStorage.getItem(key));
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function setSidebarWidth(width) {
  const app = $("app");
  if (!app) return;
  const maxWidth = Math.max(300, Math.min(680, window.innerWidth - 560));
  const nextWidth = clampNumber(Math.round(width), 300, maxWidth);
  app.style.setProperty("--sidebar-width", `${nextWidth}px`);
  localStorage.setItem(SIDEBAR_WIDTH_KEY, String(nextWidth));
}

function setResponseHeight(height) {
  const pane = document.querySelector(".editor-pane");
  if (!pane) return;
  const maxHeight = Math.max(180, Math.min(760, Math.round(pane.getBoundingClientRect().height * 0.72)));
  const nextHeight = clampNumber(Math.round(height), 170, maxHeight);
  pane.style.setProperty("--response-height", `${nextHeight}px`);
  localStorage.setItem(RESPONSE_HEIGHT_KEY, String(nextHeight));
}

function setupResizableLayout() {
  const app = $("app");
  const sidebarHandle = $("sidebar-resizer");
  const responseHandle = $("response-resizer");
  const pane = document.querySelector(".editor-pane");
  if (!app || !sidebarHandle || !responseHandle || !pane) return;

  setSidebarWidth(savedNumber(SIDEBAR_WIDTH_KEY, 420));
  setResponseHeight(savedNumber(RESPONSE_HEIGHT_KEY, 360));

  let activeDrag = null;

  const stopDrag = () => {
    if (!activeDrag) return;
    activeDrag.handle.classList.remove("dragging");
    document.body.classList.remove("resizing-sidebar", "resizing-response");
    try {
      activeDrag.handle.releasePointerCapture?.(activeDrag.pointerId);
    } catch (_) {
      // Pointer capture can already be released by the browser.
    }
    activeDrag = null;
  };

  const startDrag = (event, type) => {
    if (event.button !== 0) return;
    const handle = type === "sidebar" ? sidebarHandle : responseHandle;
    const currentSidebarWidth = parseFloat(getComputedStyle(app).getPropertyValue("--sidebar-width")) || 420;
    const currentResponseHeight = parseFloat(getComputedStyle(pane).getPropertyValue("--response-height")) || 360;
    activeDrag = {
      type,
      handle,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startSidebarWidth: currentSidebarWidth,
      startResponseHeight: currentResponseHeight,
    };
    handle.classList.add("dragging");
    document.body.classList.add(type === "sidebar" ? "resizing-sidebar" : "resizing-response");
    handle.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  };

  const moveDrag = (event) => {
    if (!activeDrag || activeDrag.pointerId !== event.pointerId) return;
    if (activeDrag.type === "sidebar") {
      setSidebarWidth(activeDrag.startSidebarWidth + event.clientX - activeDrag.startX);
    } else {
      setResponseHeight(activeDrag.startResponseHeight + activeDrag.startY - event.clientY);
    }
    event.preventDefault();
  };

  sidebarHandle.addEventListener("pointerdown", (event) => startDrag(event, "sidebar"));
  responseHandle.addEventListener("pointerdown", (event) => startDrag(event, "response"));
  window.addEventListener("pointermove", moveDrag);
  window.addEventListener("pointerup", stopDrag);
  window.addEventListener("pointercancel", stopDrag);
  window.addEventListener("blur", stopDrag);
  window.addEventListener("resize", () => {
    setSidebarWidth(savedNumber(SIDEBAR_WIDTH_KEY, 420));
    setResponseHeight(savedNumber(RESPONSE_HEIGHT_KEY, 360));
  });
}

function wireEvents() {
  $("auth-form").onsubmit = async (event) => {
    event.preventDefault();
    const setupMode = state.setupRequired && !state.registrationMode;
    const username = $("auth-username").value.trim();
    const password = $("auth-password").value;
    const confirm = $("auth-confirm-password").value;
    if (!username) {
      $("auth-error").textContent = "Enter a username.";
      return;
    }
    if ((setupMode || state.registrationMode) && password !== confirm) {
      $("auth-error").textContent = "Passwords do not match.";
      return;
    }
    try {
      const authPath = setupMode
        ? "/api/setup"
        : state.registrationMode
        ? "/api/register"
        : "/api/login";
      const data = await api(authPath, {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      state.user = data.user;
      state.setupRequired = false;
      state.registrationMode = false;
      state.registrationUsername = "";
      $("auth-password").value = "";
      $("auth-confirm-password").value = "";
      showApp();
      await loadAppData();
    } catch (error) {
      if (!setupMode && !state.registrationMode && error.data?.registration_required) {
        state.registrationMode = true;
        state.registrationUsername = error.data.username || username;
        $("auth-password").value = "";
        $("auth-confirm-password").value = "";
        showAuth("User not found. Create a password to set up a new workspace.");
        return;
      }
      showAuth(error.message);
    }
  };
  $("logout-btn").onclick = async () => {
    await flushWorkspaceSave().catch(() => {});
    await api("/api/logout", { method: "POST" }).catch(() => {});
    state.user = null;
    state.workspaceReady = false;
    resetUserWorkspaceState({ clearCollections: true, clearEnv: true });
    state.onboardingSeen = false;
    state.onboardingActive = false;
    state.onboardingStep = 0;
    state.onboardingManual = false;
    state.registrationMode = false;
    state.registrationUsername = "";
    $("onboarding-overlay").classList.add("hidden");
    showAuth();
  };
  $("help-btn").onclick = () => startOnboarding(true);
  $("onboarding-next").onclick = nextOnboardingStep;
  $("onboarding-close").onclick = () => finishOnboarding(true);
  $("account-settings-btn").onclick = (event) => {
    event.stopPropagation();
    openAdminUsersModal().catch((error) => alert(error.message));
  };
  $("admin-users-close").onclick = closeAdminUsersModal;
  $("admin-users-modal").onclick = (event) => {
    if (event.target === $("admin-users-modal")) closeAdminUsersModal();
  };
  $("admin-password-form").onsubmit = async (event) => {
    event.preventDefault();
    const userId = $("admin-user-select").value;
    const password = $("admin-new-password").value;
    const confirm = $("admin-confirm-password").value;
    if (!userId) {
      setAdminUsersMessage("Select a user.", true);
      return;
    }
    if (password.length < 6) {
      setAdminUsersMessage("The password must be at least 6 characters long.", true);
      return;
    }
    if (password !== confirm) {
      setAdminUsersMessage("Passwords do not match.", true);
      return;
    }
    try {
      await api(`/api/admin/users/${userId}/password`, {
        method: "PUT",
        body: JSON.stringify({ password }),
      });
      $("admin-new-password").value = "";
      $("admin-confirm-password").value = "";
      setAdminUsersMessage("Password updated.");
      await loadAdminUsers();
    } catch (error) {
      setAdminUsersMessage(error.message, true);
    }
  };
  $("new-collection-btn").onclick = createCollection;
  $("new-tab-set-btn").onclick = () => createEmptyTabSet().catch((error) => alert(error.message));
  $("send-btn").onclick = () => {
    animateActionButton($("send-btn"));
    sendRequest().catch((error) => alert(error.message));
  };
  $("copy-response-btn").onclick = () => copyResponseText().catch((error) => alert(error.message));
  $("copy-body-btn").onclick = () => copyRequestBodyText().catch((error) => alert(error.message));
  $("download-curl-btn").onclick = () => {
    try {
      downloadCurlScript();
    } catch (error) {
      alert(error.message);
    }
  };
  $("download-response-btn").onclick = downloadResponseText;
  $("request-context-menu").onclick = async (event) => {
    event.stopPropagation();
    const button = event.target.closest("button");
    if (!button || (!state.contextRequest && !state.contextCollection)) return;
    if (button.dataset.action === "add-in-set") return;
    const action = button.dataset.action;
    const request = state.contextRequest;
    const collection = state.contextCollection;
    hideContextMenus();
    try {
      if (request && button.dataset.tabSetId) {
        await addRequestToTabSet(requestForAction(request), button.dataset.tabSetId);
      }
      if (request && action === "edit-name") await editRequestName(request);
      if (request && action === "duplicate") await duplicateRequest(request);
      if (request && action === "mark-delete") {
        await submitDeleteRequest("request", request);
      }
      if (request && action === "delete") {
        await deleteRequestPermanently(request);
      }
      if (collection && action === "edit-name") await editCollectionName(collection);
      if (collection && action === "duplicate") await duplicateCollection(collection);
      if (collection && action === "mark-delete") {
        await submitDeleteRequest("collection", collection);
      }
      if (collection && action === "delete") {
        await deleteCollectionPermanently(collection);
      }
    } catch (error) {
      alert(error.message);
    }
  };
  $("tab-context-menu").onclick = async (event) => {
    event.stopPropagation();
    const button = event.target.closest("button");
    if (!button || !state.contextTabKey) return;
    if (button.dataset.action === "add-in-set") return;
    const action = button.dataset.action;
    const key = state.contextTabKey;
    const tab = state.openTabs.find((item) => item.key === key);
    hideContextMenus();
    try {
      if (button.dataset.tabSetId) {
        if (state.activeTabKey === key) {
          syncActiveTabFromEditor();
        }
        await addRequestToTabSet(tab?.request, button.dataset.tabSetId);
        return;
      }
      if (action === "edit") await editOpenTabName(key);
      if (action === "mark-delete" && tab?.request) {
        await submitDeleteRequest("request", tab.request);
      }
      if (action === "delete" && tab?.request) {
        await deleteRequestPermanently(tab.request);
      }
      if (action === "close") closeRequestTab(key);
      if (action === "duplicate") duplicateOpenTab(key);
      if (action === "close-all") closeAllRequestTabs();
    } catch (error) {
      alert(error.message);
    }
  };
  $("tab-set-context-menu").onclick = async (event) => {
    event.stopPropagation();
    const button = event.target.closest("button");
    if (!button || !state.contextTabSet) return;
    const action = button.dataset.action;
    const tabSet = state.contextTabSet;
    hideContextMenus();
    try {
      if (action === "open") await addTabSetToWorkspace(tabSet);
      if (action === "rename") await renameTabSet(tabSet);
      if (action === "delete") await deleteTabSet(tabSet);
    } catch (error) {
      alert(error.message);
    }
  };
  $("tab-set-request-context-menu").onclick = async (event) => {
    event.stopPropagation();
    const button = event.target.closest("button");
    if (!button || !state.contextTabSet || !state.contextTabSetRequest) return;
    const tabSet = state.contextTabSet;
    const request = state.contextTabSetRequest;
    hideContextMenus();
    try {
      if (button.dataset.action === "remove-from-set") {
        await removeRequestFromTabSet(tabSet, request.id);
      }
    } catch (error) {
      alert(error.message);
    }
  };
  $("add-param-btn").onclick = () => {
    addPairRow("params-rows");
    markRequestChanged();
  };
  $("add-body-form-row-btn").onclick = () => {
    addPairRow("body-form-rows");
    markRequestChanged();
  };
  $("request-url").oninput = updateParamsFromUrl;
  $("request-url").onkeydown = (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    sendRequest().catch((error) => alert(error.message));
  };
  $("request-method").onchange = () => {
    updateMethodSelect();
    markRequestChanged();
    renderRequestTabs();
  };
  $("request-auth-type").onchange = () => {
    updateAuthEditor();
    markRequestChanged();
  };
  ["request-auth-token", "request-basic-username", "request-basic-password"].forEach((id) => {
    $(id).oninput = markRequestChanged;
  });
  $("body-editor").oninput = () => {
    updateBodyLineNumbers();
    markRequestChanged();
  };
  $("body-editor").onscroll = syncBodyLineScroll;
  $("body-format").onchange = () => {
    updateBodyEditor();
    markRequestChanged();
  };
  document.querySelectorAll('input[name="body-mode"]').forEach((radio) => {
    radio.onchange = () => {
      switchBodyMode();
      markRequestChanged();
    };
  });
  $("choose-binary-file-btn").onclick = () => $("binary-file-input").click();
  $("remove-binary-file-btn").onclick = () => {
    state.bodyBinary = {};
    $("binary-file-input").value = "";
    renderBinaryFile();
    markRequestChanged();
  };
  $("binary-file-input").onchange = async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > MAX_FORM_FILE_BYTES) {
      event.target.value = "";
      alert(`The selected file is larger than ${formatFileSize(MAX_FORM_FILE_BYTES)}.`);
      return;
    }
    const button = $("choose-binary-file-btn");
    button.disabled = true;
    button.textContent = "Reading...";
    try {
      state.bodyBinary = {
        file_name: file.name,
        file_type: file.type || "application/octet-stream",
        file_size: file.size,
        file_base64: await fileToBase64(file),
      };
      renderBinaryFile();
      markRequestChanged();
    } catch (error) {
      alert(error.message);
    } finally {
      button.disabled = false;
      renderBinaryFile();
      event.target.value = "";
    }
  };
  ["graphql-query", "graphql-variables"].forEach((id) => {
    $(id).oninput = () => {
      updateGraphqlLineNumbers(id === "graphql-variables" ? "variables" : "query");
      captureActiveBodyMode();
      markRequestChanged();
    };
    $(id).onscroll = () => updateGraphqlLineNumbers(id === "graphql-variables" ? "variables" : "query");
  });
  [
    ["pre-request-script", "pre"],
    ["post-response-script", "post"],
  ].forEach(([id, type]) => {
    $(id).oninput = () => {
      updateScriptLineNumbers(type);
      markRequestChanged();
    };
    $(id).onscroll = () => updateScriptLineNumbers(type);
  });
  document.querySelectorAll(".script-subtab").forEach((button) => {
    button.onclick = () => switchScriptTab(button.dataset.scriptTab);
  });
  $("export-btn").onclick = () => exportCollections().catch((error) => alert(error.message));
  $("import-file").onchange = (event) => {
    const file = event.target.files[0];
    if (file) importCollections(file).catch((error) => alert(error.message));
  };
  $("skip-tls-verification").onchange = () => {
    updateTlsWarning();
    markRequestChanged();
  };
  $("delete-requests-btn").onclick = () => {
    renderDeleteRequests();
    $("delete-requests-modal").classList.remove("hidden");
    $("delete-requests-close").focus();
  };
  $("delete-requests-close").onclick = () => $("delete-requests-modal").classList.add("hidden");
  $("delete-requests-modal").onclick = (event) => {
    if (event.target === $("delete-requests-modal")) {
      $("delete-requests-modal").classList.add("hidden");
    }
  };
  $("delete-requests-list").onclick = async (event) => {
    const approve = event.target.closest("[data-delete-approve]");
    const dismiss = event.target.closest("[data-delete-dismiss]");
    if (!approve && !dismiss) return;
    try {
      if (approve) {
        if (!confirm("Permanently delete this item? This action cannot be undone.")) return;
        await api(`/api/delete-requests/${approve.dataset.deleteApprove}/approve`, { method: "POST" });
        await refreshSharedCatalog(await api("/api/catalog-state"));
      } else {
        if (!confirm("Dismiss this deletion request?")) return;
        await api(`/api/delete-requests/${dismiss.dataset.deleteDismiss}`, { method: "DELETE" });
      }
      await loadDeleteRequests();
    } catch (error) {
      alert(error.message);
    }
  };
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.onclick = () => switchTab(tab.dataset.tab);
  });
  setupRequestTabDragScroll();
  setupResizableLayout();

  document.addEventListener("click", (event) => {
    if (event.target.closest(".context-menu")) return;
    hideContextMenus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      hideContextMenus();
      closeAdminUsersModal();
      $("delete-requests-modal").classList.add("hidden");
      finishOnboarding(true);
    }
  });
  window.addEventListener("resize", () => {
    hideContextMenus();
    positionOnboarding();
  });
  document.addEventListener("scroll", positionOnboarding, true);
}

wireEvents();
boot().catch((error) => showAuth(error.message));
