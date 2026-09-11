const $ = (selector) => document.querySelector(selector);
const state = { mode: "login", token: localStorage.getItem("verifeye_token"), file: null, streamStops: new Map(), identityImageUrls: new Set(), currentUser: null, cameraLoadController: null, cameraLoadGeneration: 0, cameraEventPoll: null, setupRequired: false };

function setBrandMenu(open) {
  $("#brand-menu").classList.toggle("open", open);
  $("#brand-menu-toggle").setAttribute("aria-expanded", String(open));
  $("#brand-menu-panel").setAttribute("aria-hidden", String(!open));
  $("#brand-menu-panel").inert = !open;
}

$("#brand-menu-toggle").addEventListener("click", () => setBrandMenu(!$("#brand-menu").classList.contains("open")));
document.addEventListener("click", event => { if (!$("#brand-menu").contains(event.target)) setBrandMenu(false); });
document.addEventListener("keydown", event => { if (event.key === "Escape") { setBrandMenu(false); $("#brand-menu-toggle").focus(); } });

const brandEye = $(".mark");
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
let eyeTrackingFrame = null;
function centerBrandEye() {
  brandEye.style.setProperty("--pupil-x", "0px");
  brandEye.style.setProperty("--pupil-y", "0px");
}
document.addEventListener("pointermove", event => {
  if (event.pointerType === "touch" || reducedMotion.matches) return;
  cancelAnimationFrame(eyeTrackingFrame);
  eyeTrackingFrame = requestAnimationFrame(() => {
    const rect = brandEye.getBoundingClientRect();
    const dx = event.clientX - (rect.left + rect.width / 2);
    const dy = event.clientY - (rect.top + rect.height / 2);
    const distance = Math.hypot(dx, dy) || 1;
    const strength = Math.min(1, distance / 90);
    brandEye.style.setProperty("--pupil-x", `${(dx / distance) * 4.2 * strength}px`);
    brandEye.style.setProperty("--pupil-y", `${(dy / distance) * 2.8 * strength}px`);
  });
}, {passive: true});
document.addEventListener("click", () => {
  brandEye.classList.remove("blinking");
  void brandEye.offsetWidth;
  brandEye.classList.add("blinking");
});
brandEye.addEventListener("animationend", () => brandEye.classList.remove("blinking"));
window.addEventListener("blur", centerBrandEye);
reducedMotion.addEventListener("change", event => { if (event.matches) centerBrandEye(); });

function setTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  $("#theme-toggle").setAttribute("aria-pressed", String(dark));
  $("#theme-toggle").setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} mode`);
  $(".theme-icon").textContent = dark ? "☀" : "☾";
  $(".theme-label").textContent = dark ? "Light" : "Dark";
}

setTheme(document.documentElement.dataset.theme || "light");
$("#theme-toggle").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("verifeye_theme", theme);
  setTheme(theme);
});

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = "Something went wrong. Please try again.";
    try { message = (await response.json()).detail || message; } catch (_) {}
    const error = new Error(message); error.status = response.status; throw error;
  }
  return response.status === 204 ? null : response.json();
}

function setMode(mode) {
  if (mode === "register" && !state.setupRequired) mode = "login";
  state.mode = mode;
  const registering = mode === "register";
  $("#login-tab").classList.toggle("active", !registering);
  $("#register-tab").classList.toggle("active", registering);
  $("#login-tab").setAttribute("aria-selected", String(!registering));
  $("#register-tab").setAttribute("aria-selected", String(registering));
  $("#name-field").classList.toggle("hidden", !registering);
  $("#display-name").required = registering;
  $("#confirm-password-field").classList.toggle("hidden", !registering);
  $("#confirm-password").required = registering;
  if (!registering) $("#confirm-password").value = "";
  $("#password").autocomplete = registering ? "new-password" : "current-password";
  $("#form-title").textContent = registering ? "Make it yours." : "Good to see you.";
  $("#form-subtitle").textContent = registering ? "Create your private, local account." : "Sign in to manage your identity.";
  $("#submit-label").textContent = registering ? "Create account" : "Sign in";
  $("#auth-error").textContent = "";
}

function showDashboard(user) {
  setBrandMenu(false);
  state.currentUser = user;
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  const guest = user.role === "guest";
  $("#app-nav").classList.toggle("hidden", guest);
  $("#guest-nav").classList.toggle("hidden", !guest);
  $("#privacy-badge").classList.add("hidden");
  if (guest) {
    document.querySelectorAll(".app-page").forEach(page => page.classList.add("hidden"));
    $("#guest-cameras-page").classList.remove("hidden");
    if (location.pathname !== "/guest/cameras") history.replaceState({}, "", "/guest/cameras");
    loadGuestCameras();
  } else {
    if (location.pathname === "/guest/cameras") history.replaceState({}, "", "/");
    showPage(new URLSearchParams(location.search).has("event") ? "events" : "dashboard");
  }
}

function showAuth() {
  setBrandMenu(false);
  stopAllStreams();
  stopCameraDashboardUpdates();
  clearIdentityImageUrls();
  state.token = null; localStorage.removeItem("verifeye_token");
  state.currentUser = null;
  $("#app-view").classList.add("hidden"); $("#auth-view").classList.remove("hidden");
  $("#app-nav").classList.add("hidden"); $("#guest-nav").classList.add("hidden"); $("#privacy-badge").classList.remove("hidden");
  refreshSetupStatus();
}

async function refreshSetupStatus() {
  try {
    const setup = await api("/api/auth/setup");
    state.setupRequired = setup.setupRequired;
    $("#register-tab").classList.toggle("hidden", !state.setupRequired);
    if (!state.setupRequired && state.mode === "register") setMode("login");
    if (state.setupRequired) setMode("register");
  } catch (_) {}
}

$("#login-tab").addEventListener("click", () => setMode("login"));
$("#register-tab").addEventListener("click", () => setMode("register"));
$("#password-visibility").addEventListener("click", event => {
  const visible = $("#password").type === "text";
  $("#password").type = visible ? "password" : "text";
  $("#confirm-password").type = visible ? "password" : "text";
  event.currentTarget.setAttribute("aria-label", visible ? "Show password" : "Hide password");
  event.currentTarget.setAttribute("aria-pressed", String(!visible));
});
$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget;
  if (!form.reportValidity()) return;
  if (state.mode === "register" && $("#password").value !== $("#confirm-password").value) {
    $("#auth-error").textContent = "Passwords do not match.";
    $("#confirm-password").focus();
    return;
  }
  const button = form.querySelector("button[type=submit]"); button.disabled = true;
  $("#auth-error").textContent = "";
  try {
    const payload = Object.fromEntries(new FormData(form));
    const data = await api(`/api/auth/${state.mode}`, { method: "POST", body: JSON.stringify(payload) });
    state.token = data.token; localStorage.setItem("verifeye_token", data.token); showDashboard(data.user);
  } catch (error) { $("#auth-error").textContent = error.message; }
  finally { button.disabled = false; }
});


function showPage(page) {
  if (state.currentUser?.role === "guest") {
    showDashboard(state.currentUser);
    return;
  }
  $("#dashboard-page").classList.toggle("hidden", page !== "dashboard");
  $("#events-page").classList.toggle("hidden", page !== "events");
  $("#identities-page").classList.toggle("hidden", page !== "identities");
  $("#notifications-page").classList.toggle("hidden", page !== "notifications");
  $("#guest-cameras-page").classList.add("hidden");
  if (page !== "identities") clearIdentityImageUrls();
  document.querySelectorAll(".nav-link").forEach(button => button.classList.toggle("active", button.dataset.page === page));
  if (page === "dashboard") { loadCameras(); loadGuestAccount(); }
  else {
    stopAllStreams();
    stopCameraDashboardUpdates();
    if (page === "events") loadEventPage(); else if (page === "identities") loadIdentities(); else loadNotifications();
  }
}
document.querySelectorAll(".nav-link").forEach(button => button.addEventListener("click", () => { showPage(button.dataset.page); setBrandMenu(false); }));
$("#open-enrollment").addEventListener("click", () => $("#enrollment-panel").classList.toggle("hidden"));
$("#event-state-filter").addEventListener("change", loadEvents);
$("#event-camera-filter").addEventListener("change", loadEvents);
$("#clear-event-filters").addEventListener("click", () => {
  $("#event-state-filter").value = "";
  $("#event-camera-filter").value = "";
  loadEvents();
});
$("#delivery-channel").addEventListener("change", loadNotificationDeliveries);
$("#delivery-status").addEventListener("change", loadNotificationDeliveries);
$("#rule-identity-filter").addEventListener("change", renderNotificationRules);
$("#clear-notification-filters").addEventListener("click", event => {
  $("#rule-identity-filter").value = "";
  $("#delivery-channel").value = "";
  $("#delivery-status").value = "";
  renderNotificationRules();
  loadNotificationDeliveries();
  event.currentTarget.closest("details").open = false;
});

async function signOut() { try { await api("/api/auth/logout", { method: "POST" }); } finally { showAuth(); } }
$("#logout").addEventListener("click", signOut);
$("#guest-logout").addEventListener("click", signOut);
const photo = $("#photo"), zone = $("#drop-zone");
function resetEnrollment() {
  if ($("#preview").src.startsWith("blob:")) URL.revokeObjectURL($("#preview").src);
  state.file = null; photo.value = ""; $("#identity-name").value = ""; $("#preview").removeAttribute("src");
  zone.classList.remove("hidden"); $("#preview-wrap").classList.add("hidden"); $("#enroll-button").disabled = true;
}
function chooseFile(file) {
  if (!file) return;
  if (!['image/jpeg','image/png','image/webp'].includes(file.type) || file.size > 10 * 1024 * 1024) {
    $("#enroll-error").textContent = "Choose a JPEG, PNG, or WebP image up to 10 MB."; return;
  }
  if ($("#preview").src.startsWith("blob:")) URL.revokeObjectURL($("#preview").src);
  state.file = file; $("#preview").src = URL.createObjectURL(file);
  zone.classList.add("hidden"); $("#preview-wrap").classList.remove("hidden");
  $("#enroll-button").disabled = false; $("#enroll-error").textContent = ""; $("#success").classList.add("hidden");
}
zone.addEventListener("click", () => photo.click());
$("#change-photo").addEventListener("click", () => photo.click());
photo.addEventListener("change", () => chooseFile(photo.files[0]));
['dragenter','dragover'].forEach(name => zone.addEventListener(name, e => { e.preventDefault(); zone.classList.add('drag'); }));
['dragleave','drop'].forEach(name => zone.addEventListener(name, e => { e.preventDefault(); zone.classList.remove('drag'); }));
zone.addEventListener("drop", e => chooseFile(e.dataTransfer.files[0]));
$("#enroll-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!state.file || !event.currentTarget.reportValidity()) return;
  const identityName = $("#identity-name").value.trim();
  if (!identityName) { $("#enroll-error").textContent = "Enter a name for this identity."; return; }
  const button = $("#enroll-button"); button.disabled = true; button.querySelector("span").textContent = "Processing face…";
  const data = new FormData(); data.append("name", identityName); data.append("image", state.file);
  try { const result = await api("/api/enroll", { method: "POST", body: data }); $("#success-message").textContent = `${result.displayName} is ready for recognition.`; $("#success").classList.remove("hidden"); resetEnrollment(); await loadIdentities(); }
  catch (error) { $("#enroll-error").textContent = error.message; if (error.status === 401) showAuth(); }
  finally { button.disabled = !state.file; button.querySelector("span").textContent = "Create identity"; }
});

(async function restoreSession() {
  if (!state.token) { await refreshSetupStatus(); return; }
  try { showDashboard(await api("/api/auth/me")); } catch (_) { showAuth(); }
})();

function statusText(camera) { return {connecting:"Loading",live:"Live",offline:"Offline",authentication_failed:"Authentication failure",retrying:"Retrying",stopped:"Stopped"}[camera.connectionState] || camera.connectionState; }
function stopAllStreams() { for (const stop of state.streamStops.values()) stop(); state.streamStops.clear(); }
function stopCameraEventPolling() { clearInterval(state.cameraEventPoll); state.cameraEventPoll = null; }
function stopCameraDashboardUpdates() {
  state.cameraLoadController?.abort();
  state.cameraLoadController = null;
  state.cameraLoadGeneration++;
  stopCameraEventPolling();
}
window.addEventListener("pagehide", stopAllStreams);
function findBytes(bytes, needle, from=0) { for (let i=Math.max(0,from);i<=bytes.length-needle.length;i++) if (needle.every((b,j)=>bytes[i+j]===b)) return i; return -1; }

async function renderMjpeg(cameraId, image) {
  const controller = new AbortController();
  state.streamStops.get(cameraId)?.();
  const stop = () => { controller.abort(); image.removeAttribute("src"); };
  state.streamStops.set(cameraId, stop);
  try {
    const response = await fetch("/api/cameras/" + cameraId + "/stream", { headers: { Authorization: "Bearer " + state.token }, signal: controller.signal });
    if (!response.ok) throw new Error("Stream unavailable");
    const reader = response.body.getReader(); let buffer = new Uint8Array();
    while (!controller.signal.aborted) { const {value, done} = await reader.read(); if (done) break; const merged = new Uint8Array(buffer.length + value.length); merged.set(buffer); merged.set(value, buffer.length); buffer = merged;
      let start=findBytes(buffer,[0xff,0xd8]),end=findBytes(buffer,[0xff,0xd9],start+2); while(start>=0&&end>=0){const old=image.src;image.src=URL.createObjectURL(new Blob([buffer.slice(start,end+2)],{type:"image/jpeg"}));if(old.startsWith("blob:"))URL.revokeObjectURL(old);buffer=buffer.slice(end+2);start=findBytes(buffer,[0xff,0xd8]);end=findBytes(buffer,[0xff,0xd9],start+2);}
    }
  } catch (error) { if (error.name !== "AbortError") image.alt = "Stream unavailable"; }
  finally { if (state.streamStops.get(cameraId) === stop) state.streamStops.delete(cameraId); }
}

function renderWhep(camera, video, badge, previewToken = state.token) {
  state.streamStops.get(camera.id)?.();
  let reader = null, stopped = false;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    reader?.close(); reader = null;
    video.pause(); video.srcObject = null;
  };
  state.streamStops.set(camera.id, stop);
  if (!camera.previewUrl || !window.MediaMTXWebRTCReader) {
    badge.className = "stream-state state-offline"; badge.textContent = "Preview unavailable";
    return;
  }
  reader = new window.MediaMTXWebRTCReader({
    url: camera.previewUrl,
    token: previewToken,
    onTrack: event => {
      if (stopped) return;
      video.srcObject = event.streams[0] || new MediaStream([event.track]);
      video.play().catch(() => {});
      badge.className = "stream-state state-live"; badge.textContent = "Live";
    },
    onError: () => {
      if (stopped) return;
      badge.className = "stream-state state-retrying"; badge.textContent = "Reconnecting";
    },
  });
}

async function triggerTestRecognition(camera, button) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Starting…";
  $("#camera-error").textContent = "";
  let credential = null;
  try {
    credential = await api(`/api/cameras/${camera.id}/event-token`, { method: "POST" });
    const form = new FormData();
    form.append("sourceEventId", `manual-${crypto.randomUUID()}`);
    form.append("eventType", "manual_test");
    form.append("occurredAt", new Date().toISOString());
    form.append("metadata", JSON.stringify({ source: "dashboard", manualTest: true }));
    const event = await api(`/api/cameras/${camera.id}/events`, {
      method: "POST", body: form, headers: { "X-Camera-Event-Token": credential.token }
    });
    button.textContent = "Recognition started";
    setTimeout(() => { if (button.isConnected) { button.textContent = original; button.disabled = false; } }, 1800);
    return event;
  } catch (error) {
    $("#camera-error").textContent = error.message;
    button.textContent = original;
    button.disabled = false;
  } finally {
    if (credential) {
      try { await api(`/api/cameras/${camera.id}/event-tokens/${credential.id}`, { method: "DELETE" }); }
      catch (_) {}
    }
  }
}

async function loadCameras() {
  if (!state.token) return;
  state.cameraLoadController?.abort();
  const controller = new AbortController(), generation = ++state.cameraLoadGeneration;
  state.cameraLoadController = controller;
  stopAllStreams();
  try {
    const cameras = await api("/api/cameras", {signal: controller.signal});
    if (controller.signal.aborted || generation !== state.cameraLoadGeneration) return;
    updateCameraSystemStatus(cameras);
    const list = $("#camera-list");
    list.innerHTML = cameras.length ? "" : '<p class="empty">No cameras configured.</p>';
    for (const camera of cameras) {
      const card = document.createElement("article");
      card.className = "camera-card"; card.dataset.cameraId = camera.id;
      card.innerHTML = `<div class="camera-latest-event"><button class="camera-latest-link" type="button" disabled><span class="latest-event-label">Last event</span><span class="latest-event-copy"><strong>Checking for events…</strong><small></small></span><span class="latest-event-arrow" aria-hidden="true">→</span></button></div><div class="camera-video"><video aria-label="Live camera preview" autoplay muted playsinline></video><div class="stream-state state-${camera.connectionState}">${statusText(camera)}</div></div><div class="camera-meta"><div class="camera-identity"><strong></strong><small></small></div><details class="camera-menu"><summary><span aria-hidden="true">•••</span></summary><div class="camera-actions"><button class="recognize-test" type="button">Test recognition</button><button class="edit" type="button">Edit camera</button><button class="toggle" type="button"></button><button class="remove danger" type="button">Delete camera</button></div></details></div>`;
      card.querySelector(".camera-identity strong").textContent = camera.name;
      card.querySelector(".camera-identity small").textContent = camera.host;
      const menu = card.querySelector(".camera-menu");
      menu.querySelector("summary").setAttribute("aria-label", `Options for ${camera.name}`);
      menu.addEventListener("toggle", () => {
        if (menu.open) document.querySelectorAll(".camera-menu[open]").forEach(other => { if (other !== menu) other.open = false; });
      });
      const test = card.querySelector(".recognize-test");
      test.disabled = !camera.running;
      test.title = camera.running ? "Create a manual camera event" : "Start the camera before testing recognition";
      test.onclick = () => triggerTestRecognition(camera, test);
      card.querySelector(".edit").onclick = async () => {
        const name = prompt("Camera name", camera.name); if (name === null) return;
        const url = prompt("New RTSP URL (leave blank to keep the saved URL)", "");
        const change = {name}; if (url) change.url = url;
        await api(`/api/cameras/${camera.id}`, {method:"PATCH", body:JSON.stringify(change)}); loadCameras();
      };
      const toggle = card.querySelector(".toggle");
      toggle.textContent = camera.running ? "Turn off camera" : "Start camera";
      const setStreamRunning = async running => {
        toggle.disabled = true;
        try {
          if (!running) state.streamStops.get(camera.id)?.();
          await api(`/api/cameras/${camera.id}/${running ? "start" : "stop"}`, {method:"POST"});
          await loadCameras();
        } catch (error) { $("#camera-error").textContent = error.message; }
        finally { toggle.disabled = false; }
      };
      toggle.onclick = () => setStreamRunning(!camera.running);
      card.querySelector(".remove").onclick = async () => {
        await api(`/api/cameras/${camera.id}`, {method:"DELETE"}); loadCameras();
      };
      list.appendChild(card);
      if (camera.running && !state.streamStops.has(camera.id)) renderWhep(camera, card.querySelector("video"), card.querySelector(".stream-state"));
    }
    await refreshLatestCameraEvents(cameras, generation);
    if (generation === state.cameraLoadGeneration) {
      stopCameraEventPolling();
      state.cameraEventPoll = setInterval(() => refreshLatestCameraEvents(cameras, generation), 15000);
    }
  } catch (error) {
    if (error.name !== "AbortError" && generation === state.cameraLoadGeneration) $("#camera-error").textContent = error.message;
  } finally {
    if (state.cameraLoadController === controller) state.cameraLoadController = null;
  }
}

function updateCameraSystemStatus(cameras) {
  const indicator = $("#camera-status-indicator"), label = $("#camera-system-status");
  const running = cameras.filter(camera => camera.running).length;
  const allRunning = cameras.length > 0 && running === cameras.length;
  indicator.classList.toggle("status-on", allRunning);
  indicator.classList.toggle("status-off", !allRunning);
  if (!cameras.length) label.textContent = "No cameras configured";
  else if (allRunning) label.textContent = cameras.length === 1 ? "Camera on" : `All ${cameras.length} cameras on`;
  else if (!running) label.textContent = cameras.length === 1 ? "Camera off" : `All ${cameras.length} cameras off`;
  else label.textContent = `${running} of ${cameras.length} cameras on`;
}

function openCameraEvent(eventId) {
  history.pushState({}, "", `/?event=${encodeURIComponent(eventId)}`);
  $("#event-state-filter").value = "";
  $("#event-camera-filter").value = "";
  showPage("events");
}

async function refreshLatestCameraEvents(cameras, generation) {
  await Promise.all(cameras.map(async camera => {
    try {
      const query = new URLSearchParams({camera_id: String(camera.id), limit: "1", offset: "0"});
      const events = await api(`/api/camera-events?${query}`);
      if (generation !== state.cameraLoadGeneration) return;
      const card = document.querySelector(`.camera-card[data-camera-id="${camera.id}"]`);
      const button = card?.querySelector(".camera-latest-link");
      if (!button) return;
      const event = events[0];
      if (!event) {
        button.querySelector("strong").textContent = "No events recorded";
        button.querySelector("small").textContent = "";
        button.disabled = true;
        return;
      }
      button.querySelector("strong").textContent = eventTitleLabel(event.event_type);
      button.querySelector("small").textContent = `${eventStateLabel(event.state)} · ${formatDate(event.accepted_at)}`;
      button.disabled = false;
      button.onclick = () => openCameraEvent(event.id);
    } catch (_) {}
  }));
}

async function loadGuestCameras() {
  if (!state.token || state.currentUser?.role !== "guest") return;
  stopAllStreams();
  const list = $("#guest-camera-list");
  $("#guest-camera-error").textContent = "";
  try {
    const cameras = await api("/api/guest/cameras");
    list.innerHTML = cameras.length ? "" : '<p class="empty">No camera previews are available.</p>';
    for (const camera of cameras) {
      const card = document.createElement("article");
      card.className = "camera-card";
      card.dataset.cameraId = camera.id;
      card.innerHTML = `<div class="camera-video"><video aria-label="Live camera preview" autoplay muted playsinline></video><div class="stream-state state-${camera.connectionState}">${statusText(camera)}</div></div><div class="camera-meta"><div><strong></strong><small></small></div></div>`;
      card.querySelector("strong").textContent = camera.name;
      card.querySelector("small").textContent = camera.previewAvailable ? "Preview available" : "Preview unavailable";
      list.appendChild(card);
      if (camera.previewAvailable) {
        try {
          const authorization = await api(`/api/guest/cameras/${camera.id}/preview-authorization`, { method: "POST" });
          renderWhep({ ...camera, previewUrl: authorization.url }, card.querySelector("video"), card.querySelector(".stream-state"), authorization.token);
        } catch (_) {
          const badge = card.querySelector(".stream-state");
          badge.className = "stream-state state-offline";
          badge.textContent = "Preview unavailable";
        }
      }
    }
  } catch (error) {
    $("#guest-camera-error").textContent = error.message;
    if (error.status === 401) showAuth();
  }
}

async function loadGuestAccount() {
  if (state.currentUser?.role !== "admin") return;
  $("#guest-account-error").textContent = "";
  try {
    const result = await api("/api/admin/guest");
    $("#guest-account-status").textContent = result.configured ? "Configured" : "Not configured";
    $("#revoke-guest").disabled = !result.configured;
    if (result.guest) {
      $("#guest-display-name").value = result.guest.displayName;
      $("#guest-email").value = result.guest.email;
    }
  } catch (error) { $("#guest-account-error").textContent = error.message; }
}

$("#guest-account-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!event.currentTarget.reportValidity()) return;
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true; $("#guest-account-error").textContent = "";
  try {
    await api("/api/admin/guest", { method: "PUT", body: JSON.stringify({displayName: $("#guest-display-name").value, email: $("#guest-email").value, password: $("#guest-password").value}) });
    $("#guest-password").value = "";
    await loadGuestAccount();
  } catch (error) { $("#guest-account-error").textContent = error.message; }
  finally { button.disabled = false; }
});

$("#revoke-guest").addEventListener("click", async event => {
  event.currentTarget.disabled = true; $("#guest-account-error").textContent = "";
  try { await api("/api/admin/guest", { method: "DELETE" }); $("#guest-password").value = ""; await loadGuestAccount(); }
  catch (error) { $("#guest-account-error").textContent = error.message; event.currentTarget.disabled = false; }
});

function eventStateLabel(value) {
  return value ? value.charAt(0).toUpperCase() + value.slice(1).replaceAll("_", " ") : "—";
}

function eventTitleLabel(value) {
  return value === "onvif_motion" ? "MOTION" : eventStateLabel(value);
}

async function expandEvent(card, eventId, button) {
  const detail = card.querySelector(".event-detail");
  if (!detail.classList.contains("hidden")) {
    detail.classList.add("hidden"); button.textContent = "View details"; return;
  }
  button.disabled = true;
  try {
    const event = await api(`/api/camera-events/${eventId}`);
    detail.innerHTML = "";
    const facts = document.createElement("div"); facts.className = "event-detail-facts";
    const values = [
      ["Event type", event.event_type],
      ["Source ID", event.source_event_id],
      ["Occurred", formatDate(event.occurred_at)],
      ["Dispatch", event.dispatch?.state || "—"],
      ["Session", event.session?.session_state || "Not attached"],
      ["Stream mode", event.session?.stream_mode || "—"],
    ];
    for (const [label, value] of values) {
      const item = document.createElement("div");
      item.innerHTML = "<span></span><strong></strong>";
      item.querySelector("span").textContent = label; item.querySelector("strong").textContent = value;
      facts.appendChild(item);
    }
    detail.appendChild(facts);
    const error = event.error_message || event.session?.session_error_message || event.dispatch?.last_error_message;
    if (error) { const message = document.createElement("p"); message.className = "event-failure"; message.textContent = error; detail.appendChild(message); }
    const results = document.createElement("div"); results.className = "event-results";
    const screenshotsByResult = new Map();
    const unmatchedScreenshots = [];
    for (const shot of event.screenshots) {
      if (shot.result_id == null) {
        unmatchedScreenshots.push(shot);
        continue;
      }
      const resultScreenshots = screenshotsByResult.get(String(shot.result_id)) || [];
      resultScreenshots.push(shot);
      screenshotsByResult.set(String(shot.result_id), resultScreenshots);
    }
    const createScreenshotLink = shot => {
      const link = document.createElement("a");
      link.href = `/assets/image-viewer.html?id=${encodeURIComponent(shot.id)}`;
      link.target = "_blank"; link.rel = "noopener";
      link.textContent = `${eventStateLabel(shot.role)} image`;
      return link;
    };
    if (!event.results.length) results.innerHTML = '<p class="empty">No recognition results yet.</p>';
    for (const result of event.results) {
      const row = document.createElement("div"); row.className = "event-result";
      row.innerHTML = '<span class="outcome-badge"></span><strong></strong><span class="result-time"></span><span class="result-screenshots"></span>';
      row.querySelector(".outcome-badge").textContent = eventStateLabel(result.outcome);
      row.querySelector("strong").textContent = result.displayed_label || (result.outcome === "no_face" ? "No face detected" : result.error_message || "Recognition result");
      row.querySelector(".result-time").textContent = formatDate(result.capture_timestamp);
      const resultScreenshots = row.querySelector(".result-screenshots");
      for (const shot of screenshotsByResult.get(String(result.id)) || []) {
        resultScreenshots.appendChild(createScreenshotLink(shot));
      }
      screenshotsByResult.delete(String(result.id));
      results.appendChild(row);
    }
    for (const shots of screenshotsByResult.values()) unmatchedScreenshots.push(...shots);
    detail.appendChild(results);
    if (unmatchedScreenshots.length) {
      const images = document.createElement("div"); images.className = "event-screenshots";
      for (const shot of unmatchedScreenshots) {
        images.appendChild(createScreenshotLink(shot));
      }
      detail.appendChild(images);
    }
    detail.classList.remove("hidden"); button.textContent = "Hide details";
  } catch (error) { $("#event-error").textContent = error.message; }
  finally { button.disabled = false; }
}

async function loadEvents() {
  if (!state.token) return;
  const list = $("#event-list"), filter = $("#event-state-filter").value;
  const cameraFilter = $("#event-camera-filter").value;
  $("#event-error").textContent = "";
  try {
    const events = [];
    for (let offset = 0;; offset += 200) {
      const query = new URLSearchParams({limit: "200", offset: String(offset)});
      if (filter) query.set("state", filter);
      if (cameraFilter) query.set("camera_id", cameraFilter);
      const page = await api(`/api/camera-events?${query}`);
      events.push(...page);
      if (page.length < 200) break;
    }
    $("#event-count").textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
    list.innerHTML = events.length ? "" : '<p class="empty">No camera events recorded.</p>';
    for (const event of events) {
      const card = document.createElement("article"); card.className = "event-card";
      card.innerHTML = '<div class="event-summary"><div><span class="event-camera"></span><h2></h2><p></p></div><span class="event-state"></span><button class="ghost event-expand">View details</button></div><div class="event-detail hidden"></div>';
      card.querySelector(".event-camera").textContent = event.camera_name;
      card.querySelector("h2").textContent = eventTitleLabel(event.event_type);
      card.querySelector("p").textContent = `${formatDate(event.accepted_at)} · ${event.source_event_id}`;
      const badge = card.querySelector(".event-state");
      badge.className = `event-state event-state-${event.state}`; badge.textContent = eventStateLabel(event.state);
      const expand = card.querySelector(".event-expand");
      expand.onclick = () => expandEvent(card, event.id, expand);
      list.appendChild(card);
      if (String(event.id) === new URLSearchParams(location.search).get("event")) {
        expand.click();
        requestAnimationFrame(() => card.scrollIntoView({behavior: "smooth", block: "center"}));
      }
    }
  } catch (error) {
    $("#event-error").textContent = error.message;
    if (error.status === 401) showAuth();
  }
}

async function loadEventPage() {
  await loadEventCameraFilters();
  await loadEvents();
}

async function loadEventCameraFilters() {
  const select = $("#event-camera-filter"), selected = select.value;
  try {
    const cameras = await api("/api/cameras");
    select.innerHTML = '<option value="">All cameras</option>';
    for (const camera of cameras) {
      const option = document.createElement("option");
      option.value = String(camera.id);
      option.textContent = camera.name;
      select.appendChild(option);
    }
    if ([...select.options].some(option => option.value === selected)) select.value = selected;
  } catch (error) {
    $("#event-error").textContent = error.message;
    if (error.status === 401) showAuth();
  }
}

function clearIdentityImageUrls(){for(const url of state.identityImageUrls)URL.revokeObjectURL(url);state.identityImageUrls.clear();}
async function loadIdentityReference(link,embeddingId){
  if(link.dataset.loaded)return;link.dataset.loaded="true";
  try{const response=await fetch(`/api/embeddings/${embeddingId}/reference-image`,{headers:{Authorization:`Bearer ${state.token}`}});if(!response.ok){let message="Reference unavailable";try{message=(await response.json()).detail||message;}catch(_){}throw new Error(message);}const url=URL.createObjectURL(await response.blob());state.identityImageUrls.add(url);const image=document.createElement("img");image.src=url;image.alt="Enrollment reference with face boxes and landmarks";link.querySelector("span").replaceWith(image);}
  catch(error){link.querySelector("span").textContent=error.message;link.classList.add("reference-error");}
}
async function loadIdentities(){
  if(!state.token)return;const list=$("#identity-list"); $("#identity-error").textContent="";
  try{const identities=await api("/api/identities");clearIdentityImageUrls(); $("#identity-count").textContent=identities.length+(identities.length===1?" identity":" identities"); list.innerHTML=identities.length?"":"<p class=empty>No identities enrolled.</p>";
    for(const identity of identities){const card=document.createElement("article");card.className="identity-card";card.innerHTML="<div class=identity-summary><div class=identity-avatar></div><div><h2></h2><p class=identity-id></p></div><button class=delete-identity>Delete identity</button></div><div class=identity-facts><div><span>Face records</span><strong>"+identity.embeddings.length+"</strong></div><div><span>Created</span><strong>"+formatDate(identity.createdAt)+"</strong></div><div><span>Last updated</span><strong>"+formatDate(identity.updatedAt)+"</strong></div></div><details><summary>View biometric data</summary><div class=embedding-list></div></details>";
      card.querySelector(".identity-avatar").textContent=(identity.displayName||"?").trim().charAt(0).toUpperCase();card.querySelector("h2").textContent=identity.displayName;card.querySelector(".identity-id").textContent=identity.externalId;const records=card.querySelector(".embedding-list");if(!identity.embeddings.length)records.innerHTML="<p class=empty>No face records.</p>";
      for(const item of identity.embeddings){const row=document.createElement("div");row.className="embedding-row";row.innerHTML='<a class="embedding-reference" target="_blank" rel="noopener"><span class="reference-status">Loading reference…</span><small class="reference-key">Green face · Red crop · Blue landmarks</small></a><div><strong></strong><span></span></div><div><span>Detection</span><strong></strong></div><div><span>Vector</span><strong></strong></div>';const reference=row.querySelector(".embedding-reference");reference.href=`/assets/image-viewer.html?embedding=${encodeURIComponent(item.id)}`;reference.dataset.embeddingId=String(item.id);const parts=row.querySelectorAll(":scope > div");parts[0].querySelector("strong").textContent=item.metadata.original_name||item.sourcePath||"Unknown source";parts[0].querySelector("span").textContent=formatDate(item.createdAt);parts[1].querySelector("strong").textContent=item.detectionScore==null?"—":Math.round(item.detectionScore*100)+"%";parts[2].querySelector("strong").textContent=item.dimensions+"d";records.appendChild(row);}
      const details=card.querySelector("details");details.addEventListener("toggle",()=>{if(details.open)details.querySelectorAll(".embedding-reference").forEach(link=>loadIdentityReference(link,link.dataset.embeddingId));});
      const remove=card.querySelector(".delete-identity");remove.className="ghost danger delete-identity";remove.onclick=async()=>{if(!confirm("Delete "+identity.displayName+" and all enrolled face records? This cannot be undone."))return;try{await api("/api/identities/"+identity.id,{method:"DELETE"});loadIdentities();}catch(error){$("#identity-error").textContent=error.message;}};list.appendChild(card);}
  }catch(error){$("#identity-error").textContent=error.message;if(error.status===401)showAuth();}
}

let notificationSettings = null;

function notificationRuleCard(rule) {
  const card = document.createElement("details"); card.className = "notification-rule";
  const ruleTypes = {identity:{title:rule.identityName,subtitle:"When this identity is recognized",outcome:"recognized"},unknown_face:{title:"Unknown face",subtitle:"When any face in the full recognition session is not in the database",outcome:"unrecognized_face"},no_face:{title:"No face",subtitle:"When the full recognition session completes without detecting a face",outcome:"no_face"},system_error:{title:"System fallback",subtitle:"When recognition cannot complete because of a processing error",outcome:"processing_error"}};
  const ruleType = rule.ruleType, definition = ruleTypes[ruleType];
  card.innerHTML = `<summary class="rule-head"><h2></h2><span class="disclosure-chevron" aria-hidden="true"></span></summary><div class="rule-body"><p class="rule-subtitle"></p>
    <div class="rule-grid"><label>Email address<input class="rule-email" type="email" maxlength="320" placeholder="alerts@example.com"></label><label>Phone number<input class="rule-phone" inputmode="tel" maxlength="32" placeholder="+15551234567"></label>
    <div class="check-row channel-checks"><label><input class="email-enabled" type="checkbox"> Email enabled</label><label><input class="sms-enabled" type="checkbox"> SMS enabled</label></div><div class="check-row outcome-checks"></div><div class="camera-checks"></div></div>
    <p class="rule-feedback" role="status"></p><div class="rule-actions"><button type="button" class="ghost danger delete-rule">Delete rule</button><span class="rule-action-spacer"></span><button type="button" class="ghost test-email">Test email</button><button type="button" class="ghost test-sms">Test SMS</button><button type="button" class="ghost save-rule">Save rule</button></div></div>`;
  card.querySelector("h2").textContent = definition.title;
  card.querySelector(".rule-subtitle").textContent = definition.subtitle;
  card.querySelector(".rule-email").value = rule.emailAddress || ""; card.querySelector(".rule-phone").value = rule.phoneNumber || "";
  card.querySelector(".email-enabled").checked = rule.emailEnabled; card.querySelector(".sms-enabled").checked = rule.smsEnabled;
  const outcomeLabel=document.createElement("span");outcomeLabel.textContent=eventStateLabel(definition.outcome);card.querySelector(".outcome-checks").appendChild(outcomeLabel);
  const cameras = card.querySelector(".camera-checks");
  if (!notificationSettings.cameras.length) cameras.textContent = "All cameras (none configured yet)";
  for (const camera of notificationSettings.cameras) { const label=document.createElement("label"); label.innerHTML='<input type="checkbox"> <span></span>'; label.querySelector("input").value=camera.id; label.querySelector("input").checked=rule.cameraIds.includes(camera.id); label.querySelector("span").textContent=camera.name; cameras.appendChild(label); }
  const payload = () => ({identityId:rule.identityId,ruleType,emailAddress:card.querySelector(".rule-email").value,phoneNumber:card.querySelector(".rule-phone").value,emailEnabled:card.querySelector(".email-enabled").checked,smsEnabled:card.querySelector(".sms-enabled").checked,cameraIds:[...card.querySelectorAll(".camera-checks input:checked")].map(i=>Number(i.value)),version:rule.version});
  const feedback=card.querySelector(".rule-feedback"),saveButton=card.querySelector(".save-rule");
  const saveRule=async()=>{feedback.className="rule-feedback";feedback.textContent="Saving…";saveButton.disabled=true;try{await api(`/api/notification-rules/${rule.id}`,{method:"PUT",body:JSON.stringify(payload())});feedback.classList.add("success-text");feedback.textContent="Rule saved.";rule.version+=1;rule.emailAddress=card.querySelector(".rule-email").value;rule.phoneNumber=card.querySelector(".rule-phone").value;return true;}catch(error){feedback.classList.add("error-text");feedback.textContent=error.message;return false;}finally{saveButton.disabled=false;}};
  saveButton.onclick=saveRule;
  card.querySelector(".delete-rule").onclick=async()=>{if(!confirm(`Delete the ${definition.title} notification rule?`))return;try{await api(`/api/notification-rules/${rule.id}`,{method:"DELETE"});await loadNotifications();}catch(error){$("#notification-error").textContent=error.message;}};
  const test=async(channel,button)=>{button.disabled=true;feedback.className="rule-feedback";feedback.textContent=`Saving and queueing test ${channel}…`;try{if(!await saveRule())return;await api("/api/notification-tests",{method:"POST",body:JSON.stringify({ruleId:rule.id,channel})});feedback.classList.add("success-text");feedback.textContent=`Test ${channel} queued.`;await loadNotificationDeliveries();}catch(error){feedback.classList.add("error-text");feedback.textContent=error.message;}finally{button.disabled=false;}};
  const testEmail=card.querySelector(".test-email"),testSms=card.querySelector(".test-sms");
  testEmail.onclick=()=>test("email",testEmail);testSms.onclick=()=>test("sms",testSms);
  testEmail.title=notificationSettings.providers.email.ready?"Save this rule and queue a test email":"SMTP is not configured on the server";
  testSms.title=notificationSettings.providers.sms.ready?"Save this rule and queue a test SMS":"Twilio is not configured on the server";
  return card;
}

async function loadNotifications(){
  if(!state.token)return; $("#notification-error").textContent="";
  try{
    notificationSettings=await api("/api/notification-settings");
    renderNewRuleTargets();
    renderSmtpSettings();
    renderNotificationRuleFilters();
    renderNotificationRules();
    await loadNotificationDeliveries();
  }catch(error){$("#notification-error").textContent=error.message;if(error.status===401)showAuth();}
}

function renderSmtpSettings(){
  const smtp=notificationSettings.providers.email,stateLabel=$("#smtp-provider-state");
  stateLabel.textContent=smtp.ready?"Configured":"Not configured";stateLabel.classList.toggle("ready",smtp.ready);
  $("#smtp-host").value=smtp.host||"";$("#smtp-port").value=smtp.port||587;$("#smtp-username").value=smtp.username||"";$("#smtp-sender").value=smtp.sender||"";$("#smtp-tls-mode").value=smtp.tlsMode||"starttls";$("#smtp-password").value="";$("#smtp-clear-password").checked=false;
  $("#smtp-password").placeholder=smtp.passwordConfigured?"Saved — leave blank to keep":"Enter SMTP password if required";
}

function renderNotificationRuleFilters(){
  const select=$("#rule-identity-filter"),selected=select.value;select.innerHTML='<option value="">All identities</option>';
  for(const identity of notificationSettings.identities){const option=document.createElement("option");option.value=String(identity.id);option.textContent=identity.displayName;select.appendChild(option);}
  if([...select.options].some(option=>option.value===selected))select.value=selected;
}

function renderNotificationRules(){
  if(!notificationSettings)return;const selected=$("#rule-identity-filter").value;
  const rules=selected?notificationSettings.rules.filter(rule=>String(rule.identityId)===selected):notificationSettings.rules;
  const list=$("#notification-rules");list.innerHTML=rules.length?"":`<p class="empty">${selected?"No rules for this identity.":"No notification rules configured."}</p>`;
  rules.forEach(rule=>list.appendChild(notificationRuleCard(rule)));
}

$("#smtp-settings-form").addEventListener("submit",async event=>{
  event.preventDefault();if(!event.currentTarget.reportValidity())return;
  const button=event.currentTarget.querySelector("button[type=submit]"),feedback=$("#smtp-settings-feedback");button.disabled=true;feedback.className="rule-feedback";feedback.textContent="Saving…";
  try{const smtp=await api("/api/notification-settings/smtp",{method:"PUT",body:JSON.stringify({host:$("#smtp-host").value,port:Number($("#smtp-port").value),username:$("#smtp-username").value,password:$("#smtp-password").value||null,sender:$("#smtp-sender").value,tlsMode:$("#smtp-tls-mode").value,clearPassword:$("#smtp-clear-password").checked})});notificationSettings.providers.email=smtp;renderSmtpSettings();feedback.classList.add("success-text");feedback.textContent="SMTP settings saved.";}
  catch(error){feedback.classList.add("error-text");feedback.textContent=error.message;}
  finally{button.disabled=false;}
});

function renderNewRuleTargets(){
  if(!notificationSettings)return;
  const select=$("#new-rule-target");select.innerHTML="";
  for(const [value,label] of [["unknown_face","Unknown face (whole session)"],["no_face","No face (whole session)"],["system_error","System fallback (processing errors)"]]){const option=document.createElement("option");option.value=value;option.textContent=label;select.appendChild(option);}
  for(const identity of notificationSettings.identities){const option=document.createElement("option");option.value=`identity:${identity.id}`;option.textContent=identity.displayName;select.appendChild(option);}
  $("#add-notification-rule").disabled=false;
  $("#add-notification-rule").title="Create a notification rule";
}

$("#add-notification-rule").addEventListener("click",()=>{
  $("#notification-error").textContent="";
  if(!notificationSettings){$("#notification-error").textContent="Notification settings are still loading. Try again in a moment.";return;}
  renderNewRuleTargets();
  if(!$("#new-rule-target").options.length){$("#notification-error").textContent="No notification rule targets are available.";return;}
  $("#new-notification-rule").classList.remove("hidden");$("#new-rule-target").focus();
});
$("#cancel-notification-rule").addEventListener("click",()=>$("#new-notification-rule").classList.add("hidden"));
$("#create-notification-rule").addEventListener("click",async event=>{
  const target=$("#new-rule-target").value;if(!target)return;
  const isIdentity=target.startsWith("identity:"),ruleType=isIdentity?"identity":target,identityId=isIdentity?Number(target.split(":")[1]):null;
  const button=event.currentTarget;button.disabled=true;$("#notification-error").textContent="";
  try{await api("/api/notification-rules",{method:"POST",body:JSON.stringify({identityId,ruleType,emailAddress:"",phoneNumber:"",emailEnabled:false,smsEnabled:false,cameraIds:[]})});$("#new-notification-rule").classList.add("hidden");await loadNotifications();}
  catch(error){$("#notification-error").textContent=error.message;}
  finally{button.disabled=false;}
});

async function loadNotificationDeliveries(){
  if(!state.token)return;const query=new URLSearchParams({limit:"100"});const channel=$("#delivery-channel").value,status=$("#delivery-status").value;if(channel)query.set("channel",channel);if(status)query.set("status",status);
  try{const deliveries=await api(`/api/notification-deliveries?${query}`);const list=$("#notification-deliveries");list.innerHTML=deliveries.length?"":'<p class="empty">No deliveries yet.</p>';for(const delivery of deliveries){const row=document.createElement("article");row.className="delivery-row";row.innerHTML='<strong class="delivery-channel"></strong><div><strong class="delivery-destination"></strong><div class="delivery-meta"></div></div><span class="delivery-status"></span><time></time>';row.querySelector(".delivery-channel").textContent=delivery.channel.toUpperCase();row.querySelector(".delivery-destination").textContent=delivery.destination;const trace=delivery.providerMessageId?` · Trace ${delivery.providerMessageId}`:"";row.querySelector(".delivery-meta").textContent=`${eventStateLabel(delivery.outcome)} · ${delivery.identityName||delivery.cameraName||"Test"}${delivery.lastError?` · ${delivery.lastError}`:trace}`;const statusLabel=delivery.channel==="email"&&delivery.status==="sent"?"accepted":delivery.status;row.querySelector(".delivery-status").textContent=statusLabel;row.querySelector(".delivery-status").classList.add(delivery.status);row.querySelector("time").textContent=formatDate(delivery.updatedAt||delivery.createdAt);list.appendChild(row);}}catch(error){$("#notification-error").textContent=error.message;}
}
function formatDate(value){if(!value)return "—";const normalized=value.includes("T")?value:value.replace(" ","T")+"Z";return new Intl.DateTimeFormat(undefined,{dateStyle:"medium",timeStyle:"short"}).format(new Date(normalized));}

$("#camera-form").addEventListener("submit",async event=>{event.preventDefault();try{await api("/api/cameras",{method:"POST",body:JSON.stringify({name:$("#camera-name").value,url:$("#camera-url").value,enabled:true})});event.target.reset();loadCameras();}catch(error){$("#camera-error").textContent=error.message;}});
$("#discover-onvif").addEventListener("click", async event => {
  const panel = $("#onvif-panel"), devices = $("#onvif-devices"), button = event.currentTarget;
  panel.classList.remove("hidden"); devices.innerHTML = ""; button.disabled = true;
  $("#onvif-status").textContent = "Searching the LAN…";
  try {
    const found = await api("/api/onvif/discover", {method:"POST"});
    $("#onvif-status").textContent = found.length ? "Enter the camera credentials, then choose a stream." : "No ONVIF devices found.";
    for (const device of found) renderOnvifDevice(device, devices);
  } catch (error) { $("#onvif-status").textContent = error.message; }
  finally { button.disabled = false; }
});

function renderOnvifDevice(device, container) {
  const form = document.createElement("form"); form.className = "onvif-device";
  form.innerHTML = '<strong class="onvif-endpoint"></strong><label>Username<input name="username" autocomplete="username"></label><label>Password<input name="password" type="password" autocomplete="current-password"></label><button class="ghost find-onvif-streams" type="button">Find streams</button><div class="onvif-profile hidden"><label>Preview stream<select name="previewToken"></select></label><label>Recognition stream<select name="recognitionToken"></select></label><label>Camera name<input name="name" maxlength="100"></label><button class="primary add-onvif" type="button">Add and start camera</button></div><p class="error" role="alert"></p>';
  form.querySelector(".onvif-endpoint").textContent = device.endpoint;
  form.querySelector(".find-onvif-streams").addEventListener("click", async event => {
    const submit = event.currentTarget, error = form.querySelector(".error");
    submit.disabled = true; error.textContent = "";
    try {
      const credentials = {endpoint:device.endpoint, username:form.elements.username.value, password:form.elements.password.value};
      const profiles = await api("/api/onvif/profiles", {method:"POST", body:JSON.stringify(credentials)});
      if (!profiles.length) throw new Error("No RTSP media profiles found.");
      const preview = form.elements.previewToken, recognition = form.elements.recognitionToken;
      preview.innerHTML = ""; recognition.innerHTML = '<option value="">Use preview stream</option>';
      for (const profile of profiles) {
        for (const select of [preview, recognition]) { const option = document.createElement("option"); option.value = profile.token; option.textContent = profile.name || profile.token; select.appendChild(option); }
      }
      const substream = profiles.findIndex(profile => /sub\s*stream|substream/i.test(profile.name || profile.token));
      const mainstream = profiles.findIndex(profile => /main\s*stream|mainstream/i.test(profile.name || profile.token));
      preview.selectedIndex = substream >= 0 ? substream : 0;
      recognition.selectedIndex = mainstream >= 0 ? mainstream + 1 : 0;
      form.elements.name.value = (profiles[mainstream >= 0 ? mainstream : 0].name || "ONVIF camera").replace(/[_ ]?(main|sub)\s*stream/i, "");
      form.querySelector(".onvif-profile").classList.remove("hidden");
    } catch (problem) { error.textContent = problem.message; }
    finally { submit.disabled = false; }
  });
  form.querySelector(".add-onvif").addEventListener("click", async event => {
    const add = event.currentTarget, error = form.querySelector(".error"), values = Object.fromEntries(new FormData(form));
    if (!values.name.trim()) { error.textContent = "Camera name is required."; return; }
    add.disabled = true; error.textContent = "";
    try {
      await api("/api/onvif/import", {method:"POST", body:JSON.stringify({...values, endpoint:device.endpoint})});
      $("#onvif-status").textContent = "Camera added and stream started."; form.remove(); await loadCameras();
    } catch (problem) { error.textContent = problem.message; add.disabled = false; }
  });
  container.appendChild(form);
}
async function pollCameraStatus(){if(!state.token||state.currentUser?.role==="guest"||$("#dashboard-page").classList.contains("hidden"))return;try{for(const camera of await api("/api/cameras")){const card=document.querySelector(`[data-camera-id="${camera.id}"]`);if(!card)continue;const badge=card.querySelector(".stream-state");badge.className=`stream-state state-${camera.connectionState}`;badge.textContent=statusText(camera);}}catch(_){}}
setInterval(pollCameraStatus,5000);
