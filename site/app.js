const MANIFEST = "manifest.json";
const ARCHIVE = "__archive__";
const FAVOURITES = "__favourites__";

const el = (id) => document.getElementById(id);
const timeline = el("timeline");
const status = el("status");

let allItems = [];
let archivedItems = [];
let shown = [];
let activeFilter = "all";
// Which parent's children are on show. null means the top level.
let openParent = null;
const selected = new Set();
// Per user, so they come from the API rather than the shared manifest.
let favourites = new Set();

// S3 keys carry spaces and Hebrew; each segment is encoded separately so the
// slashes survive. Assigning a raw key to src is what broke the old gallery.
const url = (key) => key.split("/").map(encodeURIComponent).join("/");

const bytes = (n) => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
};

const clock = (seconds) => {
  if (!seconds) return "";
  const s = Math.round(seconds);
  const pad = (v) => String(v).padStart(2, "0");
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  return h ? `${h}:${pad(m % 60)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
};

const when = (item) => item.taken_at || item.modified_at || null;

const monthOf = (item) => {
  const raw = when(item);
  if (!raw) return "Undated";
  const d = new Date(raw);
  return Number.isNaN(d.valueOf())
    ? "Undated"
    : d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
};

const GLYPH = { audio: "♪", video: "▶", image: "▦", other: "◆" };

function tile(item, index) {
  const isArchived = activeFilter === ARCHIVE;
  const button = document.createElement("button");
  button.className = "tile" + (isArchived ? " tile-archived" : "");
  button.type = "button";
  button.dataset.key = item.key;

  if (item.thumb) {
    const img = document.createElement("img");
    img.src = url(item.thumb);
    img.alt = item.name;
    img.loading = "lazy";
    img.decoding = "async";
    button.append(img);
  } else {
    const fallback = document.createElement("div");
    fallback.className = "tile-fallback";
    fallback.innerHTML = `<div class="glyph">${GLYPH[item.kind] || GLYPH.other}</div>`;
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = item.title || item.name;
    fallback.append(label);
    button.append(fallback);
  }

  const heart = document.createElement("span");
  heart.className = "fav" + (favourites.has(item.key) ? " on" : "");
  heart.setAttribute("role", "button");
  heart.tabIndex = 0;
  heart.textContent = "♥";
  heart.setAttribute("aria-label", `Favourite ${item.name}`);
  const toggleFav = (e) => {
    e.stopPropagation();
    e.preventDefault();
    setFavourite(item.key, !favourites.has(item.key), heart);
  };
  heart.addEventListener("click", toggleFav);
  heart.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") toggleFav(e);
  });
  button.append(heart);

  if (item.kind !== "image") {
    const badge = document.createElement("span");
    badge.className = "badge" + (item.playable === false ? " badge-warn" : "");
    badge.textContent = item.playable === false
      ? "no preview"
      : clock(item.duration) || GLYPH[item.kind];
    button.append(badge);
  }

  // Checkbox lives on the tile. It is only visible on hover, once something is
  // selected, or on touch devices where there is no hover to reveal it.
  if (item.playable !== undefined) {
    const pick = document.createElement("span");
    pick.className = "pick";
    pick.setAttribute("role", "checkbox");
    pick.setAttribute("aria-checked", "false");
    pick.setAttribute("aria-label", `Select ${item.name}`);
    pick.tabIndex = 0;
    pick.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' +
      '<path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" ' +
      'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    const toggle = (e) => { e.stopPropagation(); e.preventDefault(); toggleSelect(button, item); };
    pick.addEventListener("click", toggle);
    pick.addEventListener("keydown", (e) => {
      if (e.key === " " || e.key === "Enter") toggle(e);
    });
    button.append(pick);
  }

  button.addEventListener("click", () => {
    // Once a selection exists, tapping a tile extends it rather than opening it.
    if (selected.size) return toggleSelect(button, item);
    openLightbox(index);
  });
  return button;
}

function toggleSelect(button, item) {
  const on = !selected.has(item.key);
  if (on) selected.add(item.key); else selected.delete(item.key);
  button.classList.toggle("picked", on);
  const pick = button.querySelector(".pick");
  if (pick) pick.setAttribute("aria-checked", String(on));
  refreshSelectionUI();
}

function refreshSelectionUI() {
  const any = selected.size > 0;
  document.body.classList.toggle("selecting", any);
  el("selbar").hidden = !any;
  el("sel-count").textContent = `${selected.size} selected`;
  el("do-archive").disabled = !any;
  el("do-fav").disabled = !any;
  el("do-move").disabled = !any || !el("cat-input").value.trim();
}

/* Categories nest with '/'. The bar shows one level at a time so it stays
   short however deep the tree goes. */

const parentOf = (path) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : null);
const leafOf = (path) => path.slice(path.lastIndexOf("/") + 1);

// Every item under this category, including its descendants.
const inBranch = (item, branch) => {
  const c = item.category || "Other";
  return c === branch || c.startsWith(branch + "/");
};

function childrenOf(parent) {
  const depth = parent ? parent.split("/").length : 0;
  const counts = new Map();
  for (const item of allItems) {
    const path = item.category || "Other";
    if (parent && !path.startsWith(parent + "/")) continue;
    const parts = path.split("/");
    if (parts.length <= depth) continue;
    const branch = parts.slice(0, depth + 1).join("/");
    counts.set(branch, (counts.get(branch) || 0) + 1);
  }
  return counts;
}

function chip(label, value, count, extra = "") {
  const button = document.createElement("button");
  button.className = `chip ${extra}` + (value === activeFilter ? " on" : "");
  button.type = "button";
  // Category names are user-typed, so never interpolate them into markup.
  button.append(document.createTextNode(label + " "));
  if (count !== null) {
    button.append(Object.assign(document.createElement("span"),
      { className: "chip-n", textContent: String(count) }));
  }
  button.addEventListener("click", () => {
    activeFilter = value;
    // Selecting a branch that has children drills into it.
    openParent = value !== "all" && value !== ARCHIVE && childrenOf(value).size ? value : parentOf(value);
    cancelSelect();
    renderChips();
    applyFilter();
  });
  return button;
}

function renameableCategory() {
  const special = activeFilter === "all" || activeFilter === ARCHIVE
    || activeFilter === FAVOURITES;
  return special ? null : activeFilter;
}

function renamePencil() {
  const button = document.createElement("button");
  button.className = "chip chip-edit";
  button.type = "button";
  button.setAttribute("aria-label", `Rename ${leafOf(activeFilter)}`);
  button.title = `Rename ${leafOf(activeFilter)}`;
  button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' +
    '<path d="M4 20h4L19 9l-4-4L4 16v4zM14 6l4 4" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  button.addEventListener("click", openRename);
  return button;
}

// The selected chip and its pencil share one pill, so the pencil clearly
// belongs to that category rather than floating beside it.
function appendChip(bar, node, value) {
  if (value !== activeFilter || !renameableCategory()) {
    bar.append(node);
    return;
  }
  const group = document.createElement("span");
  group.className = "chip-group";
  node.classList.add("in-group");
  const divider = document.createElement("span");
  divider.className = "chip-divider";
  const pencil = renamePencil();
  pencil.classList.add("in-group");
  group.append(node, divider, pencil);
  bar.append(group);
}

function renderChips() {
  const bar = el("filters");
  bar.replaceChildren();

  if (openParent) {
    const up = document.createElement("button");
    up.className = "chip chip-up";
    up.type = "button";
    up.setAttribute("aria-label", "Back");
    up.textContent = "‹ " + (parentOf(openParent) ? leafOf(parentOf(openParent)) : "All");
    up.addEventListener("click", () => {
      const grandparent = parentOf(openParent);
      activeFilter = grandparent || "all";
      openParent = grandparent;
      cancelSelect();
      renderChips();
      applyFilter();
    });
    bar.append(up);
    appendChip(bar, chip(`All of ${leafOf(openParent)}`,
      openParent, allItems.filter((i) => inBranch(i, openParent)).length), openParent);
  } else {
    bar.append(chip("All", "all", allItems.length));
  }

  for (const [branch, count] of [...childrenOf(openParent)].sort((a, b) => b[1] - a[1])) {
    const hasKids = childrenOf(branch).size > 0;
    appendChip(bar, chip(leafOf(branch) + (hasKids ? " ›" : ""), branch, count), branch);
  }

  // Shown even at zero, so the filter is discoverable before anything is
  // hearted — and so you can get back out of it once it empties.
  if (!openParent) {
    bar.append(chip("♥ Favourites", FAVOURITES, favourites.size, "chip-fav"));
  }
  if (!openParent && archivedItems.length) {
    bar.append(chip("Archive", ARCHIVE, archivedItems.length, "chip-archive"));
  }
  bar.hidden = false;
}

function applyFilter() {
  if (activeFilter === ARCHIVE) {
    shown = archivedItems;
  } else if (activeFilter === FAVOURITES) {
    shown = allItems.filter((i) => favourites.has(i.key));
  } else if (activeFilter === "all") {
    shown = allItems;
  } else {
    // A branch shows everything beneath it, not only its direct members.
    shown = allItems.filter((i) => inBranch(i, activeFilter));
  }

  timeline.replaceChildren();
  cancelSelect();
  el("count").textContent = `${shown.length} item${shown.length === 1 ? "" : "s"}`;
  // Archiving something already archived is meaningless, so hide the affordance.
  document.body.classList.toggle("no-select", activeFilter === ARCHIVE);
  renderTimeline(shown);
}

function renderTimeline(items) {
  if (!items.length) {
    status.hidden = false;
    status.textContent = activeFilter === FAVOURITES
      ? "No favourites yet — hover a photo and click the heart."
      : "Nothing here.";
    return;
  }
  status.hidden = true;

  const groups = new Map();
  for (const item of items) {
    const key = monthOf(item);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }

  let index = 0;
  for (const [month, group] of groups) {
    const heading = document.createElement("h2");
    heading.className = "month";
    heading.textContent = month;
    const grid = document.createElement("div");
    grid.className = "grid";
    for (const item of group) grid.append(tile(item, index++));
    timeline.append(heading, grid);
  }
}

/* ---------- select mode ---------- */

function cancelSelect() {
  selected.clear();
  for (const t of timeline.querySelectorAll(".picked")) {
    t.classList.remove("picked");
    const pick = t.querySelector(".pick");
    if (pick) pick.setAttribute("aria-checked", "false");
  }
  refreshSelectionUI();
}

function fillCategoryList() {
  const names = new Set(allItems.map((i) => i.category).filter(Boolean));
  const list = el("cat-list");
  list.replaceChildren();
  for (const name of [...names].sort()) {
    const option = document.createElement("option");
    option.value = name;
    list.append(option);
  }
}

// CloudFront signs origin requests to the Lambda URL with SigV4, and Lambda
// rejects unsigned payloads, so the body hash has to come from the client.
// Without this every write returns 403 with a signature mismatch.
async function bodyHash(body) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// A session predating a change to what the login issues is still valid to
// CloudFront but missing the identity cookie, so the API answers 401. Sending
// the viewer back through login is always the right response.
function reauthenticate() {
  window.location.href = "/auth/login?next=" + encodeURIComponent(window.location.pathname);
}

async function postJson(path, payload) {
  const body = JSON.stringify(payload);
  return fetch(path, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-amz-content-sha256": await bodyHash(body),
    },
    body,
  });
}

async function post(path, payload, button, busyLabel) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = busyLabel;
  try {
    const response = await postJson(path, payload);
    if (response.status === 401) { reauthenticate(); return false; }
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `failed (${response.status})`);
    return true;
  } catch (err) {
    status.hidden = false;
    status.textContent = `Could not ${busyLabel.toLowerCase().replace("…", "")}: ${err.message}`;
    return false;
  } finally {
    button.textContent = label;
    button.disabled = false;
  }
}

async function setFavourite(key, on, node) {
  node.classList.toggle("on", on);
  try {
    const response = await postJson("/api/favourite", { keys: [key], on });
    if (response.status === 401) return reauthenticate();
    if (!response.ok) throw new Error(`failed (${response.status})`);
    favourites = new Set((await response.json()).favourites || []);
    renderChips();
  } catch (err) {
    node.classList.toggle("on", !on);
    status.hidden = false;
    status.textContent = `Could not update favourite: ${err.message}`;
  }
}

async function favouriteSelected() {
  const keys = [...selected];
  if (await post("/api/favourite", { keys, on: true }, el("do-fav"), "Saving…")) {
    cancelSelect();
    await loadFavourites();
    renderChips();
    applyFilter();
  }
}

async function loadFavourites() {
  try {
    const response = await fetch("/api/favourites", { cache: "no-store" });
    if (response.status === 401) return reauthenticate();
    if (response.ok) favourites = new Set((await response.json()).favourites || []);
  } catch {
    favourites = new Set();
  }
}

async function moveSelected() {
  const category = el("cat-input").value.trim();
  if (!category) return;
  const keys = [...selected];
  if (await post("/api/category", { keys, category }, el("do-move"), "Moving…")) {
    el("cat-input").value = "";
    cancelSelect();
    await load();
  }
}

async function archiveSelected() {
  const keys = [...selected];
  if (await post("/api/archive", { keys }, el("do-archive"), "Archiving…")) {
    cancelSelect();
    await load();
  }
}

/* ---------- rename ---------- */

function openRename() {
  const category = renameableCategory();
  if (!category) return;
  const affected = allItems.filter((i) => inBranch(i, category)).length;
  const nested = childrenOf(category).size;
  el("rename-note").textContent =
    `${affected} item${affected === 1 ? "" : "s"}` +
    (nested ? `, including ${nested} sub-categor${nested === 1 ? "y" : "ies"} that move with it` : "");
  el("rename-input").value = category;
  el("rename-dialog").showModal();
}

async function submitRename(event) {
  if (event.submitter && event.submitter.value !== "ok") return;
  const from = renameableCategory();
  const to = el("rename-input").value.trim().replace(/^\/+|\/+$/g, "");
  if (!from || !to || to === from) return;
  if (await post("/api/rename-category", { from, to }, el("rename-go"), "Renaming…")) {
    // The old branch no longer exists, so go back to where it lived.
    openParent = parentOf(to);
    activeFilter = to;
    await load();
  }
}

el("rename-form").addEventListener("submit", submitRename);

/* ---------- lightbox ---------- */

const lightbox = el("lightbox");
const lbContent = el("lb-content");
let current = 0;

function openLightbox(index) {
  current = (index + shown.length) % shown.length;
  const item = shown[current];
  lbContent.replaceChildren();

  const caption = [item.category, item.name].filter(Boolean).join(" · ");

  if (item.playable === false) {
    // Filenames are attacker-influenced in principle, so build this with
    // textContent rather than interpolating into innerHTML.
    const notice = document.createElement("div");
    notice.className = "lb-notice";
    const title = document.createElement("p");
    title.append(Object.assign(document.createElement("strong"), { textContent: item.name }));
    const explain = document.createElement("p");
    explain.textContent = "This format can't play in a browser.";
    const link = document.createElement("a");
    link.className = "btn";
    link.href = url(item.key);
    link.setAttribute("download", "");
    link.textContent = "Download original";
    const wrap = document.createElement("p");
    wrap.append(link);
    notice.append(title, explain, wrap);
    lbContent.append(notice);
  } else {
    let node;
    if (item.kind === "image") {
      node = document.createElement("img");
      node.alt = item.name;
    } else {
      node = document.createElement(item.kind === "audio" ? "audio" : "video");
      node.controls = true;
      node.autoplay = true;
    }
    node.src = url(item.key);
    lbContent.append(node);
  }

  const stamp = when(item);
  const parts = [caption];
  if (stamp && !Number.isNaN(new Date(stamp).valueOf())) {
    parts.push(new Date(stamp).toLocaleString());
  }
  if (item.size) parts.push(bytes(item.size));
  if (item.archived_by) {
    const at = item.archived_at ? new Date(item.archived_at) : null;
    parts.push(`archived by ${item.archived_by}` +
      (at && !Number.isNaN(at.valueOf()) ? ` on ${at.toLocaleDateString()}` : ""));
  }
  el("lb-caption").textContent = parts.join(" · ");
  lightbox.hidden = false;
  document.body.style.overflow = "hidden";
}

function closeLightbox() {
  lightbox.hidden = true;
  lbContent.replaceChildren();
  document.body.style.overflow = "";
}

const step = (delta) => openLightbox(current + delta);

lightbox.querySelector(".lb-close").addEventListener("click", closeLightbox);
lightbox.querySelector(".lb-prev").addEventListener("click", () => step(-1));
lightbox.querySelector(".lb-next").addEventListener("click", () => step(1));
lightbox.addEventListener("click", (e) => { if (e.target === lightbox) closeLightbox(); });

document.addEventListener("keydown", (e) => {
  if (!lightbox.hidden) {
    if (e.key === "Escape") closeLightbox();
    if (e.key === "ArrowLeft") step(-1);
    if (e.key === "ArrowRight") step(1);
    return;
  }
  if (selected.size && e.key === "Escape") cancelSelect();
});

let touchX = null;
lightbox.addEventListener("touchstart", (e) => { touchX = e.changedTouches[0].clientX; }, { passive: true });
lightbox.addEventListener("touchend", (e) => {
  if (touchX === null) return;
  const dx = e.changedTouches[0].clientX - touchX;
  if (Math.abs(dx) > 60) step(dx > 0 ? -1 : 1);
  touchX = null;
}, { passive: true });

el("sel-cancel").addEventListener("click", cancelSelect);
el("do-archive").addEventListener("click", archiveSelected);
el("do-move").addEventListener("click", moveSelected);
el("do-fav").addEventListener("click", favouriteSelected);
el("cat-input").addEventListener("input", refreshSelectionUI);
el("cat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !el("do-move").disabled) moveSelected();
});

/* ---------- load ---------- */

async function load() {
  try {
    const response = await fetch(MANIFEST, { cache: "no-cache" });
    if (response.status === 403 || response.status === 401) return reauthenticate();
    if (!response.ok) throw new Error(`manifest returned ${response.status}`);
    const manifest = await response.json();

    await loadFavourites();
    allItems = (manifest.items || []).filter((i) => i.renderable !== false);
    archivedItems = manifest.archive || [];

    if (!allItems.length && !archivedItems.length) {
      status.hidden = false;
      status.textContent = "Nothing here yet.";
      el("filters").hidden = true;
      return;
    }
    fillCategoryList();
    renderChips();
    applyFilter();
  } catch (err) {
    status.hidden = false;
    status.textContent = `Could not load the gallery: ${err.message}`;
  }
}

load();
