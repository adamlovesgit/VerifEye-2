const $ = (selector) => document.querySelector(selector);
const state = { mode: "login", token: localStorage.getItem("verifeye_token"), file: null, streamStops: new Map(), currentUser: null, cameraLoadController: null, cameraLoadGeneration: 0 };

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
  state.mode = mode;
  const registering = mode === "register";
  $("#login-tab").classList.toggle("active", !registering);
  $("#register-tab").classList.toggle("active", registering);
  $("#login-tab").setAttribute("aria-selected", String(!registering));
  $("#register-tab").setAttribute("aria-selected", String(registering));
  $("#name-field").classList.toggle("hidden", !registering);
  $("#display-name").required = registering;
  $("#password").autocomplete = registering ? "new-password" : "current-password";
  $("#form-title").textContent = registering ? "Make it yours." : "Good to see you.";
  $("#form-subtitle").textContent = registering ? "Create your private, local account." : "Sign in to manage your identity.";
  $("#submit-label").textContent = registering ? "Create account" : "Sign in";
  $("#auth-error").textContent = "";
}

function showDashboard(user) {
  state.currentUser = user;
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#app-nav").classList.remove("hidden");
  $("#privacy-badge").classList.add("hidden");
  showPage("dashboard");
}

function showAuth() {
  stopAllStreams();
  state.token = null; localStorage.removeItem("verifeye_token");
  state.currentUser = null;
  $("#app-view").classList.add("hidden"); $("#auth-view").classList.remove("hidden");
  $("#app-nav").classList.add("hidden"); $("#privacy-badge").classList.remove("hidden");
}

$("#login-tab").addEventListener("click", () => setMode("login"));
$("#register-tab").addEventListener("click", () => setMode("register"));
$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const button = form.querySelector("button[type=submit]"); button.disabled = true;
  $("#auth-error").textContent = "";
  try {
    const data = await api(`/api/auth/${state.mode}`, { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form))) });
    state.token = data.token; localStorage.setItem("verifeye_token", data.token); showDashboard(data.user);
  } catch (error) { $("#auth-error").textContent = error.message; }
  finally { button.disabled = false; }
});


function showPage(page) {
  $("#dashboard-page").classList.toggle("hidden", page !== "dashboard");
  $("#events-page").classList.toggle("hidden", page !== "events");
  $("#identities-page").classList.toggle("hidden", page !== "identities");
  document.querySelectorAll(".nav-link").forEach(button => button.classList.toggle("active", button.dataset.page === page));
  if (page === "dashboard") loadCameras();
  else {
    stopAllStreams();
    if (page === "events") loadEvents(); else loadIdentities();
  }
}
document.querySelectorAll(".nav-link").forEach(button => button.addEventListener("click", () => showPage(button.dataset.page)));
$("#open-enrollment").addEventListener("click", () => $("#enrollment-panel").classList.toggle("hidden"));
$("#refresh-identities").addEventListener("click", loadIdentities);
$("#refresh-events").addEventListener("click", loadEvents);
$("#event-state-filter").addEventListener("change", loadEvents);
$("#open-events").addEventListener("click", () => showPage("events"));

$("#logout").addEventListener("click", async () => { try { await api("/api/auth/logout", { method: "POST" }); } finally { showAuth(); } });
const photo = $("#photo"), zone = $("#drop-zone");
function chooseFile(file) {
  if (!file) return;
  if (!['image/jpeg','image/png','image/webp'].includes(file.type) || file.size > 10 * 1024 * 1024) {
    $("#enroll-error").textContent = "Choose a JPEG, PNG, or WebP image up to 10 MB."; return;
  }
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
  event.preventDefault(); if (!state.file) return;
  const button = $("#enroll-button"); button.disabled = true; button.querySelector("span").textContent = "Processing face…";
  const data = new FormData(); data.append("image", state.file);
  try { await api("/api/enroll", { method: "POST", body: data }); $("#success").classList.remove("hidden"); await loadIdentities(); }
  catch (error) { $("#enroll-error").textContent = error.message; if (error.status === 401) showAuth(); }
  finally { button.disabled = false; button.querySelector("span").textContent = "Enroll this face"; }
});

(async function restoreSession() {
  if (!state.token) return;
  try { showDashboard(await api("/api/auth/me")); } catch (_) { showAuth(); }
})();

function statusText(camera) { return {connecting:"Loading",live:"Live",offline:"Offline",authentication_failed:"Authentication failure",retrying:"Retrying",stopped:"Stopped"}[camera.connectionState] || camera.connectionState; }
function stopAllStreams() { for (const stop of state.streamStops.values()) stop(); state.streamStops.clear(); }
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
    const list = $("#camera-list");
    list.innerHTML = cameras.length ? "" : '<p class="empty">No cameras configured.</p>';
    for (const camera of cameras) {
      const card = document.createElement("article");
      card.className = "camera-card"; card.dataset.cameraId = camera.id;
      card.innerHTML = `<div class="camera-video"><img alt="Live recognition"><div class="stream-state state-${camera.connectionState}">${statusText(camera)}</div>${camera.running?'<button class="stream-stop" type="button" aria-label="Turn off camera stream">Turn off</button>':""}</div><div class="camera-meta"><div><strong></strong><small></small></div><div class="camera-actions"><button class="ghost recognize-test">Test recognition</button><button class="ghost edit">Edit</button><button class="ghost toggle"></button><button class="ghost remove">Delete</button></div></div>`;
      card.querySelector("strong").textContent = camera.name;
      card.querySelector("small").textContent = camera.host;
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
      const toggle = card.querySelector(".toggle"), streamStop = card.querySelector(".stream-stop");
      toggle.textContent = camera.running ? "Turn off stream" : "Start stream";
      const setStreamEnabled = async enabled => {
        toggle.disabled = true; if (streamStop) streamStop.disabled = true;
        try {
          if (!enabled) state.streamStops.get(camera.id)?.();
          await api("/api/cameras/" + camera.id, {method:"PATCH", body:JSON.stringify({enabled})});
          loadCameras();
        } catch (error) { $("#camera-error").textContent = error.message; }
        finally { toggle.disabled = false; if (streamStop) streamStop.disabled = false; }
      };
      toggle.onclick = () => setStreamEnabled(!camera.running);
      if (streamStop) streamStop.onclick = () => setStreamEnabled(false);
      card.querySelector(".remove").onclick = async () => {
        await api(`/api/cameras/${camera.id}`, {method:"DELETE"}); loadCameras();
      };
      list.appendChild(card);
      if (camera.running && !state.streamStops.has(camera.id)) renderMjpeg(camera.id, card.querySelector("img"));
    }
  } catch (error) {
    if (error.name !== "AbortError" && generation === state.cameraLoadGeneration) $("#camera-error").textContent = error.message;
  } finally {
    if (state.cameraLoadController === controller) state.cameraLoadController = null;
  }
}

function eventStateLabel(value) {
  return value ? value.charAt(0).toUpperCase() + value.slice(1).replaceAll("_", " ") : "—";
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
    if (!event.results.length) results.innerHTML = '<p class="empty">No recognition results yet.</p>';
    for (const result of event.results) {
      const row = document.createElement("div"); row.className = "event-result";
      row.innerHTML = '<span class="outcome-badge"></span><strong></strong><span class="result-time"></span>';
      row.querySelector(".outcome-badge").textContent = eventStateLabel(result.outcome);
      row.querySelector("strong").textContent = result.displayed_label || (result.outcome === "no_face" ? "No face detected" : result.error_message || "Recognition result");
      row.querySelector(".result-time").textContent = formatDate(result.capture_timestamp);
      results.appendChild(row);
    }
    detail.appendChild(results);
    if (event.screenshots.length) {
      const images = document.createElement("div"); images.className = "event-screenshots";
      for (const shot of event.screenshots) {
        const link = document.createElement("a"); link.href = shot.contentPath; link.target = "_blank"; link.rel = "noopener";
        link.textContent = `${eventStateLabel(shot.role)} image`; images.appendChild(link);
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
  $("#event-error").textContent = "";
  try {
    const events = [];
    for (let offset = 0;; offset += 200) {
      const query = new URLSearchParams({limit: "200", offset: String(offset)});
      if (filter) query.set("state", filter);
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
      card.querySelector("h2").textContent = eventStateLabel(event.event_type);
      card.querySelector("p").textContent = `${formatDate(event.accepted_at)} · ${event.source_event_id}`;
      const badge = card.querySelector(".event-state");
      badge.className = `event-state event-state-${event.state}`; badge.textContent = eventStateLabel(event.state);
      const expand = card.querySelector(".event-expand");
      expand.onclick = () => expandEvent(card, event.id, expand);
      list.appendChild(card);
    }
  } catch (error) {
    $("#event-error").textContent = error.message;
    if (error.status === 401) showAuth();
  }
}

async function loadIdentities(){
  if(!state.token)return; const list=$("#identity-list"); $("#identity-error").textContent="";
  try{const identities=await api("/api/identities"); $("#identity-count").textContent=identities.length+(identities.length===1?" identity":" identities"); list.innerHTML=identities.length?"":"<p class=empty>No identities enrolled.</p>";
    for(const identity of identities){const card=document.createElement("article");card.className="identity-card";card.innerHTML="<div class=identity-summary><div class=identity-avatar></div><div><h2></h2><p class=identity-id></p></div><button class=delete-identity>Delete identity</button></div><div class=identity-facts><div><span>Face records</span><strong>"+identity.embeddings.length+"</strong></div><div><span>Created</span><strong>"+formatDate(identity.createdAt)+"</strong></div><div><span>Last updated</span><strong>"+formatDate(identity.updatedAt)+"</strong></div></div><details><summary>View biometric metadata</summary><div class=embedding-list></div></details>";
      card.querySelector(".identity-avatar").textContent=(identity.displayName||"?").trim().charAt(0).toUpperCase();card.querySelector("h2").textContent=identity.displayName;card.querySelector(".identity-id").textContent=identity.externalId;const records=card.querySelector(".embedding-list");if(!identity.embeddings.length)records.innerHTML="<p class=empty>No face records.</p>";
      for(const item of identity.embeddings){const row=document.createElement("div");row.className="embedding-row";row.innerHTML="<div><strong></strong><span></span></div><div><span>Detection</span><strong></strong></div><div><span>Vector</span><strong></strong></div>";const parts=row.querySelectorAll("div");parts[0].querySelector("strong").textContent=item.metadata.original_name||item.sourcePath||"Unknown source";parts[0].querySelector("span").textContent=formatDate(item.createdAt);parts[1].querySelector("strong").textContent=item.detectionScore==null?"—":Math.round(item.detectionScore*100)+"%";parts[2].querySelector("strong").textContent=item.dimensions+"d";records.appendChild(row);}
      const remove=card.querySelector(".delete-identity");remove.className="ghost danger delete-identity";remove.onclick=async()=>{if(!confirm("Delete "+identity.displayName+" and all enrolled face records? This cannot be undone."))return;try{await api("/api/identities/"+identity.id,{method:"DELETE"});loadIdentities();}catch(error){$("#identity-error").textContent=error.message;}};list.appendChild(card);}
  }catch(error){$("#identity-error").textContent=error.message;if(error.status===401)showAuth();}
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
async function pollCameraStatus(){if(!state.token||$("#dashboard-page").classList.contains("hidden"))return;try{for(const camera of await api("/api/cameras")){const card=document.querySelector(`[data-camera-id="${camera.id}"]`);if(!card)continue;const badge=card.querySelector(".stream-state");badge.className=`stream-state state-${camera.connectionState}`;badge.textContent=statusText(camera);}}catch(_){}}
setInterval(pollCameraStatus,5000);
