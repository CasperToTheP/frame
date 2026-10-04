// Frame management UI. Plain JavaScript, no build step.
"use strict";

const $ = (sel) => document.querySelector(sel);

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = isError ? "error" : "";
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 8000 : 3000);
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

async function act(method, url, body, { reload = false, ok } = {}) {
  try {
    const data = await api(method, url, body);
    if (data.warning) toast(data.warning, true);
    else if (ok) toast(ok);
    if (reload) setTimeout(() => location.reload(), data.warning ? 1500 : 200);
    else refreshStatus();
    return data;
  } catch (e) {
    toast(e.message, true);
  }
}

// --- now playing ----------------------------------------------------------

let nowState = JSON.parse(document.body.dataset.now || "{}");
let clockOffset = 0; // server time minus phone time, in seconds

function fmtCountdown(nextAt) {
  if (!nextAt) return "";
  const secs = Math.max(0, Math.round(nextAt - (Date.now() / 1000 + clockOffset)));
  const m = Math.floor(secs / 60);
  const s = String(secs % 60).padStart(2, "0");
  return ` · next in ${m}:${s}`;
}

function renderCountdowns() {
  const v = nowState.visual || {};
  const snd = nowState.sound || {};
  $("#position").textContent = v.count > 1 && v.position ? ` · ${v.position} of ${v.count}` : "";
  $("#countdown").textContent = fmtCountdown(v.next_at);
  $("#sound-countdown").textContent =
    (snd.count > 1 && snd.position ? ` (${snd.position} of ${snd.count})` : "") + fmtCountdown(snd.next_at);
}

function renderStatus(s) {
  nowState = s.now || {};
  if (s.server_time) clockOffset = s.server_time - Date.now() / 1000;
  $("#current").textContent = nowState.visual?.current || "Nothing (black screen)";
  $("#sound-now").textContent = nowState.sound?.current || "artwork's own";
  const err = $("#player-error");
  err.textContent = nowState.error || "";
  err.hidden = !nowState.error;
  $("#btn-next").hidden = !(nowState.visual?.count > 1);
  renderCountdowns();
  $("#playback").textContent = s.playback;
  const health = $("#health");
  health.textContent = s.player.reachable ? "player online" : "player offline";
  health.className = "badge " + (s.player.reachable ? "ok" : "bad");
  const mute = $("#btn-mute");
  mute.textContent = s.muted ? "Unmute" : "Mute";
  mute.setAttribute("aria-pressed", String(s.muted));
  $("#btn-pause").disabled = s.playback !== "playing";
  $("#btn-resume").disabled = s.playback !== "paused";
  const vol = $("#volume");
  if (document.activeElement !== vol) {
    vol.value = s.volume;
    $("#volume-out").textContent = s.volume;
  }
  const hw = $("#hwdec-current");
  if (hw) hw.textContent = (s.player.hwdec_current || "–");
}

async function refreshStatus() {
  try {
    renderStatus(await api("GET", "/api/status"));
  } catch (e) {
    const health = $("#health");
    health.textContent = "frame unreachable";
    health.className = "badge bad";
  }
}

document.querySelectorAll("[data-action]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const action = btn.dataset.action;
    if (action === "stop") {
      if (!confirm("Show a black screen? The playlist is kept; tap Start to continue.")) return;
      act("POST", "/api/stop", {}, { reload: true });
    } else if (action === "start") {
      act("POST", "/api/start", {}, { reload: true });
    } else if (action === "mute") {
      act("POST", "/api/mute", {});
    } else {
      act("POST", `/api/${action}`, {});
    }
  });
});

// Volume: send while dragging, but at most every 150 ms.
let volTimer = null;
$("#volume").addEventListener("input", (e) => {
  const v = Number(e.target.value);
  $("#volume-out").textContent = v;
  clearTimeout(volTimer);
  volTimer = setTimeout(() => act("POST", "/api/volume", { volume: v }), 150);
});

// --- library --------------------------------------------------------------

document.querySelectorAll("[data-play]").forEach((btn) => {
  btn.addEventListener("click", () => {
    btn.disabled = true;
    act("POST", "/api/play", { filename: btn.dataset.play }, { reload: true })
      .finally(() => { btn.disabled = false; });
  });
});

document.querySelectorAll("[data-sound]").forEach((btn) => {
  btn.addEventListener("click", () => {
    btn.disabled = true;
    act("POST", "/api/soundtrack", { filename: btn.dataset.sound }, { reload: true })
      .finally(() => { btn.disabled = false; });
  });
});

document.querySelectorAll("[data-add]").forEach((btn) => {
  btn.addEventListener("click", () => {
    btn.disabled = true;
    act("POST", "/api/add", { filename: btn.dataset.add }, { reload: true });
  });
});

document.querySelectorAll("[data-next]").forEach((btn) => {
  btn.addEventListener("click", () => act("POST", "/api/next", { which: btn.dataset.next }));
});

// --- playlist and sound list ------------------------------------------------

const LIST_API = { playlist: "/api/playlist", sounds: "/api/sounds" };

function listItems(id) {
  return JSON.parse(document.getElementById(id).dataset.items || "[]");
}

function saveList(id, items) {
  act("POST", LIST_API[id], { items }, { reload: true });
}

document.querySelectorAll("[data-move]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const id = btn.dataset.move;
    const items = listItems(id);
    const i = Number(btn.dataset.index);
    const j = i + Number(btn.dataset.delta);
    if (j < 0 || j >= items.length) return;
    [items[i], items[j]] = [items[j], items[i]];
    saveList(id, items);
  });
});

document.querySelectorAll("[data-remove]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const id = btn.dataset.remove;
    const items = listItems(id);
    items.splice(Number(btn.dataset.index), 1);
    saveList(id, items);
  });
});

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
  ownSound.addEventListener("click", () => act("POST", "/api/sounds", { items: [] }, { reload: true }));
}

document.querySelectorAll("[data-delete]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const name = btn.dataset.delete;
    const listed = btn.dataset.listed === "1";
    const q = listed
      ? `"${name}" is in the playlist or sound list. Remove it and delete the file? This cannot be undone.`
      : `Delete "${name}"? This cannot be undone.`;
    if (!confirm(q)) return;
    const url = `/api/media/${encodeURIComponent(name)}` + (listed ? "?force=1" : "");
    act("DELETE", url, undefined, { reload: true, ok: "Deleted" });
  });
});

// --- upload (XHR for progress reporting) ----------------------------------

$("#upload-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  const file = form.file.files[0];
  if (!file) return;
  const bar = $("#upload-progress");
  const submit = form.querySelector("button[type=submit]");
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/media");
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) bar.value = Math.round((ev.loaded / ev.total) * 100);
  };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) { /* non-JSON */ }
    submit.disabled = false;
    bar.hidden = true;
    if (xhr.status >= 200 && xhr.status < 300) {
      toast(data.warning || `Uploaded ${data.saved.join(", ")}`, Boolean(data.warning));
      setTimeout(() => location.reload(), 800);
    } else {
      toast(data.error || `Upload failed (${xhr.status})`, true);
    }
  };
  xhr.onerror = () => {
    submit.disabled = false;
    bar.hidden = true;
    toast("Upload interrupted. Check the Wi-Fi connection and try again.", true);
  };
  submit.disabled = true;
  bar.value = 0;
  bar.hidden = false;
  xhr.send(new FormData(form));
});

// --- settings -------------------------------------------------------------

document.querySelectorAll("[data-setting]").forEach((sel) => {
  sel.addEventListener("change", () => {
    let value = sel.value;
    if (["rotation", "fade"].includes(sel.dataset.setting)) value = Number(value);
    act("POST", "/api/settings", { [sel.dataset.setting]: value }, { ok: "Saved" });
  });
});

async function loadAudioDevices() {
  const sel = $("#audio-device");
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
