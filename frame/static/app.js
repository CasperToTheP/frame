// Frame management UI. Plain JavaScript, no build step.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const readJSON = (el, key, fallback) => {
  try { return JSON.parse(el.dataset[key] || ""); } catch (_) { return fallback; }
};

const KINDS = readJSON(document.body, "kinds", {});      // filename -> kind
const SAVED = readJSON(document.body, "saved", []);      // saved playlist names
const LIVE = readJSON(document.body, "live", []);        // files in what's playing now
const FOLDERS = readJSON(document.body, "folders", []);  // [{path, name, parent, count, ...}]
let libFilter = "all";  // library: all / visual / audio
const selected = new Set();  // library: files ticked in select mode
const VISUAL = new Set(["video", "animation", "image"]);

// --- feedback --------------------------------------------------------------

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 8000 : 2600);
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    throw new Error("Frame not reachable. Is it on and connected to Wi-Fi?");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

// Reload the page but stay on the same tab and scroll position.
function reloadSoon(delay = 200) {
  try { sessionStorage.setItem("frame-scroll", JSON.stringify([location.hash, scrollY])); } catch (_) {}
  setTimeout(() => location.reload(), delay);
}

async function act(method, url, body, { reload = false, ok } = {}) {
  try {
    const data = await api(method, url, body);
    if (data.warning) toast(data.warning, true);
    else if (ok) toast(ok);
    if (reload) reloadSoon(data.warning ? 1800 : (ok ? 700 : 200));
    else refreshStatus();
    return data;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

// --- tabs ------------------------------------------------------------------

const VIEWS = ["now", "playlists", "library", "settings"];

function currentView() {
  let hash = "";
  try { hash = decodeURIComponent(location.hash.slice(1)); } catch (_) { /* bad URL */ }
  if (VIEWS.includes(hash)) return hash;
  // A folder in the Library: #library/<path>
  if (hash.startsWith("library/")) return "library";
  // A playlist editor: #edit/<name>
  if (hash.startsWith("edit/") && $$(".view").some((v) => v.dataset.view === hash)) return hash;
  return "now";
}

function showView() {
  const name = currentView();
  const tab = name.startsWith("edit/") ? "playlists" : name;
  for (const v of $$(".view")) v.hidden = v.dataset.view !== name;
  for (const a of $$(".tabbar a")) {
    const active = a.dataset.tab === tab;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  document.body.classList.toggle("on-now", name === "now");
  if (name === "library") renderLibrary();
}
window.addEventListener("hashchange", () => { showView(); scrollTo(0, 0); });
showView();
try {
  const saved = JSON.parse(sessionStorage.getItem("frame-scroll") || "null");
  sessionStorage.removeItem("frame-scroll");
  if (saved && saved[0] === location.hash) requestAnimationFrame(() => scrollTo(0, saved[1]));
} catch (_) {}

// --- thumbnails ----------------------------------------------------------------
// The frame makes thumbnails in the background. One that isn't ready yet answers
// 404: keep the placeholder icon showing and try again a few times.

function watchThumbs(root = document) {
  for (const img of $$("img.tn", root)) {
    if (img.dataset.watched) continue;
    img.dataset.watched = "1";
    if (!img.complete) img.classList.add("loading");
    else if (!img.naturalWidth) retryThumb(img);
    img.addEventListener("load", () => img.classList.remove("loading"));
    img.addEventListener("error", () => retryThumb(img));
  }
}

function retryThumb(img) {
  img.classList.add("loading");
  const tries = Number(img.dataset.tries || 0);
  if (tries >= 8) return; // give up: the icon stays
  img.dataset.tries = String(tries + 1);
  setTimeout(() => {
    const url = new URL(img.src, location.href);
    url.searchParams.set("try", String(tries + 1));
    img.src = url.toString();
  }, 3000 + tries * 2000);
}
watchThumbs();

// A folder's cover, copied from its Library tile.
function folderPreviewOf(path) {
  const tile = document.querySelector(`[data-folder-tile="${CSS.escape(path)}"] .tile-media`);
  if (!tile) return [];
  return Array.from(tile.children, (el) => {
    const copy = el.cloneNode(true);
    for (const img of $$("img", copy)) delete img.dataset.watched;
    return copy;
  });
}

// Copies of a file's preview elements, taken from its Library tile.
function previewOf(name) {
  const tile = name && document.querySelector(`[data-tile="${CSS.escape(name)}"] .tile-media`);
  if (!tile) return [];
  return Array.from(tile.querySelectorAll(":scope > img, :scope > .ph"), (el) => {
    const copy = el.cloneNode(true);
    delete copy.dataset.watched;
    return copy;
  });
}

function setPreview(container, name) {
  if (!container) return;
  container.replaceChildren(...previewOf(name));
  watchThumbs(container);
}

// --- now playing -------------------------------------------------------------

let nowState = readJSON(document.body, "now", {});
let playback = "unknown";
let clockOffset = 0; // server time minus phone time, in seconds
let heroName = nowState.visual?.current || null;

function fmtCountdown(nextAt) {
  if (!nextAt) return "";
  const secs = Math.max(0, Math.round(nextAt - (Date.now() / 1000 + clockOffset)));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = String(secs % 60).padStart(2, "0");
  return ` · next in ${h ? `${h}:${String(m).padStart(2, "0")}` : m}:${s}`;
}

function renderCountdowns() {
  const v = nowState.visual || {};
  const snd = nowState.sound || {};
  $("#position").textContent = v.count > 1 && v.position ? ` · ${v.position} of ${v.count}` : "";
  $("#countdown").textContent = fmtCountdown(v.next_at);
  $("#sound-countdown").textContent =
    (snd.count > 1 && snd.position ? ` · ${snd.position} of ${snd.count}` : "") + fmtCountdown(snd.next_at);
  const where = v.count > 1 && v.position ? `${v.position} of ${v.count}` : "";
  const label = { paused: "Paused", idle: "Black screen", "player offline": "Player offline" }[playback];
  $("#mini-sub").textContent = [label, where, fmtCountdown(v.next_at).replace(" · ", "")]
    .filter(Boolean).join(" · ") || (snd.current ? `♪ ${snd.current}` : "");
}

function setIcon(button, name) {
  const use = button.querySelector("use");
  if (use) use.setAttribute("href", `#i-${name}`);
}

function renderStatus(s) {
  nowState = s.now || {};
  playback = s.playback;
  if (s.server_time) clockOffset = s.server_time - Date.now() / 1000;
  const current = nowState.visual?.current || null;
  $("#current").textContent = current || "Nothing — black screen";
  $("#sound-now").textContent = nowState.sound?.current || "Artwork's own sound";
  $("#mini-title").textContent = current || "Black screen";
  if (current !== heroName) {
    heroName = current;
    setPreview($("#hero-media"), current);
    setPreview($("#mini-thumb"), current);
    for (const item of $$("[data-jump]")) {
      item.classList.toggle("current", item.dataset.jump === current);
      if (item.dataset.jump === current) item.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
    }
  }
  const err = $("#player-error");
  err.textContent = nowState.error || "";
  err.hidden = !nowState.error;
  $("#btn-next").disabled = $("#mini-next").disabled = !(nowState.visual?.count > 1);
  renderCountdowns();
  const labels = { playing: "Playing", paused: "Paused", idle: "Black screen",
                   "player offline": "Player offline", unknown: "…" };
  $("#playback").textContent = labels[s.playback] || s.playback;

  const health = $("#health");
  health.className = "status-pill " + (s.player.reachable ? "ok" : "bad");
  $(".label", health).textContent = s.player.reachable ? "Online" : "Player offline";

  const mute = $("#btn-mute");
  mute.setAttribute("aria-pressed", String(s.muted));
  mute.setAttribute("aria-label", s.muted ? "Unmute" : "Mute");
  setIcon(mute, s.muted ? "mute" : "vol");

  const paused = s.playback === "paused";
  for (const toggle of [$("#btn-toggle"), $("#mini-toggle")]) {
    setIcon(toggle, paused ? "play" : "pause");
    toggle.setAttribute("aria-label", paused ? "Play" : "Pause");
    toggle.disabled = !["playing", "paused"].includes(s.playback);
  }

  const vol = $("#volume");
  if (document.activeElement !== vol) {
    vol.value = s.volume;
    $("#volume-out").textContent = s.volume;
  }
  const hw = $("#hwdec-current");
  if (hw) hw.textContent = s.player.hwdec_current || "–";
}

async function refreshStatus() {
  try {
    renderStatus(await api("GET", "/api/status"));
  } catch (e) {
    const health = $("#health");
    health.className = "status-pill bad";
    $(".label", health).textContent = "Frame unreachable";
  }
}

for (const btn of $$("[data-action]")) {
  btn.addEventListener("click", () => {
    const action = btn.dataset.action;
    if (action === "stop") {
      if (!confirm("Turn the screen black? Your playlist is kept — tap Start to continue.")) return;
      act("POST", "/api/stop", {}, { reload: true });
    } else if (action === "start") {
      act("POST", "/api/start", {}, { reload: true });
    } else if (action === "mute") {
      act("POST", "/api/mute", {});
    } else if (action === "toggle") {
      act("POST", playback === "paused" ? "/api/resume" : "/api/pause", {});
    }
  });
}

// Up next: start scrolled to what's on the frame; tap a picture to show it now.
const stripCurrent = $("#strip .current");
if (stripCurrent) {
  const strip = $("#strip");
  strip.scrollLeft = stripCurrent.offsetLeft - (strip.clientWidth - stripCurrent.clientWidth) / 2;
}
for (const item of $$("[data-jump]")) {
  item.addEventListener("click", () => {
    if (item.classList.contains("current")) return;
    for (const other of $$("[data-jump]")) other.classList.toggle("current", other === item);
    act("POST", "/api/next", { which: "visual", filename: item.dataset.jump }, { ok: "Switching…" });
  });
}

for (const btn of $$("[data-next]")) {
  btn.addEventListener("click", () => {
    act("POST", "/api/next", { which: btn.dataset.next },
        { ok: btn.dataset.next === "sound" ? "Next track" : "Next artwork" });
  });
}

// Volume: send while dragging, but at most every 150 ms.
let volTimer = null;
$("#volume").addEventListener("input", (e) => {
  const v = Number(e.target.value);
  $("#volume-out").textContent = v;
  clearTimeout(volTimer);
  volTimer = setTimeout(() => act("POST", "/api/volume", { volume: v }), 150);
});

// --- playlists ------------------------------------------------------------------

const editHash = (name) => `#edit/${encodeURIComponent(name)}`;

function askName(question, current = "") {
  const name = (prompt(question, current) || "").trim();
  return name || null;
}

for (const btn of $$("[data-load]")) {
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    act("POST", "/api/saved/load", { name: btn.dataset.load },
        { reload: true, ok: `Playing “${btn.dataset.load}”` });
  });
}

for (const btn of $$("[data-new-playlist]")) {
  btn.addEventListener("click", async () => {
    const name = askName("Name the new playlist — for example “Sleeping” or “Morning”:");
    if (!name) return;
    const data = await act("POST", "/api/saved/new", { name }, { ok: `Created “${name}”` });
    if (data) { location.hash = editHash(data.name); reloadSoon(300); }
  });
}

for (const btn of $$("[data-save]")) {
  btn.addEventListener("click", () => {
    const name = askName("Save what's playing as a playlist called:");
    if (!name) return;
    if (SAVED.includes(name) && !confirm(`Replace the playlist “${name}” with what's playing now?`)) return;
    act("POST", "/api/saved", { name }, { reload: true, ok: `Saved “${name}”` });
  });
}

for (const btn of $$("[data-delete-saved]")) {
  btn.addEventListener("click", () => {
    const name = btn.dataset.deleteSaved;
    if (!confirm(`Delete the playlist “${name}”? Your files stay in the Library, and the frame keeps playing.`)) return;
    act("DELETE", `/api/saved/${encodeURIComponent(name)}`, undefined, { ok: "Deleted" })
      .then((data) => { if (data) { location.hash = "#playlists"; reloadSoon(300); } });
  });
}

// --- playlist editor ---------------------------------------------------------------

function editor(el) { return el.closest("[data-playlist]"); }

function editPlaylist(ed, changes, ok) {
  return act("POST", "/api/saved/edit", { name: ed.dataset.playlist, ...changes }, { reload: true, ok });
}

function edList(ed, field) { return readJSON(ed, field, []); }

for (const btn of $$("[data-ed-move]")) {
  btn.addEventListener("click", () => {
    const ed = editor(btn);
    const field = btn.dataset.edMove;
    const items = edList(ed, field);
    const i = Number(btn.dataset.index);
    const j = i + Number(btn.dataset.delta);
    if (j < 0 || j >= items.length) return;
    [items[i], items[j]] = [items[j], items[i]];
    editPlaylist(ed, { [field]: items });
  });
}

for (const btn of $$("[data-ed-remove]")) {
  btn.addEventListener("click", () => {
    const ed = editor(btn);
    const field = btn.dataset.edRemove;
    const items = edList(ed, field);
    items.splice(Number(btn.dataset.index), 1);
    editPlaylist(ed, { [field]: items }, "Removed");
  });
}

for (const el of $$("[data-ed-field]")) {
  el.addEventListener("change", () => {
    const value = el.type === "checkbox" ? el.checked : Number(el.value);
    editPlaylist(editor(el), { [el.dataset.edField]: value }, "Saved");
  });
}

for (const btn of $$("[data-rename]")) {
  btn.addEventListener("click", async () => {
    const ed = editor(btn);
    const name = askName("New name:", ed.dataset.playlist);
    if (!name || name === ed.dataset.playlist) return;
    const data = await act("POST", "/api/saved/edit", { name: ed.dataset.playlist, rename: name },
                           { ok: "Renamed" });
    if (data) { location.hash = editHash(data.name); reloadSoon(300); }
  });
}

// Picker: choose files from the Library for a playlist's artwork or sound.
const picker = $("#picker");
let pickerState = null;

function openPicker(ed, field) {
  const audio = field === "sounds";
  const chosen = new Set(edList(ed, field));
  pickerState = { ed, field, chosen, before: edList(ed, field), tapped: [] };
  $("#picker-title").textContent = audio ? "Add music" : "Add artwork";
  const tiles = $("#picker-tiles");
  const names = Object.keys(KINDS).filter((n) => (KINDS[n] === "audio") === audio).sort(
    (a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  // Folders first: picking one adds the whole folder, which stays linked.
  const folders = FOLDERS.filter((f) => (audio ? f.audio > 0 : f.visuals > 0 || f.count === 0));
  if (!names.length && !folders.length) {
    tiles.innerHTML = "";
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = audio ? "No music in the Library yet. Upload some first."
                          : "No artwork in the Library yet. Upload some first.";
    tiles.appendChild(p);
  } else {
    const entries = [
      ...folders.map((f) => ({ value: `folder:${f.path}`, label: `📁 ${f.path}`,
                              media: folderPreviewOf(f.path) })),
      ...names.map((n) => ({ value: n, label: n, media: previewOf(n) })),
    ];
    tiles.replaceChildren(...entries.map(({ value: name, label: text, media: els }) => {
      const tile = document.createElement("button");
      tile.type = "button";
      tile.className = "tile pick" + (chosen.has(name) ? " chosen" : "");
      tile.dataset.pick = name;
      const media = document.createElement("span");
      media.className = "tile-media";
      media.append(...els);
      const tick = document.createElement("span");
      tick.className = "pick-tick";
      tick.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-check"/></svg>';
      media.appendChild(tick);
      const label = document.createElement("span");
      label.className = "tile-name";
      label.textContent = text;
      tile.append(media, label);
      tile.addEventListener("click", () => {
        const { tapped } = pickerState;
        if (chosen.has(name)) {
          chosen.delete(name);
          if (tapped.includes(name)) tapped.splice(tapped.indexOf(name), 1);
        } else {
          chosen.add(name);
          tapped.push(name);
        }
        tile.classList.toggle("chosen", chosen.has(name));
      });
      return tile;
    }));
    watchThumbs(tiles);
  }
  if (typeof picker.showModal === "function") picker.showModal(); else picker.setAttribute("open", "");
}

if (picker) {
  for (const btn of $$("[data-pick]")) {
    if (btn.closest("#picker")) continue;
    btn.addEventListener("click", () => openPicker(editor(btn), btn.dataset.pick));
  }
  picker.addEventListener("click", (e) => { if (e.target === picker) picker.close(); });
  $("#picker-done").addEventListener("click", () => {
    const { ed, field, chosen, before, tapped } = pickerState;
    picker.close();
    // Keep the existing order; new picks go at the end, in the order they were tapped.
    const items = before.filter((n) => chosen.has(n));
    for (const name of tapped) if (!items.includes(name)) items.push(name);
    if (items.join("\n") !== before.join("\n")) editPlaylist(ed, { [field]: items }, "Saved");
  });
}

// --- library: filters and the action sheet ---------------------------------

function currentFolder() {
  let hash = "";
  try { hash = decodeURIComponent(location.hash.slice(1)); } catch (_) { /* bad URL */ }
  const path = hash.startsWith("library/") ? hash.slice("library/".length) : "";
  return FOLDERS.some((f) => f.path === path) ? path : "";
}

function folderHash(path) {
  return path ? `#library/${encodeURIComponent(path)}` : "#library";
}

// Show the folders and files of the folder we're in, and the path above it.
function renderLibrary() {
  const here = currentFolder();
  let shown = 0;
  for (const tile of $$("[data-folder-tile]")) {
    tile.hidden = tile.dataset.parent !== here || libFilter !== "all";
    if (!tile.hidden) shown += 1;
  }
  for (const tile of $$("[data-tile]")) {
    const audio = tile.dataset.kind === "audio";
    tile.hidden = tile.dataset.folder !== here
      || (libFilter === "visual" && audio) || (libFilter === "audio" && !audio);
    if (!tile.hidden) shown += 1;
  }
  const empty = $("#lib-empty");
  if (empty) {
    empty.hidden = shown > 0;
    empty.textContent = here
      ? "This folder is empty. Upload here, or use Select in another folder to move files in."
      : "Nothing here yet. Tap Upload to add videos, GIFs, images or music.";
  }
  const crumbs = $("#crumbs");
  if (crumbs) {
    const parts = here ? here.split("/") : [];
    const links = [["Library", ""], ...parts.map((p, i) => [p, parts.slice(0, i + 1).join("/")])];
    crumbs.replaceChildren(...links.flatMap(([label, path], i) => {
      const a = document.createElement(i === links.length - 1 ? "span" : "a");
      a.textContent = label;
      if (a.tagName === "A") a.href = folderHash(path);
      if (i === 0) return [a];
      const sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = "›";
      return [sep, a];
    }));
    crumbs.hidden = !here;
  }
  const hint = $("#upload-folder-hint");
  if (hint) {
    hint.textContent = here ? `Uploads go into “${here}”.` : "";
    hint.hidden = !here;
  }
}

for (const btn of $$("[data-filter]")) {
  btn.addEventListener("click", () => {
    libFilter = btn.dataset.filter;
    for (const b of $$("[data-filter]")) {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-selected", String(b === btn));
    }
    renderLibrary();
  });
}

// --- choices sheet (which folder? which playlist?) ----------------------------

const chooserDialog = $("#chooser");
let chooserResolve = null;

// The dialog's "close" event comes later, asynchronously; by then a click may have
// answered and opened the next question, so only an unanswered question resolves null.
function answerChooser(value) {
  const resolve = chooserResolve;
  chooserResolve = null;
  if (resolve) resolve(value);
}
if (chooserDialog) {
  chooserDialog.addEventListener("close", () => { if (!chooserDialog.open) answerChooser(null); });
  chooserDialog.addEventListener("click", (e) => { if (e.target === chooserDialog) chooserDialog.close(); });
}

function choose(title, options, sub = "") {
  return new Promise((resolve) => {
    if (!chooserDialog) { resolve(null); return; }
    answerChooser(null);
    chooserResolve = resolve;
    $("#chooser-title").textContent = title;
    $("#chooser-sub").textContent = sub;
    $("#chooser-sub").hidden = !sub;
    const list = $("#chooser-list");
    list.replaceChildren(...options.map((o) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn wide list-choice" + (o.ghost ? " ghost" : "") + (o.danger ? " danger" : "")
        + (o.current ? " current" : "");
      b.style.paddingLeft = `${18 + (o.depth || 0) * 18}px`;
      b.textContent = o.label;
      b.disabled = Boolean(o.disabled);
      b.addEventListener("click", () => { answerChooser(o.value); chooserDialog.close(); });
      return b;
    }));
    if (!chooserDialog.open) chooserDialog.showModal();
  });
}

function folderOptions(exclude = null) {
  const here = currentFolder();
  return [
    { label: "Library (no folder)", value: "/", current: here === "" },
    ...FOLDERS.filter((f) => !(exclude && (f.path === exclude || f.path.startsWith(exclude + "/"))))
      .map((f) => ({ label: `📁 ${f.name}`, value: f.path, depth: f.path.split("/").length - 1,
                     current: f.path === here })),
    { label: "＋ New folder…", value: "+new", ghost: true },
  ];
}

async function chooseFolder(title) {
  let path = await choose(title, folderOptions());
  if (path === null) return null;
  if (path === "+new") {
    const name = askName("Name the new folder:");
    if (!name) return null;
    const made = await act("POST", "/api/folders", { parent: currentFolder(), name });
    if (!made) return null;
    path = made.path;
  }
  return path === "/" ? "" : path;
}

async function choosePlaylist(title, sub) {
  const options = [...SAVED.map((n) => ({ label: n, value: n })),
                   { label: "＋ New playlist…", value: "+new", ghost: true }];
  const name = await choose(title, options, sub);
  if (name !== "+new") return name;
  const fresh = askName("Name the new playlist:");
  if (!fresh) return null;
  const made = await act("POST", "/api/saved/new", { name: fresh });
  return made ? made.name : null;
}

async function moveFiles(files) {
  const folder = await chooseFolder(files.length > 1 ? `Move ${files.length} files to…` : "Move to…");
  if (folder === null) return;
  await act("POST", "/api/media/move", { files, folder },
            { reload: true, ok: `Moved to ${folder ? `“${folder}”` : "the Library"}` });
}

async function addFilesToPlaylist(files) {
  const name = await choosePlaylist("Add to playlist", files.length > 1 ? `${files.length} files` : files[0]);
  if (!name) return;
  let added = 0;
  for (const filename of files) {
    const data = await act("POST", "/api/saved/add", { name, filename });
    if (!data) return;
    if (data.added) added += 1;
  }
  toast(added ? `Added ${added} to “${name}”` : `Already in “${name}”`);
  reloadSoon(900);
}

// --- folders ------------------------------------------------------------------

for (const btn of $$("[data-new-folder]")) {
  btn.addEventListener("click", async () => {
    const name = askName("Name the new folder:");
    if (!name) return;
    const made = await act("POST", "/api/folders", { parent: currentFolder(), name },
                           { ok: `Created “${name}”` });
    if (made) reloadSoon(500);
  });
}

for (const btn of $$("[data-folder-menu]")) {
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    const path = btn.dataset.folderMenu;
    const name = path.split("/").pop();
    const choice = await choose(`📁 ${name}`, [
      { label: "Add to playlist… (stays linked)", value: "add" },
      { label: "Move to…", value: "move" },
      { label: "Rename", value: "rename" },
      { label: "Delete folder (keeps the files)", value: "delete", danger: true },
    ], path.includes("/") ? path : "");
    if (choice === "add") {
      const playlist = await choosePlaylist("Add folder to playlist",
                                            "New files in the folder will play too.");
      if (!playlist) return;
      const data = await act("POST", "/api/saved/add", { name: playlist, folder: path });
      if (data) { toast(data.added ? `Added “${name}” to “${playlist}”` : `Already in “${playlist}”`); reloadSoon(900); }
    } else if (choice === "move") {
      let target = await choose(`Move “${name}” into…`, folderOptions(path).filter((o) => o.value !== "+new"));
      if (target === null) return;
      target = target === "/" ? "" : target;
      act("POST", "/api/folders/move", { path, parent: target }, { reload: true, ok: "Moved" });
    } else if (choice === "rename") {
      const fresh = askName("New folder name:", name);
      if (!fresh || fresh === name) return;
      act("POST", "/api/folders/rename", { path, name: fresh }, { reload: true, ok: "Renamed" });
    } else if (choice === "delete") {
      if (!confirm(`Delete the folder “${name}”? Its files are kept and move up a level.`)) return;
      act("POST", "/api/folders/delete", { path }, { reload: true, ok: "Folder deleted" });
    }
  });
}

// --- select mode ----------------------------------------------------------------

const selbar = $("#selbar");

function setSelecting(on) {
  document.body.classList.toggle("selecting", on);
  if (selbar) selbar.hidden = !on;
  if (!on) {
    selected.clear();
    for (const t of $$("[data-tile].chosen")) t.classList.remove("chosen");
  }
  const btn = $("#btn-select");
  if (btn) btn.textContent = on ? "Done" : "Select";
  updateSelection();
}

function updateSelection() {
  const n = selected.size;
  const count = $("#sel-count");
  if (count) {
    count.textContent = String(n);
    count.setAttribute("aria-label", `${n} selected`);
  }
  for (const b of $$("[data-sel]")) if (b.dataset.sel !== "cancel") b.disabled = n === 0;
}

const selectBtn = $("#btn-select");
if (selectBtn) {
  selectBtn.addEventListener("click", () => setSelecting(!document.body.classList.contains("selecting")));
}

for (const b of $$("[data-sel]")) {
  b.addEventListener("click", async () => {
    const files = [...selected];
    if (b.dataset.sel === "cancel") { setSelecting(false); return; }
    if (!files.length) return;
    if (b.dataset.sel === "move") await moveFiles(files);
    else if (b.dataset.sel === "add") await addFilesToPlaylist(files);
    else if (b.dataset.sel === "delete") {
      if (!confirm(`Delete ${files.length} file${files.length === 1 ? "" : "s"}? They're also removed from your playlists. This can't be undone.`)) return;
      for (const name of files) {
        const url = `/api/media/${encodeURIComponent(name)}` + (LIVE.includes(name) ? "?force=1" : "");
        if (!(await act("DELETE", url))) return;
      }
      toast(`Deleted ${files.length} file${files.length === 1 ? "" : "s"}`);
      reloadSoon(800);
    }
  });
}

const sheet = $("#sheet");
let sheetName = null;

function openSheet(tile) {
  sheetName = tile.dataset.tile;
  const audio = tile.dataset.kind === "audio";
  $("#sheet-title").textContent = sheetName;
  $("#sheet-meta").textContent = $(".tile-meta", tile).textContent;
  const warning = tile.dataset.warning;
  $("#sheet-warning").textContent = warning ? `⚠ ${warning}` : "";
  $("#sheet-warning").hidden = !warning;
  const thumb = $("#sheet-thumb");
  setPreview(thumb, sheetName);
  $("#sheet-play span").textContent = audio ? "Play only this music now" : "Show only this artwork now";
  $("#sheet-lists").hidden = true;
  $("#sheet-add").hidden = false;
  // Mark the playlists that already have this file.
  for (const btn of $$("[data-add-to]")) {
    const ed = $$("[data-playlist]").find((el) => el.dataset.playlist === btn.dataset.addTo);
    const has = ed && [...edList(ed, "items"), ...edList(ed, "sounds")].includes(sheetName);
    btn.classList.toggle("has", Boolean(has));
  }
  if (typeof sheet.showModal === "function") sheet.showModal();
  else sheet.setAttribute("open", "");
}

function closeSheet() {
  if (typeof sheet.close === "function") sheet.close();
  else sheet.removeAttribute("open");
}

for (const tile of $$("[data-tile]")) {
  tile.addEventListener("click", () => {
    if (!document.body.classList.contains("selecting")) { openSheet(tile); return; }
    const name = tile.dataset.tile;
    if (selected.has(name)) selected.delete(name); else selected.add(name);
    tile.classList.toggle("chosen", selected.has(name));
    updateSelection();
  });
}

if (sheet) {
  // Tap outside the sheet to close it.
  sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });

  $("#sheet-play").addEventListener("click", () => {
    const audio = KINDS[sheetName] === "audio";
    closeSheet();
    act("POST", audio ? "/api/soundtrack" : "/api/play", { filename: sheetName },
        { reload: true, ok: audio ? "Playing this music" : "Showing this artwork" });
  });

  $("#sheet-move").addEventListener("click", () => {
    const name = sheetName;
    closeSheet();
    moveFiles([name]);
  });

  $("#sheet-add").addEventListener("click", () => {
    $("#sheet-add").hidden = true;
    $("#sheet-lists").hidden = false;
  });

  for (const btn of $$("[data-add-to]")) {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.addTo;
      closeSheet();
      const data = await act("POST", "/api/saved/add", { name, filename: sheetName });
      if (data) {
        toast(data.added ? `Added to “${name}”` : `Already in “${name}”`);
        reloadSoon(900);
      }
    });
  }

  const addToNew = $("[data-add-to-new]");
  if (addToNew) {
    addToNew.addEventListener("click", async () => {
      const name = askName("Name the new playlist:");
      if (!name) return;
      closeSheet();
      const made = await act("POST", "/api/saved/new", { name });
      if (made && await act("POST", "/api/saved/add", { name: made.name, filename: sheetName })) {
        toast(`Added to “${made.name}”`);
        reloadSoon(900);
      }
    });
  }

  $("#sheet-delete").addEventListener("click", () => {
    const listed = LIVE.includes(sheetName);
    const q = listed
      ? `Delete “${sheetName}”? It's playing now, so the frame moves on. It's also removed from your playlists. This can't be undone.`
      : `Delete “${sheetName}”? It's also removed from your playlists. This can't be undone.`;
    if (!confirm(q)) return;
    closeSheet();
    const url = `/api/media/${encodeURIComponent(sheetName)}` + (listed ? "?force=1" : "");
    act("DELETE", url, undefined, { reload: true, ok: "Deleted" });
  });
}

// --- upload ----------------------------------------------------------------
// Files go one at a time (one failure doesn't lose the batch), after checking
// there's room: the web server stores a whole upload before Frame can check it.

const STALL_SECONDS = 45;        // no progress for this long = stalled
const PROCESSING_SECONDS = 600;  // after 100%: saving / converting on the Pi

const fmtSize = (bytes) => bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB`
  : bytes >= 1048576 ? `${Math.round(bytes / 1048576)} MB` : "less than 1 MB";

function uploadOne(file, target, onProgress) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    form.append("file", file, file.name);
    if (target) form.append("to", target);
    const folder = currentFolder();
    if (folder) form.append("folder", folder);
    let last = Date.now();
    let sent = false;
    const watchdog = setInterval(() => {
      const limit = sent ? PROCESSING_SECONDS : STALL_SECONDS;
      if (Date.now() - last > limit * 1000) {
        clearInterval(watchdog);
        xhr.abort();
        resolve({ error: sent
          ? "the frame took too long to save it"
          : "the upload stalled — check the Wi-Fi and keep this page open", stalled: true });
      }
    }, 2000);
    xhr.upload.onprogress = (ev) => {
      last = Date.now();
      if (!ev.lengthComputable) return;
      sent = ev.loaded >= ev.total;
      onProgress(ev.loaded / ev.total);
    };
    xhr.upload.onload = () => { sent = true; last = Date.now(); onProgress(1); };
    xhr.onload = () => {
      clearInterval(watchdog);
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else resolve({ error: data.error || `failed (${xhr.status})`, status: xhr.status });
    };
    xhr.onerror = () => {
      clearInterval(watchdog);
      resolve({ error: "the connection to the frame was lost", stalled: true });
    };
    xhr.open("POST", "/api/media");
    xhr.send(form);
  });
}

// Before uploading, ask the frame which files it already has. Flagged files start
// left out of the upload; the user can put them back (or take others out).
// Resolves with the files to upload, or null if cancelled.
async function reviewUploads(files) {
  let checks;
  try {
    const data = await api("POST", "/api/media/check",
                           { files: files.map((f) => ({ name: f.name, size: f.size })) });
    checks = data.files;
  } catch (_) {
    return files;  // can't check: upload as before
  }
  const flagged = checks.filter((c) => c.status !== "ok").length;
  const dialog = $("#upload-review");
  if (!flagged || !dialog) return files;

  const rows = files.map((file, i) => ({ file, check: checks[i] || { status: "ok" } }));
  for (const row of rows) row.keep = row.check.status === "ok" || row.check.status === "same_name";
  rows.sort((a, b) => Number(a.check.status === "ok") - Number(b.check.status === "ok"));

  const dupes = checks.filter((c) => c.status === "duplicate" || c.status === "twice").length;
  $("#review-title").textContent = dupes
    ? `${dupes} of ${files.length} ${dupes === 1 ? "file is" : "files are"} already on the frame`
    : `Check ${flagged === 1 ? "this file" : "these files"} before uploading`;
  $("#review-sub").textContent = "Files already on the frame are left out. Tap Keep to upload one anyway, "
    + "or Remove to leave out any other file.";

  const list = $("#review-list");
  const go = $("#review-go");
  const render = () => {
    list.replaceChildren(...rows.map((row) => {
      const { file, check } = row;
      const li = document.createElement("li");
      li.className = `review-row ${check.status}` + (row.keep ? "" : " out");
      const text = document.createElement("span");
      text.className = "q-name";
      const name = document.createElement("span");
      name.className = "rv-name";
      name.textContent = file.name;
      text.append(name);
      const small = document.createElement("small");
      const renamed = check.existing && check.existing !== file.name ? ` as ${check.existing}` : "";
      const where = renamed + (check.folder ? ` in “${check.folder}”` : "");
      small.textContent = check.status === "ok" ? fmtSize(file.size)
        : check.status === "duplicate" ? `Already on the frame${where}`
        : check.status === "twice" ? "Chosen twice"
        : check.message || check.status;
      small.className = check.status === "ok" ? "" : "warn";
      text.append(small);
      li.append(text);
      if (check.status !== "unsupported") {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn small" + (row.keep ? " ghost" : "");
        b.textContent = row.keep ? "Remove" : "Keep";
        b.addEventListener("click", () => { row.keep = !row.keep; render(); });
        li.append(b);
      }
      return li;
    }));
    const n = rows.filter((r) => r.keep).length;
    go.disabled = n === 0;
    $("span", go).textContent = n ? `Upload ${n}` : "Nothing to upload";
  };
  render();

  return new Promise((resolve) => {
    let answer = null;
    const onGo = () => { answer = files.filter((f) => rows.find((r) => r.file === f).keep); dialog.close(); };
    go.addEventListener("click", onGo);
    dialog.addEventListener("close", () => { go.removeEventListener("click", onGo); resolve(answer); },
                            { once: true });
    dialog.showModal();
  });
}

async function keepAwake() {
  // Stops the phone from sleeping (and pausing the upload) where it's supported.
  try { return await navigator.wakeLock.request("screen"); } catch (_) { return null; }
}

const fileInput = $("#file");
let uploading = false;
window.addEventListener("beforeunload", (e) => { if (uploading) { e.preventDefault(); e.returnValue = ""; } });

if (fileInput) {
  fileInput.addEventListener("change", async () => {
    if (!fileInput.files.length || uploading) return;
    const files = await reviewUploads(Array.from(fileInput.files));
    if (!files || !files.length) {
      fileInput.value = "";
      return;
    }
    const bar = $("#upload-progress");
    const label = $(".upload-btn span");
    const target = $("#upload-to")?.value || "";
    const total = files.reduce((n, f) => n + f.size, 0);

    // Room for everything? Each file briefly needs twice its size while it's saved.
    try {
      const space = await api("GET", "/api/space");
      const tooBig = files.find((f) => f.size > space.max_upload_bytes);
      if (tooBig) {
        toast(`“${tooBig.name}” is ${fmtSize(tooBig.size)}; the limit is ${fmtSize(space.max_upload_bytes)} per file.`, true);
        fileInput.value = "";
        return;
      }
      const largest = Math.max(...files.map((f) => f.size));
      const needed = total + largest + space.reserve_bytes;
      if (space.free_bytes !== null && needed > space.free_bytes) {
        toast(`Not enough space on the frame: ${files.length > 1 ? "these files need" : "this file needs"} ` +
              `${fmtSize(total + largest)}, but only ${fmtSize(Math.max(0, space.free_bytes - space.reserve_bytes))} ` +
              "is free. Delete some files or make them smaller first.", true);
        fileInput.value = "";
        return;
      }
    } catch (e) {
      toast(e.message, true);
      fileInput.value = "";
      return;
    }

    uploading = true;
    const lock = await keepAwake();
    bar.value = 0;
    bar.hidden = false;
    const saved = [];
    const failed = [];
    const warnings = [];
    let doneBytes = 0;
    for (const [i, file] of files.entries()) {
      const prefix = files.length > 1 ? `${i + 1} of ${files.length} · ` : "";
      label.textContent = `${prefix}Uploading…`;
      const result = await uploadOne(file, target, (fraction) => {
        bar.value = Math.round(((doneBytes + fraction * file.size) / total) * 100);
        label.textContent = fraction < 1
          ? `${prefix}Uploading… ${Math.round(fraction * 100)}%`
          : `${prefix}Processing…`;
      });
      doneBytes += file.size;
      if (result.error) {
        failed.push(`${file.name}: ${result.error}`);
        // Out of space or a lost connection: the rest would fail the same way.
        if (result.status === 507 || result.stalled) {
          for (const rest of files.slice(i + 1)) failed.push(`${rest.name}: not uploaded`);
          break;
        }
      } else {
        saved.push(...(result.saved || []));
        if (result.warning) warnings.push(result.warning);
      }
    }
    uploading = false;
    if (lock) lock.release().catch(() => {});
    bar.hidden = true;
    label.textContent = "Upload";
    fileInput.value = "";

    if (failed.length) {
      const head = saved.length ? `Uploaded ${saved.length} of ${files.length}. ` : "Upload failed. ";
      toast(head + failed.join(" · "), true);
    } else if (warnings.length) {
      toast(warnings.join(" "), true);
    } else {
      toast(saved.length === 1 ? `Uploaded ${saved[0]}` : `Uploaded ${saved.length} files`);
    }
    if (saved.length) reloadSoon(failed.length || warnings.length ? 7000 : 900);
  });
}

// --- settings -------------------------------------------------------------

for (const sel of $$("[data-setting]")) {
  sel.addEventListener("change", () => {
    let value = sel.value;
    if (["rotation", "fade"].includes(sel.dataset.setting)) value = Number(value);
    act("POST", "/api/settings", { [sel.dataset.setting]: value }, { ok: "Saved" });
  });
}

async function loadAudioDevices() {
  const sel = $("#audio-device");
  if (!sel) return;
  try {
    const { devices } = await api("GET", "/api/audio-devices");
    const current = sel.dataset.current;
    for (const d of devices) {
      if (d.name === "auto" || d.name === current) continue;
      const opt = document.createElement("option");
      opt.value = d.name;
      opt.textContent = d.description ? `${d.description} (${d.name})` : d.name;
      sel.appendChild(opt);
    }
  } catch (_) { /* player offline: keep the short list */ }
}

refreshStatus();
loadAudioDevices();
setInterval(() => { if (!document.hidden) refreshStatus(); }, 5000);
setInterval(renderCountdowns, 1000);
