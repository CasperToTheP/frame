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
  if (!names.length) {
    tiles.innerHTML = "";
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = audio ? "No music in the Library yet. Upload some first."
                          : "No artwork in the Library yet. Upload some first.";
    tiles.appendChild(p);
  } else {
    tiles.replaceChildren(...names.map((name) => {
      const tile = document.createElement("button");
      tile.type = "button";
      tile.className = "tile pick" + (chosen.has(name) ? " chosen" : "");
      tile.dataset.pick = name;
      const media = document.createElement("span");
      media.className = "tile-media";
      media.append(...previewOf(name));
      const tick = document.createElement("span");
      tick.className = "pick-tick";
      tick.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-check"/></svg>';
      media.appendChild(tick);
      const label = document.createElement("span");
      label.className = "tile-name";
      label.textContent = name;
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

for (const btn of $$("[data-filter]")) {
  btn.addEventListener("click", () => {
    const f = btn.dataset.filter;
    for (const b of $$("[data-filter]")) {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-selected", String(b === btn));
    }
    for (const tile of $$("[data-tile]")) {
      const audio = tile.dataset.kind === "audio";
      tile.hidden = (f === "visual" && audio) || (f === "audio" && !audio);
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

for (const tile of $$("[data-tile]")) tile.addEventListener("click", () => openSheet(tile));

if (sheet) {
  // Tap outside the sheet to close it.
  sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });

  $("#sheet-play").addEventListener("click", () => {
    const audio = KINDS[sheetName] === "audio";
    closeSheet();
    act("POST", audio ? "/api/soundtrack" : "/api/play", { filename: sheetName },
        { reload: true, ok: audio ? "Playing this music" : "Showing this artwork" });
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

// --- upload (XHR for progress reporting); starts as soon as files are picked ---

const fileInput = $("#file");
if (fileInput) {
  fileInput.addEventListener("change", () => {
    const form = $("#upload-form");
    if (!fileInput.files.length) return;
    const bar = $("#upload-progress");
    const label = $(".upload-btn span");
    const count = fileInput.files.length;
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/media");
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.round((ev.loaded / ev.total) * 100);
      bar.value = pct;
      // At 100% the frame may still be converting (animated WebP) or checking the file.
      label.textContent = pct < 100 ? `Uploading… ${pct}%` : "Processing…";
    };
    const done = () => {
      bar.hidden = true;
      label.textContent = "Upload files";
      fileInput.value = "";
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* non-JSON */ }
      done();
      if (xhr.status >= 200 && xhr.status < 300) {
        const n = data.saved.length;
        toast(data.warning || `Uploaded ${n === 1 ? data.saved[0] : `${n} files`}`, Boolean(data.warning));
        // Leave a warning up long enough to read; the library shows it again after.
        reloadSoon(data.warning ? 6000 : 900);
      } else {
        toast(data.error || `Upload failed (${xhr.status})`, true);
      }
    };
    xhr.onerror = () => {
      done();
      toast("Upload interrupted. Check the Wi-Fi connection and try again.", true);
    };
    bar.value = 0;
    bar.hidden = false;
    label.textContent = count > 1 ? `Uploading ${count} files…` : "Uploading…";
    xhr.send(new FormData(form));
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
