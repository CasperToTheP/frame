// Frame management UI. Plain JavaScript, no build step.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const readJSON = (el, key, fallback) => {
  try { return JSON.parse(el.dataset[key] || ""); } catch (_) { return fallback; }
};

const KINDS = readJSON(document.body, "kinds", {});      // filename -> kind
const SAVED = readJSON(document.body, "saved", []);      // saved playlist names
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

function showView() {
  const name = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "now";
  for (const v of $$(".view")) v.hidden = v.dataset.view !== name;
  for (const a of $$(".tabbar a")) {
    const active = a.dataset.tab === name;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}
window.addEventListener("hashchange", () => { showView(); scrollTo(0, 0); });
showView();
try {
  const saved = JSON.parse(sessionStorage.getItem("frame-scroll") || "null");
  sessionStorage.removeItem("frame-scroll");
  if (saved && saved[0] === location.hash) requestAnimationFrame(() => scrollTo(0, saved[1]));
} catch (_) {}

// --- previews: videos load only their first frame, and only when visible ----

const videoObserver = "IntersectionObserver" in window
  ? new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const v = e.target;
      v.preload = "metadata";
      v.src = v.dataset.src;
      videoObserver.unobserve(v);
    }
  }, { rootMargin: "200px" })
  : null;

function watchVideos(root = document) {
  for (const v of $$("video[data-src]", root)) {
    if (videoObserver) videoObserver.observe(v);
    else { v.preload = "metadata"; v.src = v.dataset.src; }
  }
}
watchVideos();

// Copies of a file's preview elements, taken from its Library tile.
function previewOf(name) {
  const tile = name && document.querySelector(`[data-tile="${CSS.escape(name)}"] .tile-media`);
  if (!tile) return [];
  return Array.from(tile.querySelectorAll(":scope > img, :scope > video, :scope > .ph"), (el) => {
    const copy = el.cloneNode(true);
    if (copy.tagName === "VIDEO") copy.removeAttribute("src");
    return copy;
  });
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
  if (current !== heroName) {
    heroName = current;
    const hero = $("#hero-media");
    hero.replaceChildren(...previewOf(current));
    watchVideos(hero);
  }
  const err = $("#player-error");
  err.textContent = nowState.error || "";
  err.hidden = !nowState.error;
  $("#btn-next").disabled = !(nowState.visual?.count > 1);
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

  const toggle = $("#btn-toggle");
  const paused = s.playback === "paused";
  setIcon(toggle, paused ? "play" : "pause");
  toggle.setAttribute("aria-label", paused ? "Play" : "Pause");
  toggle.disabled = !["playing", "paused"].includes(s.playback);

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

// --- saved playlists ----------------------------------------------------------

for (const btn of $$("[data-load]")) {
  btn.addEventListener("click", () => {
    act("POST", "/api/saved/load", { name: btn.dataset.load },
        { reload: true, ok: `Playing “${btn.dataset.load}”` });
  });
}

for (const btn of $$("[data-save]")) {
  btn.addEventListener("click", () => {
    if (!listItems("playlist").length) {
      toast("Add some artwork to the playlist first — open the Library and tap a file.", true);
      return;
    }
    const name = (prompt("Name this playlist — for example “Sleeping” or “Morning”:") || "").trim();
    if (!name) return;
    if (SAVED.includes(name) && !confirm(`Replace the saved playlist “${name}” with what's playing now?`)) return;
    act("POST", "/api/saved", { name }, { reload: true, ok: `Saved “${name}”` });
  });
}

for (const btn of $$("[data-delete-saved]")) {
  btn.addEventListener("click", () => {
    const name = btn.dataset.deleteSaved;
    if (!confirm(`Delete the saved playlist “${name}”? Your files stay in the Library.`)) return;
    act("DELETE", `/api/saved/${encodeURIComponent(name)}`, undefined, { reload: true, ok: "Deleted" });
  });
}

// --- current artwork and sound lists -----------------------------------------

const LIST_API = { playlist: "/api/playlist", sounds: "/api/sounds" };

function listItems(id) {
  const el = document.getElementById(id);
  return el ? readJSON(el, "items", []) : [];
}

function saveList(id, items, ok) {
  return act("POST", LIST_API[id], { items }, { reload: true, ok });
}

for (const btn of $$("[data-move]")) {
  btn.addEventListener("click", () => {
    const id = btn.dataset.move;
    const items = listItems(id);
    const i = Number(btn.dataset.index);
    const j = i + Number(btn.dataset.delta);
    if (j < 0 || j >= items.length) return;
    [items[i], items[j]] = [items[j], items[i]];
    saveList(id, items);
  });
}

for (const btn of $$("[data-remove]")) {
  btn.addEventListener("click", () => {
    const id = btn.dataset.remove;
    const items = listItems(id);
    items.splice(Number(btn.dataset.index), 1);
    saveList(id, items);
  });
}

function bindOption(sel, url, field, convert) {
  const el = $(sel);
  if (!el) return;
  el.addEventListener("change", () => {
    act("POST", url, { [field]: convert(el) }, { reload: true, ok: "Saved" });
  });
}
bindOption("#interval", "/api/playlist", "interval", (el) => Number(el.value));
bindOption("#shuffle", "/api/playlist", "shuffle", (el) => el.checked);
bindOption("#sound-interval", "/api/sounds", "interval", (el) => Number(el.value));
bindOption("#sound-shuffle", "/api/sounds", "shuffle", (el) => el.checked);

const ownSound = $("#btn-own-sound");
if (ownSound) {
  ownSound.addEventListener("click", () => saveList("sounds", [], "Each artwork plays its own sound"));
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
  const listed = tile.dataset.listed === "1";
  $("#sheet-title").textContent = sheetName;
  $("#sheet-meta").textContent = $(".tile-meta", tile).textContent;
  const warning = tile.dataset.warning;
  $("#sheet-warning").textContent = warning ? `⚠ ${warning}` : "";
  $("#sheet-warning").hidden = !warning;
  const thumb = $("#sheet-thumb");
  thumb.replaceChildren(...previewOf(sheetName));
  watchVideos(thumb);
  $("#sheet-play span").textContent = audio ? "Play only this music" : "Show only this artwork";
  const list = $("#sheet-list");
  list.textContent = listed
    ? (audio ? "Remove from sound list" : "Remove from playlist")
    : (audio ? "Add to sound list" : "Add to playlist");
  list.dataset.listed = listed ? "1" : "";
  list.dataset.audio = audio ? "1" : "";
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

  $("#sheet-list").addEventListener("click", (e) => {
    const btn = e.currentTarget;
    const id = btn.dataset.audio ? "sounds" : "playlist";
    closeSheet();
    if (btn.dataset.listed) {
      saveList(id, listItems(id).filter((n) => n !== sheetName), "Removed");
    } else {
      act("POST", "/api/add", { filename: sheetName }, { reload: true, ok: "Added" });
    }
  });

  $("#sheet-delete").addEventListener("click", () => {
    const listed = listItems("playlist").includes(sheetName) || listItems("sounds").includes(sheetName);
    const q = listed
      ? `Delete “${sheetName}”? It's also removed from the playlist and saved playlists. This can't be undone.`
      : `Delete “${sheetName}”? This can't be undone.`;
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
      label.textContent = `Uploading… ${pct}%`;
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
