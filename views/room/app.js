/* 虚拟活动 · 房间视图
 * 数据全部走 window.AstrBotPluginView bridge：
 *   GET  room         -> { ok, room, status }
 *   POST room/save    -> { ok, room }
 *   POST trigger      -> ActivityResult
 *   POST tick         -> ActivityResult
 *   GET  state        -> { ok, status }
 */

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const els = {
  roomName: document.getElementById("room-name"),
  roomSub: document.getElementById("room-sub"),
  chipNow: document.getElementById("chip-now"),
  chipNext: document.getElementById("chip-next"),
  chipBody: document.getElementById("chip-body"),
  stageWrap: document.getElementById("stage-wrap"),
  stage: document.getElementById("stage"),
  panelTitle: document.getElementById("panel-title"),
  panelBody: document.getElementById("panel-body"),
  feedList: document.getElementById("feed-list"),
  feedHint: document.getElementById("feed-hint"),
  btnTick: document.getElementById("btn-tick"),
  btnSave: document.getElementById("btn-save"),
  btnAdd: document.getElementById("btn-add"),
  modeInteract: document.getElementById("mode-interact"),
  modeEdit: document.getElementById("mode-edit"),
  toast: document.getElementById("toast"),
};

const state = {
  room: null,
  status: null,
  selectedId: null,
  mode: "interact",
  scale: 1,
  suppressClick: false,
  busyEndsAt: 0,
  nextTickAt: 0,
  logs: [],
  books: { list: [], active: null },
  saveTimer: null,
  dirty: false,
  saving: false,
  saveQueued: false,
  toastTimer: null,
};

/* ------------------------------------------------------------ 工具 */

function fmtDuration(seconds) {
  const total = Math.max(0, Math.round(seconds));
  if (total >= 3600) {
    const hours = Math.floor(total / 3600);
    const minutes = Math.round((total % 3600) / 60);
    return `${hours} 小时 ${minutes} 分`;
  }
  if (total >= 60) return `${Math.round(total / 60)} 分钟`;
  return `${total} 秒`;
}

function fmtNumber(value, fallback = "—") {
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(number)}` : fallback;
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function toast(message) {
  if (!els.toast) return;
  els.toast.textContent = message;
  els.toast.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    els.toast.hidden = true;
  }, 2600);
}

async function request(method, endpoint, body) {
  if (!bridge) {
    toast("请从 AstrBot WebUI 的插件页面打开");
    return null;
  }
  try {
    if (method === "GET") return await bridge.apiGet(endpoint, body || {});
    return await bridge.apiPost(endpoint, body || {});
  } catch (error) {
    toast(error && error.message ? error.message : "请求失败");
    return null;
  }
}

function findFurniture(id) {
  if (!state.room || !id) return null;
  return state.room.furniture.find((item) => item.id === id) || null;
}

/* ------------------------------------------------------------ 启动 */

async function boot() {
  bindStaticEvents();
  if (bridge && typeof bridge.ready === "function") {
    try {
      await bridge.ready();
    } catch (error) {
      /* 忽略，继续尝试加载 */
    }
  }
  await loadRoom();
  await loadBooks();
  setInterval(refreshStatus, 4000);
  setInterval(updateChips, 1000);
  window.addEventListener("resize", fitStage);
  // 切走标签页时先把未落盘的改动存掉
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") saveRoom(false);
  });
}

async function loadRoom() {
  const data = await request("GET", "room");
  if (!data) return;
  state.room = data.room;
  if (data.status) applyStatus(data.status);
  if (!state.selectedId && state.room && state.room.furniture.length) {
    state.selectedId = state.room.furniture[0].id;
  }
  seedLogs();
  renderHeader();
  renderStage();
  renderPanel();
  renderFeed();
}

async function loadBooks() {
  const data = await request("GET", "books");
  if (!data) return;
  state.books = { list: data.books || [], active: data.active || null };
  renderPanel();
}

function seedLogs() {
  const recent = (state.status && state.status.state && state.status.state.recent_behaviors) || [];
  state.logs = recent
    .slice(-8)
    .reverse()
    .map((item) => ({
      text: `${item.furniture_name || item.furniture_id} · ${item.effect_name || item.effect_id}`,
      at: item.at ? new Date(item.at) : new Date(),
    }));
  const lastText = state.status && state.status.state && state.status.state.last_text;
  if (lastText) state.logs.unshift({ text: lastText, at: new Date() });
}

/* ------------------------------------------------------------ 状态 */

function applyStatus(status) {
  state.status = status;
  const now = Date.now();
  const busySec = Number(status.busy_remaining_sec || 0);
  state.busyEndsAt = busySec > 0 ? now + busySec * 1000 : 0;
  const nextSec = status.next_tick_in_sec;
  state.nextTickAt = nextSec === null || nextSec === undefined ? 0 : now + Number(nextSec) * 1000;
  updateChips();
  updateMarker();
}

function updateChips() {
  const status = state.status;
  if (!status) return;
  const behavior = status.state && status.state.behavior;
  const remaining = (state.busyEndsAt - Date.now()) / 1000;
  if (behavior && remaining > 0) {
    els.chipNow.textContent = `在「${behavior.furniture_name || behavior.furniture_id}」${behavior.name} · 剩 ${fmtDuration(remaining)}`;
    els.chipNow.classList.add("is-busy");
  } else {
    els.chipNow.textContent = "闲着";
    els.chipNow.classList.remove("is-busy");
  }

  if (!status.auto_tick) {
    els.chipNext.textContent = "自动活动：已关闭";
  } else if (state.nextTickAt > Date.now()) {
    els.chipNext.textContent = `下次活动 ${fmtDuration((state.nextTickAt - Date.now()) / 1000)}后`;
  } else {
    els.chipNext.textContent = "下次活动：待排";
  }

  const body = (status.state && status.state) || {};
  els.chipBody.textContent = `精力 ${fmtNumber(body.energy)} · 社交 ${fmtNumber(body.social_hunger)}`;
}

async function refreshStatus() {
  const data = await request("GET", "state");
  if (!data || !data.status) return;
  applyStatus(data.status);
}

/* ------------------------------------------------------------ 顶栏 */

function renderHeader() {
  if (!state.room) return;
  const room = state.room.room || {};
  const canvas = room.canvas || { width: 1000, height: 700 };
  els.roomName.textContent = room.name || "我的房间";
  els.roomSub.textContent = `画布 ${canvas.width}×${canvas.height} · ${state.room.furniture.length} 件家具`;
}

/* ------------------------------------------------------------ 画布 */

function renderStage() {
  if (!state.room) return;
  const canvas = (state.room.room && state.room.room.canvas) || { width: 1000, height: 700 };
  els.stage.style.width = `${canvas.width}px`;
  els.stage.style.height = `${canvas.height}px`;
  els.stage.style.background = canvas.background || "#f3ece2";
  els.stage.innerHTML = "";

  const ordered = [...state.room.furniture].sort((a, b) => (a.z || 0) - (b.z || 0));
  for (const furniture of ordered) {
    els.stage.appendChild(buildFurnitureNode(furniture));
  }
  fitStage();
  updateMarker();
}

/* ------------------------------------------------------------ 机器人位置 */

let markerEl = null;

function markerAvatar() {
  const robots = (state.room && state.room.room && state.room.room.robots) || [];
  return (robots[0] && String(robots[0].avatar || "").trim()) || "🐋";
}

function isImageSource(value) {
  const text = String(value || "").trim();
  return /^(https?:|data:|blob:|\/)/i.test(text) || /\.(png|jpe?g|gif|webp|svg|avif)(\?|$)/i.test(text);
}

function ensureMarker() {
  if (!state.room) return null;
  markerEl = els.stage.querySelector(".robot-marker");
  if (!markerEl) {
    markerEl = document.createElement("div");
    markerEl.className = "robot-marker";
    els.stage.appendChild(markerEl);
  }
  const avatar = markerAvatar();
  markerEl.innerHTML = "";
  if (isImageSource(avatar)) {
    const img = document.createElement("img");
    img.src = avatar;
    img.alt = "robot";
    markerEl.appendChild(img);
  } else {
    const span = document.createElement("span");
    span.textContent = avatar;
    markerEl.appendChild(span);
  }
  return markerEl;
}

function updateMarker() {
  if (!state.room) return;
  const marker = ensureMarker();
  if (!marker) return;

  const status = state.status || {};
  const current = status.state || {};
  const behavior = current.behavior || null;
  const location = current.location || null;
  const position = current.position || location || null;
  const canvas = (state.room.room && state.room.room.canvas) || { width: 1000, height: 700 };
  const furnitureId = (location && location.furniture_id) || (behavior && behavior.furniture_id) || null;
  const target = furnitureId ? findFurniture(furnitureId) : null;

  let anchorX;
  let anchorY;
  let below = false;
  if (target) {
    // 挂在家具正上方，用三角指向家具，不遮住图标和名字
    anchorX = target.x + target.w / 2;
    anchorY = target.y - 4;
    if (anchorY < 60) {
      anchorY = target.y + target.h + 4;
      below = true;
    }
  } else if (position) {
    anchorX = Number(position.x) || canvas.width / 2;
    anchorY = (Number(position.y) || canvas.height / 2) - 30;
    if (anchorY < 60) {
      anchorY += 60;
      below = true;
    }
  } else {
    anchorX = canvas.width / 2;
    anchorY = canvas.height / 2 - 30;
  }

  marker.style.left = `${anchorX}px`;
  marker.style.top = `${anchorY}px`;
  marker.classList.toggle("is-below", below);
  marker.title = behavior
    ? `在「${behavior.furniture_name || behavior.furniture_id}」${behavior.name || ""}`
    : "在屋里待着";

  for (const node of els.stage.querySelectorAll(".furniture")) {
    node.classList.toggle("is-current", Boolean(furnitureId) && node.dataset.id === furnitureId);
  }
}

function buildFurnitureNode(furniture) {
  const node = document.createElement("div");
  node.className = "furniture";
  node.dataset.id = furniture.id;
  applyFurnitureStyle(node, furniture);
  if (furniture.id === state.selectedId) node.classList.add("is-selected");
  if (!furniture.effects || furniture.effects.length === 0) node.classList.add("is-prop");
  if (furniture.enabled === false) node.classList.add("is-disabled");

  const emoji = document.createElement("span");
  emoji.className = "emoji";
  emoji.textContent = furniture.emoji || "";
  const label = document.createElement("span");
  label.className = "label";
  label.textContent = furniture.name || furniture.id;
  node.append(emoji, label);

  if (furniture.effects && furniture.effects.length) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = String(furniture.effects.length);
    node.appendChild(badge);
  }

  node.addEventListener("pointerdown", (event) => onPointerDown(event, furniture, node));
  node.addEventListener("click", () => onFurnitureClick(furniture));
  return node;
}

function applyFurnitureStyle(node, furniture) {
  node.style.left = `${furniture.x}px`;
  node.style.top = `${furniture.y}px`;
  node.style.width = `${furniture.w}px`;
  node.style.height = `${furniture.h}px`;
  node.style.background = furniture.color || "#c8a27a";
  node.style.zIndex = String(furniture.z || 1);
}

function fitStage() {
  if (!state.room) return;
  const canvas = (state.room.room && state.room.room.canvas) || { width: 1000, height: 700 };
  const rect = els.stageWrap.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const scale = Math.min((rect.width - 32) / canvas.width, (rect.height - 32) / canvas.height, 1.1);
  state.scale = Math.max(0.2, scale);
  els.stage.style.transform = `scale(${state.scale})`;
}

/* ------------------------------------------------------------ 交互 */

let drag = null;

function onPointerDown(event, furniture, node) {
  if (event.button !== 0) return;
  event.preventDefault();
  drag = {
    id: furniture.id,
    node,
    startX: event.clientX,
    startY: event.clientY,
    originX: furniture.x,
    originY: furniture.y,
    moved: false,
  };
  node.classList.add("is-dragging");
  node.setPointerCapture?.(event.pointerId);
  node.addEventListener("pointermove", onPointerMove);
  node.addEventListener("pointerup", onPointerUp);
  node.addEventListener("pointercancel", onPointerUp);
}

function onPointerMove(event) {
  if (!drag) return;
  const target = findFurniture(drag.id);
  if (!target) return;
  const canvas = state.room.room.canvas;
  const dx = (event.clientX - drag.startX) / state.scale;
  const dy = (event.clientY - drag.startY) / state.scale;
  if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
  target.x = clamp(Math.round(drag.originX + dx), 0, canvas.width - target.w);
  target.y = clamp(Math.round(drag.originY + dy), 0, canvas.height - target.h);
  drag.node.style.left = `${target.x}px`;
  drag.node.style.top = `${target.y}px`;
  syncPanelNumbers(target);
}

function onPointerUp(event) {
  if (!drag) return;
  const node = drag.node;
  node.classList.remove("is-dragging");
  node.releasePointerCapture?.(event.pointerId);
  node.removeEventListener("pointermove", onPointerMove);
  node.removeEventListener("pointerup", onPointerUp);
  node.removeEventListener("pointercancel", onPointerUp);
  const moved = drag.moved;
  drag = null;
  if (moved) {
    state.suppressClick = true;
    setTimeout(() => {
      state.suppressClick = false;
    }, 60);
    scheduleSave();
  }
}

function onFurnitureClick(furniture) {
  if (state.suppressClick) return;
  selectFurniture(furniture.id);
  if (state.mode === "edit") return;
  triggerFurniture(furniture.id);
}

function selectFurniture(id) {
  state.selectedId = id;
  for (const node of els.stage.querySelectorAll(".furniture")) {
    node.classList.toggle("is-selected", node.dataset.id === id);
  }
  renderPanel();
}

async function triggerFurniture(id, effectId) {
  const furniture = findFurniture(id);
  if (!furniture) return;
  if (!furniture.effects || furniture.effects.length === 0) {
    toast(`「${furniture.name}」只是个摆件`);
    return;
  }
  const data = await request("POST", "trigger", { furniture_id: id, effect_id: effectId || null });
  if (!data) return;
  if (data.ok) {
    pushLog(data.text);
    if (data.state) applyStatus({ ...(state.status || {}), state: data.state, busy_remaining_sec: remainSeconds(data.state) });
    toast(data.text);
  } else {
    toast(data.message || "没动静");
  }
  await refreshStatus();
}

function remainSeconds(stateData) {
  if (!stateData || !stateData.ends_at) return 0;
  const ends = new Date(stateData.ends_at).getTime();
  if (!Number.isFinite(ends)) return 0;
  return Math.max(0, Math.round((ends - Date.now()) / 1000));
}

async function forceTick() {
  const data = await request("POST", "tick", {});
  if (!data) return;
  if (data.ok) {
    pushLog(data.text);
    toast(data.text);
  } else {
    toast(data.message || "没触发");
  }
  await refreshStatus();
}

/* ------------------------------------------------------------ 面板 */

function renderPanel() {
  els.panelBody.innerHTML = "";
  if (!state.room) {
    els.panelTitle.textContent = "加载中";
    return;
  }
  els.panelBody.appendChild(buildRobotCard());
  els.panelBody.appendChild(buildBooksCard());
  const furniture = findFurniture(state.selectedId);
  if (!furniture) {
    els.panelTitle.textContent = "房间";
    renderFurnitureList();
    return;
  }
  els.panelTitle.textContent = furniture.name || furniture.id;
  els.panelBody.appendChild(buildFurnitureForm(furniture));
  els.panelBody.appendChild(buildEffectSection(furniture));
  els.panelBody.appendChild(buildFurnitureActions(furniture));
}

function robotOf() {
  if (!state.room) return null;
  const room = state.room.room || (state.room.room = {});
  if (!Array.isArray(room.robots)) room.robots = [];
  if (!room.robots.length) {
    room.robots.push({ id: "default", name: "小鲸鱼", avatar: "" });
  }
  return room.robots[0];
}

function buildBooksCard() {
  const box = document.createElement("div");
  box.className = "robot-card";

  const head = document.createElement("div");
  head.className = "effect-head";
  const title = document.createElement("span");
  title.textContent = "书架";
  head.appendChild(title);
  box.appendChild(head);

  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = ".txt,.md,text/plain";
  fileInput.hidden = true;
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) {
      try {
        const data = await bridge.upload("books/upload", file);
        state.books = { list: data.books || [], active: data.active || null };
        renderPanel();
        toast(`已导入《${(data.book && data.book.title) || file.name}》`);
      } catch (error) {
        toast((error && error.message) || "导入失败");
      }
    }
    fileInput.value = "";
  });

  const upload = document.createElement("button");
  upload.type = "button";
  upload.className = "btn btn-small";
  upload.textContent = "导入小说";
  upload.addEventListener("click", () => fileInput.click());

  const row = document.createElement("div");
  row.className = "avatar-row";
  row.append(upload, fileInput);
  box.appendChild(row);

  const books = (state.books && state.books.list) || [];
  if (!books.length) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "还没有书。导入一个 .txt 小说，读书活动就会从这里取内容。";
    box.appendChild(hint);
    return box;
  }

  const active = state.books.active;
  for (const book of books) {
    const item = document.createElement("div");
    item.className = "list-item";
    if (book.id === active) item.classList.add("is-active");

    const left = document.createElement("div");
    left.style.minWidth = "0";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = book.title;
    const sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent = `${book.progress}% · ${Math.round(book.chars / 1000)}k 字${book.finished ? " · 已读完" : ""}`;
    left.append(name, sub);
    item.appendChild(left);

    const actions = document.createElement("div");
    actions.className = "row";

    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "btn btn-small";
    pick.textContent = book.id === active ? "在读" : "选这本";
    pick.disabled = book.id === active;
    pick.addEventListener("click", async () => {
      const data = await request("POST", "books/select", { id: book.id });
      if (data) {
        state.books = { list: data.books || [], active: data.active || null };
        renderPanel();
        toast(`现在读《${book.title}》`);
      }
    });
    actions.appendChild(pick);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn-ghost";
    del.textContent = "删除";
    del.addEventListener("click", async () => {
      if (!window.confirm(`删除《${book.title}》？进度也会一起没了。`)) return;
      const data = await request("POST", "books/delete", { id: book.id });
      if (data) {
        state.books = { list: data.books || [], active: data.active || null };
        renderPanel();
      }
    });
    actions.appendChild(del);

    item.appendChild(actions);
    box.appendChild(item);
  }

  return box;
}

function buildRobotCard() {
  const robot = robotOf();
  const box = document.createElement("div");
  box.className = "robot-card";

  const head = document.createElement("div");
  head.className = "effect-head";
  const title = document.createElement("span");
  title.textContent = "机器人（图标 = 画布上的位置标记）";
  head.appendChild(title);
  box.appendChild(head);

  addField(box, "名字", textInput(robot.name || "", (value) => {
    robot.name = value;
    scheduleSave();
  }));

  const iconRow = document.createElement("div");
  iconRow.className = "avatar-row";

  const preview = document.createElement("div");
  preview.className = "avatar-preview";
  renderAvatarPreview(preview, robot.avatar);

  const applyAvatar = (data) => {
    robot.avatar = data;
    renderAvatarPreview(preview, data);
    if (emojiInput) emojiInput.value = "";
    updateMarker();
    scheduleSave();
  };

  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = "image/*";
  fileInput.hidden = true;
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) {
      try {
        const { img, url } = await loadImageFromFile(file);
        try {
          const data = await openCropDialog(img, url, robot);
          if (data) {
            applyAvatar(data);
            toast(`图标已更新（${robot.avatar_size || 128}px）`);
          }
        } finally {
          URL.revokeObjectURL(url);
        }
      } catch (error) {
        toast("这张图片读不出来，换一张试试");
      }
    }
    fileInput.value = "";
  });

  const pickButton = document.createElement("button");
  pickButton.type = "button";
  pickButton.className = "btn btn-small";
  pickButton.textContent = "选择图片";
  pickButton.addEventListener("click", () => fileInput.click());

  const cropButton = document.createElement("button");
  cropButton.type = "button";
  cropButton.className = "btn btn-small";
  cropButton.textContent = "重新取景";
  cropButton.addEventListener("click", async () => {
    if (!isDataUri(robot.avatar)) {
      toast("先选一张图片再调整取景");
      return;
    }
    try {
      const img = await loadImageFromUrl(robot.avatar);
      const data = await openCropDialog(img, robot.avatar, robot);
      if (data) applyAvatar(data);
    } catch (error) {
      toast("没法重新取景，重新选一次图片吧");
    }
  });

  iconRow.append(preview, pickButton, cropButton, fileInput);

  const label = document.createElement("span");
  label.className = "hint";
  label.textContent = "图标";
  box.appendChild(label);
  box.appendChild(iconRow);

  const optionRow = document.createElement("div");
  optionRow.className = "grid-2";

  const sizeSelect = document.createElement("select");
  [
    ["64", "64 px"],
    ["128", "128 px（默认）"],
    ["256", "256 px"],
    ["512", "512 px"],
  ].forEach(([value, text]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    sizeSelect.appendChild(option);
  });
  sizeSelect.value = String(robot.avatar_size || 128);
  sizeSelect.addEventListener("change", async () => {
    robot.avatar_size = Number(sizeSelect.value);
    scheduleSave();
    if (isDataUri(robot.avatar)) {
      try {
        const data = await reencodeAvatar(robot.avatar, avatarOptions(robot));
        applyAvatar(data);
      } catch (error) {
        toast("重新缩放失败，重新选一次图片吧");
      }
    }
  });
  addField(optionRow, "输出尺寸", sizeSelect);
  box.appendChild(optionRow);

  const emojiInput = textInput(isImageSource(robot.avatar) ? "" : robot.avatar || "", (value) => {
    robot.avatar = value.trim();
    renderAvatarPreview(preview, robot.avatar);
    updateMarker();
    scheduleSave();
  });
  emojiInput.placeholder = "也可以直接填表情 🐋 或图片地址";
  addField(box, "或手填", emojiInput);

  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = "选图时可以拖动、缩放调整取景；按上面的尺寸处理后存进 room.json，留空则用插件配置里的 robot_avatar";
  box.appendChild(hint);
  return box;
}

function renderAvatarPreview(el, avatar) {
  el.innerHTML = "";
  const value = String(avatar || "").trim() || "🐋";
  if (isImageSource(value)) {
    const img = document.createElement("img");
    img.src = value;
    img.alt = "avatar";
    el.appendChild(img);
  } else {
    el.textContent = value;
  }
}

function loadImageElement(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = (error) => {
      URL.revokeObjectURL(url);
      reject(error);
    };
    img.src = url;
  });
}

function avatarOptions(robot) {
  return {
    size: Number(robot.avatar_size) || 128,
    fit: robot.avatar_fit === "contain" ? "contain" : "cover",
  };
}

function isDataUri(value) {
  return /^data:image\//i.test(String(value || ""));
}

function drawAvatar(img, options = {}) {
  const size = Math.max(32, Math.min(512, Number(options.size) || 128));
  const fit = options.fit === "contain" ? "contain" : "cover";
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  const scale =
    fit === "contain"
      ? Math.min(size / img.width, size / img.height)
      : Math.max(size / img.width, size / img.height);
  const width = Math.max(1, Math.round(img.width * scale));
  const height = Math.max(1, Math.round(img.height * scale));
  ctx.drawImage(img, Math.round((size - width) / 2), Math.round((size - height) / 2), width, height);
  let data = canvas.toDataURL("image/webp", 0.9);
  if (!data.startsWith("data:image/webp")) data = canvas.toDataURL("image/png");
  if (data.length > 600000) data = canvas.toDataURL("image/jpeg", 0.75);
  return data;
}

async function fileToAvatar(file, options) {
  if (!file.type || !file.type.startsWith("image/")) {
    throw new Error("not an image");
  }
  const img = await loadImageElement(file);
  return drawAvatar(img, options);
}

function loadImageFromUrl(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = url;
  });
}

async function reencodeAvatar(dataUri, options) {
  const img = await loadImageFromUrl(dataUri);
  return drawAvatar(img, options);
}

function loadImageFromFile(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => resolve({ img, url });
    img.onerror = (error) => {
      URL.revokeObjectURL(url);
      reject(error);
    };
    img.src = url;
  });
}

function dialogButton(text, extraClass) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = `btn${extraClass ? " " + extraClass : ""}`;
  el.textContent = text;
  return el;
}

/** 取景对话框：拖动平移、滚轮/滑杆缩放，确定后按当前框选范围导出。 */
function openCropDialog(img, displayUrl, robot) {
  return new Promise((resolve) => {
    const stageSize = 240;
    const overlay = document.createElement("div");
    overlay.className = "crop-modal";

    const panel = document.createElement("div");
    panel.className = "crop-panel";

    const title = document.createElement("div");
    title.className = "crop-title";
    title.textContent = "调整取景范围（拖动移动，滑杆缩放）";

    const stage = document.createElement("div");
    stage.className = "crop-stage";
    const imgEl = document.createElement("img");
    imgEl.src = displayUrl;
    imgEl.alt = "crop";
    imgEl.draggable = false;
    stage.appendChild(imgEl);

    const zoom = document.createElement("input");
    zoom.type = "range";
    zoom.className = "crop-zoom";

    const actions = document.createElement("div");
    actions.className = "crop-actions";
    const btnFill = dialogButton("填满");
    const btnWhole = dialogButton("整张");
    const btnCancel = dialogButton("取消");
    const btnOk = dialogButton("确定", "btn-primary");
    actions.append(btnFill, btnWhole, btnCancel, btnOk);

    panel.append(title, stage, zoom, actions);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);

    const minScale = Math.min(stageSize / img.width, stageSize / img.height);
    const coverScale = Math.max(stageSize / img.width, stageSize / img.height);
    const state = { scale: coverScale, x: 0, y: 0 };

    function layout() {
      const width = img.width * state.scale;
      const height = img.height * state.scale;
      if (width <= stageSize) state.x = (stageSize - width) / 2;
      else state.x = Math.min(0, Math.max(stageSize - width, state.x));
      if (height <= stageSize) state.y = (stageSize - height) / 2;
      else state.y = Math.min(0, Math.max(stageSize - height, state.y));
      imgEl.style.width = `${width}px`;
      imgEl.style.height = `${height}px`;
      imgEl.style.left = `${state.x}px`;
      imgEl.style.top = `${state.y}px`;
    }

    function reset(scale) {
      state.scale = scale;
      state.x = (stageSize - img.width * scale) / 2;
      state.y = (stageSize - img.height * scale) / 2;
      zoom.value = String(scale);
      layout();
    }

    zoom.min = String(minScale);
    zoom.max = String(coverScale * 3);
    zoom.step = String((coverScale * 3 - minScale) / 200);
    reset(coverScale);

    zoom.addEventListener("input", () => {
      const center = stageSize / 2;
      const imgX = (center - state.x) / state.scale;
      const imgY = (center - state.y) / state.scale;
      state.scale = Number(zoom.value) || coverScale;
      state.x = center - imgX * state.scale;
      state.y = center - imgY * state.scale;
      layout();
    });

    stage.addEventListener("wheel", (event) => {
      event.preventDefault();
      const factor = event.deltaY > 0 ? 1.08 : 0.92;
      const next = Math.max(minScale, Math.min(coverScale * 3, state.scale * factor));
      zoom.value = String(next);
      zoom.dispatchEvent(new Event("input"));
    });

    let drag = null;
    stage.addEventListener("pointerdown", (event) => {
      drag = { x: event.clientX, y: event.clientY, ox: state.x, oy: state.y };
      stage.setPointerCapture(event.pointerId);
      stage.classList.add("is-dragging");
    });
    stage.addEventListener("pointermove", (event) => {
      if (!drag) return;
      state.x = drag.ox + (event.clientX - drag.x);
      state.y = drag.oy + (event.clientY - drag.y);
      layout();
    });
    const endDrag = () => {
      drag = null;
      stage.classList.remove("is-dragging");
    };
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);

    btnFill.addEventListener("click", () => reset(coverScale));
    btnWhole.addEventListener("click", () => reset(minScale));
    btnCancel.addEventListener("click", () => {
      overlay.remove();
      resolve(null);
    });

    btnOk.addEventListener("click", () => {
      const size = Math.max(32, Math.min(512, Number(robot.avatar_size) || 128));
      const canvas = document.createElement("canvas");
      canvas.width = size;
      canvas.height = size;
      const ctx = canvas.getContext("2d");
      const k = size / stageSize;
      ctx.setTransform(k, 0, 0, k, 0, 0);
      ctx.drawImage(img, state.x, state.y, img.width * state.scale, img.height * state.scale);
      let data = canvas.toDataURL("image/webp", 0.9);
      if (!data.startsWith("data:image/webp")) data = canvas.toDataURL("image/png");
      if (data.length > 600000) data = canvas.toDataURL("image/jpeg", 0.75);
      overlay.remove();
      resolve(data);
    });
  });
}

function renderFurnitureList() {
  const hint = document.createElement("p");
  hint.className = "hint";
  hint.textContent = "点画布里的家具查看和编辑；拖动可以换位置。";
  els.panelBody.appendChild(hint);

  for (const furniture of state.room.furniture) {
    const item = document.createElement("div");
    item.className = "list-item";
    if (furniture.id === state.selectedId) item.classList.add("is-active");

    const left = document.createElement("div");
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = `${furniture.emoji || ""} ${furniture.name || furniture.id}`.trim();
    const sub = document.createElement("div");
    sub.className = "sub";
    const effects = furniture.effects || [];
    sub.textContent = effects.length ? effects.map((effect) => effect.name).join(" / ") : "摆件";
    left.append(name, sub);

    item.append(left);
    item.addEventListener("click", () => selectFurniture(furniture.id));
    els.panelBody.appendChild(item);
  }
}

function addField(parent, label, input) {
  const wrapper = document.createElement("label");
  wrapper.className = "field";
  const span = document.createElement("span");
  span.textContent = label;
  wrapper.append(span, input);
  parent.appendChild(wrapper);
  return input;
}

function numberInput(value, onChange, min, max) {
  const input = document.createElement("input");
  input.type = "number";
  input.value = String(value ?? 0);
  if (min !== undefined) input.min = String(min);
  if (max !== undefined) input.max = String(max);
  input.addEventListener("input", () => {
    const parsed = Number(input.value);
    if (!Number.isFinite(parsed)) return;
    onChange(clamp(Math.round(parsed), min ?? -99999, max ?? 99999));
  });
  input.addEventListener("change", () => {
    input.value = String(clamp(Math.round(Number(input.value) || 0), min ?? -99999, max ?? 99999));
  });
  return input;
}

function buildFurnitureForm(furniture) {
  // 面板可能比模型活得久，写入时按 id 找当前对象，避免写进被替换掉的旧副本
  const live = () => findFurniture(furniture.id) || furniture;

  const box = document.createElement("div");
  box.style.display = "flex";
  box.style.flexDirection = "column";
  box.style.gap = "10px";

  const idHint = document.createElement("div");
  idHint.className = "hint";
  idHint.textContent = `id：${furniture.id}`;
  box.appendChild(idHint);

  addField(box, "名字", textInput(furniture.name, (value) => {
    live().name = value;
    syncStage();
    scheduleSave();
  }));

  const grid = document.createElement("div");
  grid.className = "grid-2";

  const canvas = state.room.room.canvas;
  const set = (key, value) => {
    live()[key] = value;
    syncStage();
    scheduleSave();
  };

  addField(grid, "x", numberInput(furniture.x, (v) => set("x", clamp(v, 0, canvas.width - furniture.w)), 0, canvas.width));
  addField(grid, "y", numberInput(furniture.y, (v) => set("y", clamp(v, 0, canvas.height - furniture.h)), 0, canvas.height));
  addField(grid, "宽", numberInput(furniture.w, (v) => set("w", clamp(v, 24, canvas.width)), 24, canvas.width));
  addField(grid, "高", numberInput(furniture.h, (v) => set("h", clamp(v, 24, canvas.height)), 24, canvas.height));
  box.appendChild(grid);

  const row = document.createElement("div");
  row.className = "grid-2";

  const color = document.createElement("input");
  color.type = "color";
  color.value = furniture.color || "#c8a27a";
  color.addEventListener("input", () => {
    live().color = color.value;
    syncStage();
    scheduleSave();
  });
  addField(row, "颜色", color);

  addField(row, "图标", textInput(furniture.emoji || "", (value) => {
    // 按码点截断，避免把 emoji 的代理对切一半；一个图标最多两个码点（如 🛏️）
    live().emoji = Array.from(value).slice(0, 2).join("");
    syncStage();
    scheduleSave();
  }, 8));
  box.appendChild(row);

  const toggle = document.createElement("label");
  toggle.className = "row";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = furniture.enabled !== false;
  checkbox.addEventListener("change", () => {
    live().enabled = checkbox.checked;
    syncStage();
    scheduleSave();
  });
  const toggleText = document.createElement("span");
  toggleText.className = "hint";
  toggleText.textContent = "启用（关掉后不参与随机）";
  toggle.append(checkbox, toggleText);
  box.appendChild(toggle);

  return box;
}

function textInput(value, onChange, maxLength) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value ?? "";
  if (maxLength) input.maxLength = maxLength;
  input.addEventListener("input", () => onChange(input.value));
  return input;
}

function buildEffectSection(furniture) {
  const box = document.createElement("div");
  box.style.display = "flex";
  box.style.flexDirection = "column";
  box.style.gap = "8px";

  const head = document.createElement("div");
  head.className = "effect-head";
  const title = document.createElement("span");
  title.textContent = `可做的事（${(furniture.effects || []).length}）`;
  const add = document.createElement("button");
  add.className = "btn btn-small";
  add.type = "button";
  add.textContent = "＋ 互动";
  add.addEventListener("click", () => addEffect(furniture));
  head.append(title, add);
  box.appendChild(head);

  const effects = furniture.effects || [];
  if (!effects.length) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "没有互动 = 纯摆件，点击不会有反应。";
    box.appendChild(hint);
    return box;
  }

  effects.forEach((effect, index) => box.appendChild(buildEffectCard(furniture, effect, index)));
  return box;
}

function buildEffectCard(furniture, effect, index) {
  const card = document.createElement("div");
  card.className = "effect-card";

  const head = document.createElement("div");
  head.className = "effect-head";
  const label = document.createElement("span");
  label.textContent = `#${index + 1} ${effect.id || ""}`;
  const remove = document.createElement("button");
  remove.className = "btn btn-ghost";
  remove.type = "button";
  remove.textContent = "删除";
  remove.addEventListener("click", () => {
    furniture.effects = furniture.effects.filter((item) => item !== effect);
    syncStage();
    renderPanel();
    scheduleSave();
  });
  head.append(label, remove);
  card.appendChild(head);

  addField(card, "名称", textInput(effect.name || "", (value) => {
    effect.name = value;
    scheduleSave();
  }));

  const actionSelect = document.createElement("select");
  [
    ["", "普通互动"],
    ["tv", "看电视（B 站视频）"],
    ["read_book", "读书（小说书架）"],
  ].forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    actionSelect.appendChild(option);
  });
  actionSelect.value = effect.action || "";
  actionSelect.addEventListener("change", () => {
    effect.action = actionSelect.value;
    scheduleSave();
  });
  addField(card, "类型", actionSelect);

  const grid = document.createElement("div");
  grid.className = "grid-2";
  addField(grid, "时长（秒）", numberInput(effect.duration_sec, (v) => {
    effect.duration_sec = clamp(v, 30, 21600);
    scheduleSave();
  }, 30, 21600));
  addField(grid, "权重", numberInput(effect.weight, (v) => {
    effect.weight = Math.max(0.1, v);
    scheduleSave();
  }, 1, 100));
  card.appendChild(grid);

  const body = document.createElement("div");
  body.className = "grid-2";
  addField(body, "精力变化", numberInput(effect.energy_delta ?? 0, (v) => {
    effect.energy_delta = clamp(v, -60, 60);
    scheduleSave();
  }, -60, 60));
  addField(body, "社交变化", numberInput(effect.social_delta ?? 0, (v) => {
    effect.social_delta = clamp(v, -60, 60);
    scheduleSave();
  }, -60, 60));
  card.appendChild(body);

  const bodyHint = document.createElement("p");
  bodyHint.className = "hint";
  bodyHint.textContent = "精力变化填正数是休息类，填负数是消耗类；精力低时会优先选正数的。";
  card.appendChild(bodyHint);

  const desc = document.createElement("textarea");
  desc.value = effect.desc || "";
  desc.rows = 2;
  desc.addEventListener("input", () => {
    effect.desc = desc.value;
    scheduleSave();
  });
  addField(card, "描述", desc);

  const hint = document.createElement("textarea");
  hint.value = effect.prompt_hint || "";
  hint.rows = 2;
  hint.addEventListener("input", () => {
    effect.prompt_hint = hint.value;
    scheduleSave();
  });
  addField(card, "给模型的提示", hint);

  return card;
}

function addEffect(furniture) {
  if (!Array.isArray(furniture.effects)) furniture.effects = [];
  const base = `${furniture.id}_e${furniture.effects.length + 1}`;
  furniture.effects.push({
    id: base,
    name: "新互动",
    desc: "",
    prompt_hint: "",
    duration_sec: 600,
    weight: 1,
    energy_delta: -6,
    social_delta: 0,
  });
  syncStage();
  renderPanel();
  scheduleSave();
}

function buildFurnitureActions(furniture) {
  const box = document.createElement("div");
  box.className = "row";
  box.style.flexWrap = "wrap";

  const go = document.createElement("button");
  go.className = "btn btn-primary";
  go.type = "button";
  go.textContent = "让它去这里";
  go.addEventListener("click", () => triggerFurniture(furniture.id));
  box.appendChild(go);

  const del = document.createElement("button");
  del.className = "btn";
  del.type = "button";
  del.textContent = "删除家具";
  del.addEventListener("click", () => {
    if (!window.confirm(`删除「${furniture.name}」？`)) return;
    state.room.furniture = state.room.furniture.filter((item) => item.id !== furniture.id);
    state.selectedId = state.room.furniture.length ? state.room.furniture[0].id : null;
    renderStage();
    renderPanel();
    scheduleSave();
  });
  box.appendChild(del);
  return box;
}

function syncStage() {
  if (!state.room) return;
  renderHeader();
  for (const furniture of state.room.furniture) {
    const node = els.stage.querySelector(`[data-id="${CSS.escape(furniture.id)}"]`);
    if (!node) continue;
    applyFurnitureStyle(node, furniture);
    node.classList.toggle("is-prop", !(furniture.effects && furniture.effects.length));
    node.classList.toggle("is-disabled", furniture.enabled === false);
    const emoji = node.querySelector(".emoji");
    if (emoji) emoji.textContent = furniture.emoji || "";
    const label = node.querySelector(".label");
    if (label) label.textContent = furniture.name || furniture.id;
    let badge = node.querySelector(".badge");
    const count = (furniture.effects || []).length;
    if (count > 0) {
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "badge";
        node.appendChild(badge);
      }
      badge.textContent = String(count);
    } else if (badge) {
      badge.remove();
    }
  }
}

function syncPanelNumbers(furniture) {
  if (!furniture || furniture.id !== state.selectedId) return;
  const inputs = els.panelBody.querySelectorAll('input[type="number"]');
  if (inputs.length >= 2) {
    inputs[0].value = String(furniture.x);
    inputs[1].value = String(furniture.y);
  }
}

/* ------------------------------------------------------------ 保存 */

function scheduleSave() {
  state.dirty = true;
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => saveRoom(false), 900);
}

async function saveRoom(showToast) {
  if (!state.room) return;
  clearTimeout(state.saveTimer);
  if (!state.dirty && showToast !== true) return;
  if (state.saving) {
    state.saveQueued = true;
    return;
  }
  state.saving = true;
  try {
    // 传副本给后端校验，但本地保留同一个对象，避免输入框绑到过期引用
    const payload = JSON.parse(JSON.stringify(state.room));
    const data = await request("POST", "room/save", { room: payload });
    if (data && data.ok) {
      state.dirty = false;
      if (showToast === true) toast("布局已保存");
    }
  } finally {
    state.saving = false;
    if (state.saveQueued) {
      state.saveQueued = false;
      scheduleSave();
    }
  }
}

/* ------------------------------------------------------------ 记录 */

function pushLog(text) {
  if (!text) return;
  state.logs.unshift({ text, at: new Date() });
  state.logs = state.logs.slice(0, 12);
  renderFeed();
}

function renderFeed() {
  els.feedList.innerHTML = "";
  if (!state.logs.length) {
    const item = document.createElement("li");
    item.className = "hint";
    item.textContent = "还没有活动记录。点一件家具试试。";
    els.feedList.appendChild(item);
    return;
  }
  for (const log of state.logs) {
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.textContent = `${String(log.at.getHours()).padStart(2, "0")}:${String(log.at.getMinutes()).padStart(2, "0")}`;
    item.append(time, document.createTextNode(log.text));
    els.feedList.appendChild(item);
  }
}

/* ------------------------------------------------------------ 事件 */

function bindStaticEvents() {
  els.btnTick.addEventListener("click", forceTick);
  els.btnSave.addEventListener("click", () => saveRoom(true));
  els.btnAdd.addEventListener("click", addFurniture);
  els.modeInteract.addEventListener("click", () => setMode("interact"));
  els.modeEdit.addEventListener("click", () => setMode("edit"));
  els.feedHint.textContent = "";
}

function setMode(mode) {
  state.mode = mode;
  els.modeInteract.classList.toggle("is-active", mode === "interact");
  els.modeEdit.classList.toggle("is-active", mode === "edit");
  els.feedHint.textContent = mode === "edit" ? "编辑模式：拖动家具换位置" : "互动模式：点家具让它去做，拖动可换位置";
}

function addFurniture() {
  if (!state.room) return;
  const canvas = state.room.room.canvas;
  const index = state.room.furniture.length + 1;
  const item = {
    id: `f${index}`,
    name: "新家具",
    shape: "rect",
    x: Math.round(canvas.width / 2 - 70),
    y: Math.round(canvas.height / 2 - 50),
    w: 140,
    h: 100,
    color: "#c8a27a",
    emoji: "📦",
    z: 3,
    enabled: true,
    effects: [],
  };
  state.room.furniture.push(item);
  state.selectedId = item.id;
  renderStage();
  renderPanel();
  scheduleSave();
}

boot();
