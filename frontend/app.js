const $ = (selector) => document.querySelector(selector);
const state = { mode: "login", token: localStorage.getItem("verifeye_token"), file: null, streamStops: new Map() };

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
  $("#auth-view").classList.add("hidden");
  $("#enroll-view").classList.remove("hidden");
  $("#user-name").textContent = user.displayName.split(" ")[0];
  loadCameras();
}

function showAuth() {
  stopAllStreams();
  state.token = null; localStorage.removeItem("verifeye_token");
  $("#enroll-view").classList.add("hidden"); $("#auth-view").classList.remove("hidden");
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
  try { await api("/api/enroll", { method: "POST", body: data }); $("#success").classList.remove("hidden"); }
  catch (error) { $("#enroll-error").textContent = error.message; if (error.status === 401) showAuth(); }
  finally { button.disabled = false; button.querySelector("span").textContent = "Enroll this face"; }
});

(async function restoreSession() {
  if (!state.token) return;
  try { showDashboard(await api("/api/auth/me")); } catch (_) { showAuth(); }
})();

function statusText(camera) { return {connecting:"Loading",live:"Live",offline:"Offline",authentication_failed:"Authentication failure",retrying:"Retrying",stopped:"Stopped"}[camera.connectionState] || camera.connectionState; }
function stopAllStreams() { for (const stop of state.streamStops.values()) stop(); state.streamStops.clear(); }
function findBytes(bytes, needle, from=0) { for (let i=Math.max(0,from);i<=bytes.length-needle.length;i++) if (needle.every((b,j)=>bytes[i+j]===b)) return i; return -1; }

async function renderMjpeg(cameraId, image) {
  let cancelled=false; state.streamStops.set(cameraId,()=>{cancelled=true;});
  try {
    const response=await fetch(`/api/cameras/${cameraId}/stream`,{headers:{Authorization:`Bearer ${state.token}`}}); if(!response.ok)throw new Error("Stream unavailable");
    const reader=response.body.getReader(); let buffer=new Uint8Array();
    while(!cancelled){const {value,done}=await reader.read();if(done)break;const merged=new Uint8Array(buffer.length+value.length);merged.set(buffer);merged.set(value,buffer.length);buffer=merged;
      let start=findBytes(buffer,[0xff,0xd8]),end=findBytes(buffer,[0xff,0xd9],start+2);while(start>=0&&end>=0){const old=image.src;image.src=URL.createObjectURL(new Blob([buffer.slice(start,end+2)],{type:"image/jpeg"}));if(old.startsWith("blob:"))URL.revokeObjectURL(old);buffer=buffer.slice(end+2);start=findBytes(buffer,[0xff,0xd8]);end=findBytes(buffer,[0xff,0xd9],start+2);}}
  } catch(_){} finally{state.streamStops.delete(cameraId);}
}

async function loadCameras(){if(!state.token)return;try{const cameras=await api("/api/cameras"),list=$("#camera-list");list.innerHTML=cameras.length?"":'<p class="empty">No cameras configured.</p>';
  for(const camera of cameras){const card=document.createElement("article");card.className="camera-card";card.dataset.cameraId=camera.id;card.innerHTML=`<div class="camera-video"><img alt="Live recognition"><div class="stream-state state-${camera.connectionState}">${statusText(camera)}</div></div><div class="camera-meta"><div><strong></strong><small></small></div><div class="camera-actions"><button class="ghost edit">Edit</button><button class="ghost toggle"></button><button class="ghost remove">Delete</button></div></div>`;card.querySelector("strong").textContent=camera.name;card.querySelector("small").textContent=camera.host;card.querySelector(".edit").onclick=async()=>{const name=prompt("Camera name",camera.name);if(name===null)return;const url=prompt("New RTSP URL (leave blank to keep the saved URL)","");const change={name};if(url)change.url=url;await api(`/api/cameras/${camera.id}`,{method:"PATCH",body:JSON.stringify(change)});loadCameras();};const toggle=card.querySelector(".toggle");toggle.textContent=camera.enabled?"Disable":"Enable";toggle.onclick=async()=>{await api(`/api/cameras/${camera.id}`,{method:"PATCH",body:JSON.stringify({enabled:!camera.enabled})});loadCameras();};card.querySelector(".remove").onclick=async()=>{await api(`/api/cameras/${camera.id}`,{method:"DELETE"});loadCameras();};list.appendChild(card);if(camera.running&&!state.streamStops.has(camera.id))renderMjpeg(camera.id,card.querySelector("img"));}
}catch(error){$("#camera-error").textContent=error.message;}}

$("#camera-form").addEventListener("submit",async event=>{event.preventDefault();try{await api("/api/cameras",{method:"POST",body:JSON.stringify({name:$("#camera-name").value,url:$("#camera-url").value,enabled:true})});event.target.reset();loadCameras();}catch(error){$("#camera-error").textContent=error.message;}});
$("#discover-onvif").addEventListener("click",async()=>{const panel=$("#onvif-panel"),devices=$("#onvif-devices");panel.classList.remove("hidden");devices.innerHTML="";$("#onvif-status").textContent="Searching the LAN…";try{const found=await api("/api/onvif/discover",{method:"POST"});$("#onvif-status").textContent=found.length?"Select a discovered device.":"No ONVIF devices found.";for(const device of found){const button=document.createElement("button");button.className="ghost";button.textContent=device.endpoint;button.onclick=()=>importOnvif(device.endpoint);devices.appendChild(button);}}catch(error){$("#onvif-status").textContent=error.message;}});
async function importOnvif(endpoint){const username=prompt("ONVIF username");if(username===null)return;const password=prompt("ONVIF password");if(password===null)return;try{const profiles=await api("/api/onvif/profiles",{method:"POST",body:JSON.stringify({endpoint,username,password})});if(!profiles.length)throw new Error("No RTSP media profiles found.");const choices=profiles.map((p,i)=>`${i+1}: ${p.name}`).join("\n"),selected=Number(prompt(`Choose a media profile:\n${choices}`,"1"))-1;if(!profiles[selected])return;const name=prompt("Camera name",profiles[selected].name||"ONVIF camera");if(!name)return;await api("/api/onvif/import",{method:"POST",body:JSON.stringify({endpoint,username,password,token:profiles[selected].token,name})});$("#onvif-status").textContent="Camera imported and started.";loadCameras();}catch(error){$("#onvif-status").textContent=error.message;}}
async function pollCameraStatus(){if(!state.token||$("#enroll-view").classList.contains("hidden"))return;try{for(const camera of await api("/api/cameras")){const card=document.querySelector(`[data-camera-id="${camera.id}"]`);if(!card)continue;const badge=card.querySelector(".stream-state");badge.className=`stream-state state-${camera.connectionState}`;badge.textContent=statusText(camera);}}catch(_){}}
setInterval(pollCameraStatus,5000);
