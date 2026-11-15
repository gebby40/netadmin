"use strict";

const main = document.getElementById("main");
const bar = document.getElementById("bar");
const $ = (id) => document.getElementById(id);

const state = { version: "", view: "racks", racks: [], unplaced: [], rackId: null, face: "front", selectedId: null, newFound: 0, live: {}, discovery: null, showIgnored: false, consoles: [], kinds: {}, kvms: [], kvmKinds: {}, openPort: null };

/* ---------- talking to the server ---------- */

async function api(path, body, method) {
  const options = { credentials: "same-origin" };
  if (method) options.method = method;
  if (body !== undefined) {
    options.method = method || "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    throw new Error("Can't reach rackmap. Check that it is running and try again.");
  }
  let data = {};
  try { data = await response.json(); } catch (err) { /* empty body */ }
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Something went wrong. Try again.";
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return data;
}

/* ---------- small DOM helpers ---------- */

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function show(templateId) {
  main.replaceChildren($(templateId).content.cloneNode(true));
}

function message(text) {
  main.replaceChildren(h("p", { class: "loading", text }));
}

function setError(id, text) {
  const el = $(id);
  el.textContent = text || "";
  el.hidden = !text;
}

const uText = (u) => `U${Number(u).toLocaleString("en", { maximumFractionDigits: 1 })}`;

function span(device) {
  const top = device.position_u + device.u_height - 1;
  return top > device.position_u ? `${uText(device.position_u)}–${uText(top)}` : uText(device.position_u);
}

/* ---------- account screens ---------- */

function wireAccountForm(onSubmit) {
  const form = $("form");
  const button = form.querySelector("button[type=submit]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setError("error", "");
    button.disabled = true;
    try {
      await onSubmit(Object.fromEntries(new FormData(form)));
    } catch (err) {
      setError("error", err.message);
    } finally {
      button.disabled = false;
    }
  });
}

function setupScreen() {
  bar.hidden = true;
  show("t-setup");
  wireAccountForm(async ({ name, password, confirm }) => {
    if (!name.trim()) throw new Error("Enter a name for the account.");
    if (password.length < 10) throw new Error("Use a password of at least 10 characters.");
    if (password !== confirm) throw new Error("The two passwords don't match.");
    await api("/api/setup", { name: name.trim(), password });
    await start();
  });
}

function loginScreen() {
  bar.hidden = true;
  show("t-login");
  wireAccountForm(async ({ name, password }) => {
    if (!name.trim() || !password) throw new Error("Enter your name and password.");
    await api("/api/login", { name: name.trim(), password });
    await start();
  });
}

/* ---------- rack view ---------- */

const currentRack = () => state.racks.find((r) => r.id === state.rackId) || null;

function findDevice(id) {
  for (const rack of state.racks) {
    const hit = rack.devices.find((d) => d.id === id);
    if (hit) return hit;
  }
  return state.unplaced.find((d) => d.id === id) || null;
}

async function reload() {
  const overview = await api("/api/overview");
  state.racks = overview.racks;
  state.unplaced = overview.unplaced;
  if (!state.racks.some((r) => r.id === state.rackId)) state.rackId = state.racks.length ? state.racks[0].id : null;
  if (state.selectedId !== null && !findDevice(state.selectedId)) state.selectedId = null;
  $("who-name").textContent = overview.user;
  state.newFound = overview.new_found || 0;
  state.consoles = overview.console_servers || [];
  state.kinds = overview.console_kinds || {};
  state.kvms = overview.kvms || [];
  state.kvmKinds = overview.kvm_kinds || {};
  renderNav();
  if (state.view === "racks" && $("rack-tabs")) render();
}

function renderNav() {
  $("nav-racks").toggleAttribute("aria-current", state.view === "racks");
  $("nav-discovery").toggleAttribute("aria-current", state.view === "discovery");
  if (state.view === "racks") $("nav-racks").setAttribute("aria-current", "page");
  if (state.view === "discovery") $("nav-discovery").setAttribute("aria-current", "page");
  $("nav-consoles").toggleAttribute("aria-current", state.view === "consoles");
  if (state.view === "consoles") $("nav-consoles").setAttribute("aria-current", "page");
  const badge = $("nav-badge");
  badge.hidden = state.newFound === 0;
  badge.textContent = state.newFound;
  badge.setAttribute("aria-label", `${state.newFound} new`);
}

function render() {
  renderTabs();
  const rack = currentRack();
  $("no-racks").hidden = state.racks.length > 0;
  $("workspace").hidden = state.racks.length === 0 && state.unplaced.length === 0;
  document.querySelector(".rack-col").hidden = !rack;
  if (rack) renderRack(rack);
  renderUnplaced();
  renderDetail();
  paintLive();
}

function renderTabs() {
  $("rack-tabs").replaceChildren(...state.racks.map((rack) => h("button", {
    type: "button", role: "tab", class: "tab", "aria-selected": rack.id === state.rackId ? "true" : "false",
    onclick: () => { state.rackId = rack.id; state.selectedId = null; render(); },
  }, rack.name)));
}

/* What a device looks like from the side of the rack being viewed. */
function deviceBody(device, viewedFace) {
  const kind = device.kind;
  const sameSide = device.face === viewedFace;
  if (!sameSide && !device.full_depth) return null; // half-depth, mounted on the other side
  const showRear = !sameSide;
  const wanted = showRear ? "rear" : "front";
  const hasImage = kind && (showRear ? kind.has_rear_image : kind.has_front_image);
  const drawn = () => Faceplate.draw(device, kind, { rear: showRear });
  if (!hasImage) return drawn();
  const img = h("img", { src: `/api/catalog/${encodeURIComponent(kind.slug)}/image/${wanted}`, alt: "", loading: "lazy", draggable: "false" });
  img.addEventListener("error", () => img.replaceWith(drawn()), { once: true });
  return img;
}

function renderRack(rack) {
  $("rack-name").textContent = rack.name;
  const used = rack.devices.reduce((n, d) => n + d.u_height, 0);
  $("rack-sub").textContent = `${rack.height_u}U · ${rack.devices.length} ${rack.devices.length === 1 ? "item" : "items"} · ${Number(used.toFixed(1))}U used`;
  $("face-front").setAttribute("aria-pressed", String(state.face === "front"));
  $("face-rear").setAttribute("aria-pressed", String(state.face === "rear"));

  const rackEl = $("rack");
  rackEl.style.setProperty("--units", rack.height_u);

  const units = [];
  for (let u = rack.height_u; u >= 1; u -= 1) units.push(h("li", {}, String(u)));
  $("units").replaceChildren(...units);

  const bay = $("bay");
  const slots = [];
  for (let u = rack.height_u; u >= 1; u -= 1) {
    slots.push(h("button", {
      type: "button", class: "slot", "aria-label": `Add equipment at ${uText(u)}`, title: `Add equipment at ${uText(u)}`,
      onclick: () => openDeviceDialog(null, { rack_id: rack.id, position_u: u, face: state.face }),
    }));
  }
  const blocks = [];
  const labels = [];
  for (const device of rack.devices) {
    const body = deviceBody(device, state.face);
    const place = (el) => {
      el.style.setProperty("--pos", device.position_u);
      el.style.setProperty("--h", device.u_height);
      return el;
    };
    const select = () => { state.selectedId = device.id; render(); };
    const where = `${span(device)}${device.face === "rear" ? ", rear" : ""}`;
    if (body) {
      blocks.push(place(h("button", {
        type: "button", class: `device${device.id === state.selectedId ? " selected" : ""}`,
        "aria-label": `${device.name}, ${where}`, title: device.name, onclick: select,
      }, body)));
    } else {
      blocks.push(place(h("button", {
        type: "button", class: `device other-side${device.id === state.selectedId ? " selected" : ""}`,
        "aria-label": `${device.name}, ${where}, mounted on the other side`, title: `${device.name} (other side)`, onclick: select,
      })));
    }
    labels.push(place(h("li", { class: device.id === state.selectedId ? "selected" : "" },
      h("button", { type: "button", class: "label", onclick: select },
        h("span", { class: "label-name" }, h("span", { class: "dot", "data-live": String(device.id), hidden: true }), device.name),
        h("span", { class: "label-meta", text: [where, device.ip].filter(Boolean).join(" · ") })))));
  }
  bay.replaceChildren(...slots, ...blocks);
  $("labels").replaceChildren(...labels);
}

function renderUnplaced() {
  $("unplaced-none").hidden = state.unplaced.length > 0;
  $("unplaced-list").replaceChildren(...state.unplaced.map((device) => h("li", {},
    h("button", { type: "button", class: `row-button${device.id === state.selectedId ? " selected" : ""}`, onclick: () => { state.selectedId = device.id; render(); } },
      h("span", { class: "label-name", text: device.name }),
      h("span", { class: "label-meta", text: [device.manufacturer, device.model].filter(Boolean).join(" ") || "No model recorded" })))));
}

/* ---------- live status ---------- */

let liveTimer = null;

function describeSince(seconds) {
  if (seconds < 90) return "just now";
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes`;
  if (seconds < 129600) return `${Math.round(seconds / 3600)} hours`;
  return `${Math.round(seconds / 86400)} days`;
}

function paintLive() {
  const live = state.live || {};
  const now = Date.now() / 1000;
  for (const dot of document.querySelectorAll("[data-live]")) {
    const entry = live[dot.dataset.live];
    dot.hidden = !entry;
    if (entry) {
      dot.className = entry.up ? "dot up" : "dot down";
      dot.title = entry.up ? "Answering on the network" : "Not answering";
    }
  }
  for (const line of document.querySelectorAll("[data-live-text]")) {
    const entry = live[line.dataset.liveText];
    if (!entry) { line.textContent = "Not checked yet"; continue; }
    const since = describeSince(now - entry.since);
    line.textContent = entry.up ? (since === "just now" ? "Answering" : `Answering (for ${since})`) : (since === "just now" ? "Not answering" : `Not answering (for ${since})`);
    line.className = entry.up ? "" : "down-text";
  }
}

async function refreshLive() {
  clearTimeout(liveTimer);
  if (state.view !== "racks") return;
  try {
    state.live = (await api("/api/live")).devices;
    paintLive();
  } catch (err) { /* shown as "not checked"; the next round tries again */ }
  liveTimer = setTimeout(refreshLive, 30000);
}

/* ---------- management controller ---------- */

let controllerSeq = 0;
let editingControllerDevice = null;

const POWER_LABELS = {
  on: { label: "Power on", confirm: null },
  shutdown: { label: "Shut down", confirm: "The controller asks the operating system to shut down cleanly. Everything running on it stops." },
  restart: { label: "Restart", confirm: "The server is restarted. If the controller can't ask the operating system first, this is like pressing the reset button." },
  off: { label: "Force off", confirm: "Power is cut immediately, like holding the power button. Anything unsaved on the server is lost." },
};

function controllerRows(device, info, status, note, problem) {
  const rows = [];
  if (status) {
    const on = status.power_state === "On";
    rows.push(h("p", { class: "power-line" },
      h("span", { class: on ? "pill on" : "pill off", text: on ? "Powered on" : status.power_state === "Off" ? "Powered off" : status.power_state }),
      status.health ? h("span", { class: status.health === "OK" ? "muted" : "down-text", text: `Health: ${status.health}` }) : null));
    rows.push(h("dl", { class: "facts" },
      fact("Reports as", [status.manufacturer, status.model].filter(Boolean).join(" ")),
      fact("Service tag", status.serial && h("span", { class: "mono", text: status.serial })),
      fact("Hardware", [status.processors, status.memory_gib ? `${Math.round(status.memory_gib)} GB memory` : ""].filter(Boolean).join(", ")),
      fact("Power draw", status.watts !== null && status.watts !== undefined ? `${status.watts} W` : ""),
      fact("Temperatures", status.temperatures.length ? status.temperatures.map((t) => `${t.name.replace(/^System Board /, "")} ${t.celsius}°C`).join(", ") : ""),
      fact("Power supplies", status.power_supplies.length ? status.power_supplies.map((p) => `${p.name.replace(/ Status$/, "")} ${p.health || "?"}`).join(", ") : "")));
  }
  if (note) rows.push(h("p", { class: "hint", text: note }));
  if (problem) rows.push(h("p", { class: "error", text: problem }));
  if (status) {
    const wanted = status.power_state === "On" ? ["shutdown", "restart", "off"] : ["on"];
    const buttons = wanted.filter((a) => status.actions.includes(a)).map((action) =>
      h("button", { type: "button", class: action === "off" ? "danger" : "secondary", onclick: () => sendPower(device, action) }, POWER_LABELS[action].label));
    if (buttons.length) rows.push(h("div", { class: "buttons" }, buttons));
  }
  const links = [];
  if (info && info.has_console) links.push(h("button", { type: "button", class: "secondary", onclick: () => { state.view = "consoles"; state.openPort = `c${device.id}`; showView(); } }, "Open text console"));
  if (info && info.kvm_url) links.push(h("a", { class: "button-link", href: info.kvm_url, target: "_blank", rel: "noopener noreferrer" }, "Open remote screen"));
  links.push(h("button", { type: "button", class: "secondary", onclick: () => openControllerDialog(device, info) }, "Edit controller"));
  rows.push(h("div", { class: "buttons" }, links));
  return rows;
}

function ago(seconds) {
  if (seconds < 90) return "a moment ago";
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes ago`;
  return `${Math.round(seconds / 3600)} hours ago`;
}

// The controller can take a long time to answer (half a minute on older ones), so the panel
// never waits for it: the buttons that don't need an answer appear at once, along with what the
// controller said last time, and the fresh answer replaces that when it arrives.
async function loadControllerPanel(device) {
  const seq = ++controllerSeq;
  const current = () => seq === controllerSeq && $("controller-panel") && state.selectedId === device.id;
  const show = (...args) => { if (current()) $("controller-panel").replaceChildren(h("h2", { text: "Management controller" }), ...controllerRows(device, ...args)); };
  const asking = "Asking the controller how the server is doing. Older controllers can take half a minute to answer.";
  let info = null;
  let status = null;
  try {
    info = (await api(`/api/devices/${device.id}/controller`)).controller;
  } catch (err) {
    return show(null, null, "", err.message);
  }
  show(info, null, asking, "");
  try {
    const kept = await api(`/api/devices/${device.id}/controller/status?last=true`);
    if (kept.known) status = kept;
  } catch { /* the fresh answer below reports any problem */ }
  if (status) show(info, status, `As of ${ago(status.age_seconds)}. Checking again…`, "");
  try {
    status = await api(`/api/devices/${device.id}/controller/status`);
    show(info, status, "", "");
  } catch (err) {
    show(info, status, status ? `As of ${ago(status.age_seconds)}.` : "", err.message || "The controller didn't answer.");
  }
}

async function sendPower(device, action) {
  const spec = POWER_LABELS[action];
  if (spec.confirm && !(await confirmDialog(`${spec.label}: “${device.name}”?`, spec.confirm, spec.label))) return;
  try {
    await api(`/api/devices/${device.id}/controller/power`, { action });
  } catch (err) {
    await failed(err);
    return;
  }
  const panel = $("controller-panel");
  if (panel) panel.replaceChildren(h("h2", { text: "Management controller" }), h("p", { class: "hint", text: `${spec.label} sent. Checking again in a few seconds…` }));
  setTimeout(() => { if (state.selectedId === device.id) loadControllerPanel(device); }, 6000);
}

function openControllerDialog(device, info) {
  editingControllerDevice = device;
  const form = $("controller-form");
  form.reset();
  $("controller-dialog-title").textContent = info ? `Controller for “${device.name}”` : `Connect the controller for “${device.name}”`;
  $("controller-save").textContent = info ? "Save" : "Connect";
  $("controller-delete").hidden = !info;
  $("controller-pw-note").hidden = !info;
  if (info) {
    for (const key of ["host", "username", "console_command", "kvm_url"]) form.elements[key].value = info[key];
  }
  form.dataset.editing = info ? "1" : "";
  setError("controller-error", "");
  $("controller-dialog").showModal();
  form.elements.host.focus();
}

function fact(label, value) {
  if (!value) return null;
  return [h("dt", { text: label }), h("dd", {}, value)];
}

function openConsoleTerminal(portId) {
  state.view = "consoles";
  state.openPort = portId;
  showView();
}

// What the details panel says about consoles. Three cases: this equipment is a console server
// (show what is plugged into it); a console server exists (offer its ports, right here); or
// there is none, and nothing is shown.
function consoleSection(device) {
  const own = state.consoles.find((server) => server.id === device.console_server_id);
  if (own) {
    const used = own.ports.filter((port) => port.device);
    return h("section", { class: "controller" },
      h("h2", { text: "Console server" }),
      h("dl", { class: "facts" },
        fact("Address", h("span", { class: "mono", text: own.host })),
        fact("Serial ports", `${own.port_count}, ${used.length} in use`)),
      used.length ? h("ul", { class: "port-links" }, used.map((port) => h("li", {},
        h("button", { type: "button", class: "link", onclick: () => openConsoleTerminal(port.id) }, `Port ${port.number}: ${port.device.name}`)))) : null,
      h("div", { class: "buttons" },
        h("button", { type: "button", class: "secondary", onclick: () => { state.view = "consoles"; showView(); } }, "All ports"),
        h("button", { type: "button", class: "secondary", onclick: () => openConsoleDialog(own) }, "Edit console server")));
  }
  if (!state.consoles.length) return "";
  const mine = device.console_ports || [];
  const choices = state.consoles.flatMap((server) => server.ports
    .filter((port) => !port.device || port.device.id === device.id)
    .map((port) => ({ id: port.id, text: `${server.name} · port ${port.number}${port.label && port.label !== device.name ? ` (${port.label})` : ""}` })));
  return h("section", { class: "controller" },
    h("h2", { text: "Console" }),
    portChoice("Console port", "device-console-port", choices, mine.length ? mine[0].id : null, `/api/devices/${device.id}/console-port`),
    mine.length ? h("div", { class: "buttons" }, mine.map((port) =>
      h("button", { type: "button", class: "primary", onclick: () => openConsoleTerminal(port.id) }, mine.length > 1 ? `Open console (port ${port.number})` : "Open console"))) : null);
}

// A dropdown of free ports that saves as soon as a choice is made.
function portChoice(label, id, choices, currentId, saveUrl) {
  const select = h("select", { id, "aria-label": label },
    h("option", { value: "" }, "Not connected"),
    ...choices.map((choice) => h("option", { value: String(choice.id) }, choice.text)));
  select.value = currentId === null ? "" : String(currentId);
  select.addEventListener("change", async () => {
    select.disabled = true;
    try {
      await api(saveUrl, { port_id: select.value ? Number(select.value) : null }, "PUT");
      await reload();
    } catch (err) { await failed(err); await reload(); }
  });
  return h("label", { class: "inline-choice" }, label, select);
}

function kvmPortName(kvm, number) {
  return kvm.port_count > 1 ? `${kvm.name} · port ${number}` : kvm.name;
}

function screenLink(url, text) {
  return h("a", { class: "button-link", href: url, target: "_blank", rel: "noopener noreferrer" }, text);
}

// KVMs work like console servers: this equipment is a KVM (show what is plugged into it), or a
// KVM exists (offer its free ports here), or there is none and nothing is shown.
function kvmSection(device) {
  const own = state.kvms.find((kvm) => kvm.id === device.kvm_id);
  if (own) {
    const used = own.ports.filter((port) => port.device);
    return h("section", { class: "controller" },
      h("h2", { text: "KVM" }),
      h("dl", { class: "facts" },
        fact("Kind", own.kind_name),
        fact("Web address", own.url ? h("span", { class: "mono", text: own.url.replace(/^https?:\/\//, "") }) : "Not on the network"),
        fact("Ports", `${own.port_count}, ${used.length} in use`)),
      used.length ? h("ul", { class: "port-links" }, used.map((port) => h("li", {},
        h("button", { type: "button", class: "link", onclick: () => { state.selectedId = port.device.id; render(); } },
          own.port_count > 1 ? `Port ${port.number}: ${port.device.name}` : port.device.name)))) : null,
      h("div", { class: "buttons" },
        own.url ? screenLink(own.url, "Open KVM page") : null,
        h("button", { type: "button", class: "secondary", onclick: () => openKvmDialog(device, own) }, "Edit KVM")));
  }
  if (!state.kvms.length) return "";
  const mine = device.kvm_ports || [];
  const choices = state.kvms.flatMap((kvm) => kvm.ports
    .filter((port) => !port.device || port.device.id === device.id)
    .map((port) => ({ id: port.id, text: kvmPortName(kvm, port.number) })));
  const linked = mine.filter((port) => port.url);
  return h("section", { class: "controller" },
    h("h2", { text: "Remote screen" }),
    portChoice("KVM port", "device-kvm-port", choices, mine.length ? mine[0].id : null, `/api/devices/${device.id}/kvm-port`),
    linked.length ? h("div", { class: "buttons" }, linked.map((port) => screenLink(port.url, "Open remote screen"))) : null,
    mine.length && !linked.length ? h("p", { class: "hint", text: "This KVM has no web address, so there is nothing to open from here." }) : null,
    linked.length && linked[0].port_count > 1 ? h("p", { class: "hint", text: `Opens ${linked[0].kvm_name}; choose port ${linked[0].number} there.` }) : null);
}

let editingKvm = null;
let kvmDevice = null;

function openKvmDialog(device, kvm) {
  editingKvm = kvm || null;
  kvmDevice = device;
  const form = $("kvm-form");
  form.reset();
  $("kvm-kind").replaceChildren(...Object.entries(state.kvmKinds).map(([value, label]) => h("option", { value }, label)));
  $("kvm-dialog-title").textContent = kvm ? `KVM “${kvm.name}”` : `Set up “${device.name}” as a KVM`;
  $("kvm-delete").hidden = !kvm;
  if (kvm) {
    for (const key of ["name", "kind", "url", "port_count"]) form.elements[key].value = kvm[key];
  } else {
    form.elements.name.value = device.name.slice(0, 80);
    const guess = /pi-?kvm/i.test(device.name) ? "pikvm" : /dell/i.test(`${device.name} ${device.manufacturer}`) ? "dell" : "other";
    form.elements.kind.value = guess;
    form.elements.port_count.value = guess === "pikvm" ? 1 : 8;
    if (device.ip) form.elements.url.value = `https://${device.ip}`;
  }
  setError("kvm-error", "");
  $("kvm-dialog").showModal();
  form.elements.name.focus();
}

$("kvm-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = { name: form.elements.name.value.trim(), kind: form.elements.kind.value, url: form.elements.url.value.trim(),
    port_count: Number(form.elements.port_count.value), device_id: kvmDevice.id };
  if (!body.name) return setError("kvm-error", "Give the KVM a name.");
  if (!(body.port_count >= 1 && body.port_count <= 64)) return setError("kvm-error", "A KVM has between 1 and 64 ports.");
  $("kvm-save").disabled = true;
  try {
    if (editingKvm) await api(`/api/kvms/${editingKvm.id}`, body, "PUT"); else await api("/api/kvms", body);
    $("kvm-dialog").close("");
    await reload();
  } catch (err) {
    setError("kvm-error", err.message);
  } finally {
    $("kvm-save").disabled = false;
  }
});

$("kvm-delete").addEventListener("click", async () => {
  const kvm = editingKvm;
  $("kvm-dialog").close("");
  if (!(await confirmDialog(`Remove the KVM set-up for “${kvm.name}”?`, "The equipment stays in the rack; rackmap forgets that it is a KVM and what was plugged into its ports.", "Remove KVM set-up"))) return;
  try {
    await api(`/api/kvms/${kvm.id}`, undefined, "DELETE");
    await reload();
  } catch (err) { await failed(err); }
});

// Set-up choices that only apply to some equipment, kept out of the way as plain links.
function roleLinks(device) {
  const links = [];
  const plain = device.console_server_id === null && device.kvm_id === null && !device.has_controller;
  if (!device.has_controller && device.console_server_id === null && device.kvm_id === null) {
    links.push(h("button", { type: "button", class: "link", onclick: () => openControllerDialog(device, null) }, "Set up a server controller (iDRAC, iLO…)"));
  }
  if (plain && !(device.console_ports || []).length) {
    links.push(h("button", { type: "button", class: "link", onclick: () => openConsoleDialog(null, device) }, "This is a console server"));
  }
  if (plain && !(device.kvm_ports || []).length) {
    links.push(h("button", { type: "button", class: "link", onclick: () => openKvmDialog(device, null) }, "This is a KVM"));
  }
  return links.length ? h("p", { class: "role-links" }, links) : "";
}

function renderDetail() {
  const panel = $("detail");
  const device = state.selectedId === null ? null : findDevice(state.selectedId);
  panel.hidden = !device;
  if (!device) { panel.replaceChildren(); return; }
  const rack = state.racks.find((r) => r.id === device.rack_id);
  const placed = rack && device.position_u !== null;
  const model = [device.manufacturer, device.model].filter(Boolean).join(" ");
  const size = device.u_height > 0 ? `${Number(device.u_height)}U, ${device.full_depth ? "full depth" : "half depth"}` : "Not rack-mounted";
  let adminLink = null;
  if (device.admin_url) {
    adminLink = h("a", { href: device.admin_url, target: "_blank", rel: "noopener noreferrer" }, device.admin_url.replace(/^https?:\/\//, ""));
  }
  panel.replaceChildren(
    h("div", { class: "detail-head" },
      h("h2", { text: "Equipment" }),
      h("button", { type: "button", class: "link", "aria-label": "Close details", onclick: () => { state.selectedId = null; render(); } }, "Close")),
    h("h1", { text: device.name }),
    h("dl", { class: "facts" },
      fact("Model", model || "Not recorded"),
      fact("Size", size),
      fact("Location", placed ? `${rack.name}, ${span(device)}, ${device.face}` : "Not in a rack"),
      fact("IP address", device.ip && h("span", { class: "mono", text: device.ip })),
      device.ip ? [h("dt", { text: "On the network" }), h("dd", { id: "live-line", "data-live-text": String(device.id) }, "Not checked yet")] : null,
      fact("MAC address", device.mac && h("span", { class: "mono", text: device.mac })),
      fact("Serial number", device.serial && h("span", { class: "mono", text: device.serial })),
      fact("Admin page", adminLink),
      fact("Notes", device.notes && h("span", { class: "notes", text: device.notes }))),
    consoleSection(device),
    kvmSection(device),
    device.has_controller ? h("section", { class: "controller", id: "controller-panel" }, h("h2", { text: "Management controller" }), h("p", { class: "hint", text: "Asking the controller…" })) : "",
    h("div", { class: "buttons" },
      h("button", { type: "button", class: "secondary", onclick: () => openDeviceDialog(device) }, "Edit"),
      placed ? h("button", { type: "button", class: "secondary", onclick: () => takeOut(device) }, "Take out of rack") : null,
      h("span", { class: "spacer" }),
      h("button", { type: "button", class: "danger", onclick: () => removeDevice(device) }, "Delete")),
    roleLinks(device));
  if (device.has_controller) loadControllerPanel(device);
}

async function takeOut(device) {
  try {
    await api(`/api/devices/${device.id}`, { rack_id: null, position_u: null }, "PATCH");
    await reload();
  } catch (err) { await failed(err); }
}

async function removeDevice(device) {
  const ok = await confirmDialog(`Delete “${device.name}”?`, "Its record is removed from rackmap. This can't be undone.", "Delete equipment");
  if (!ok) return;
  try {
    await api(`/api/devices/${device.id}`, undefined, "DELETE");
    state.selectedId = null;
    await reload();
  } catch (err) { await failed(err); }
}

async function failed(err) {
  if (err.status === 401) { await start(); return; }
  await confirmDialog("That didn't work", err.message, null);
}

/* ---------- dialogs ---------- */

function confirmDialog(title, text, actionLabel) {
  const dialog = $("confirm-dialog");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  const ok = $("confirm-ok");
  ok.hidden = !actionLabel;
  ok.textContent = actionLabel || "";
  dialog.querySelector("[data-close]").textContent = actionLabel ? "Cancel" : "Close";
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
  });
}

for (const button of document.querySelectorAll("dialog [data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close(""));
}

/* rack dialog */
let editingRack = null;

function openRackDialog(rack) {
  editingRack = rack || null;
  const form = $("rack-form");
  form.reset();
  $("rack-dialog-title").textContent = rack ? "Edit rack" : "Add rack";
  $("rack-save").textContent = rack ? "Save rack" : "Add rack";
  $("rack-delete").hidden = !rack;
  if (rack) {
    form.elements.name.value = rack.name;
    form.elements.height_u.value = rack.height_u;
    form.elements.notes.value = rack.notes;
  }
  setError("rack-error", "");
  $("rack-dialog").showModal();
  form.elements.name.focus();
}

$("rack-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = { name: form.elements.name.value.trim(), height_u: Number(form.elements.height_u.value), notes: form.elements.notes.value };
  if (!body.name) return setError("rack-error", "Give the rack a name.");
  if (!Number.isInteger(body.height_u) || body.height_u < 1 || body.height_u > 60) return setError("rack-error", "A rack is between 1 and 60 units tall.");
  $("rack-save").disabled = true;
  try {
    const saved = editingRack ? await api(`/api/racks/${editingRack.id}`, body, "PUT") : await api("/api/racks", body);
    state.rackId = saved.id;
    $("rack-dialog").close("");
    await reload();
  } catch (err) {
    setError("rack-error", err.message);
  } finally {
    $("rack-save").disabled = false;
  }
});

$("rack-delete").addEventListener("click", async () => {
  const rack = editingRack;
  $("rack-dialog").close("");
  const count = rack.devices.length;
  const text = count ? `Its ${count} ${count === 1 ? "item stays" : "items stay"} recorded and ${count === 1 ? "moves" : "move"} to “Not in a rack”.` : "The rack is empty.";
  if (!(await confirmDialog(`Delete “${rack.name}”?`, text, "Delete rack"))) return;
  try {
    await api(`/api/racks/${rack.id}`, undefined, "DELETE");
    await reload();
  } catch (err) { await failed(err); }
});

/* equipment dialog */
let editingDevice = null;
let chosenKind = null; // catalogue entry picked in the dialog
let modelMode = "search"; // search | chosen | custom
let searchTimer = null;
let searchSeq = 0;

function setModelMode(mode) {
  modelMode = mode;
  $("model-search").hidden = mode !== "search";
  $("model-chosen").hidden = mode !== "chosen";
  $("model-custom").hidden = mode !== "custom";
  if (mode === "chosen" && chosenKind) {
    $("chosen-name").textContent = `${chosenKind.manufacturer} ${chosenKind.model}`;
    $("chosen-meta").textContent = chosenKind.u_height > 0 ? `${Number(chosenKind.u_height)}U, ${chosenKind.full_depth ? "full depth" : "half depth"}` : "not rack-mounted";
  }
  refreshPositions();
}

function dialogSize() {
  const form = $("device-form");
  if (modelMode === "chosen" && chosenKind) return { u: chosenKind.u_height, full: chosenKind.full_depth };
  if (modelMode === "custom") return { u: Number(form.elements.u_height.value) || 0, full: form.elements.full_depth.value === "true" };
  if (editingDevice) return { u: editingDevice.u_height, full: editingDevice.full_depth };
  return { u: 1, full: true };
}

let positionSeq = 0;
async function refreshPositions(preferred) {
  const form = $("device-form");
  const rackId = form.elements.rack_id.value;
  const select = $("f-pos");
  const hint = $("f-pos-hint");
  const keep = preferred !== undefined ? String(preferred) : select.value;
  $("f-pos-label").hidden = !rackId;
  $("f-face").disabled = !rackId;
  hint.hidden = true;
  if (!rackId) { select.replaceChildren(); return; }
  const size = dialogSize();
  if (!(size.u > 0)) {
    select.replaceChildren();
    $("f-pos-label").hidden = true;
    hint.textContent = "This equipment has no rack height, so it can't be given a position. Choose “Not in a rack”, or set its height.";
    hint.hidden = false;
    return;
  }
  const seq = ++positionSeq;
  const query = new URLSearchParams({ u_height: size.u, face: form.elements.face.value, full_depth: size.full });
  if (editingDevice) query.set("ignore", editingDevice.id);
  let positions = [];
  try {
    positions = (await api(`/api/racks/${rackId}/free?${query}`)).positions;
  } catch (err) {
    if (seq === positionSeq) { hint.textContent = err.message; hint.hidden = false; }
    return;
  }
  if (seq !== positionSeq) return;
  select.replaceChildren(...positions.map((u) => {
    const top = u + size.u - 1;
    return h("option", { value: String(u) }, top > u ? `${uText(u)} (up to ${uText(top)})` : uText(u));
  }));
  if (!positions.length) {
    hint.textContent = "There's no free space this size on that side of the rack.";
    hint.hidden = false;
  } else if (positions.map(String).includes(keep)) {
    select.value = keep;
  }
}

function openDeviceDialog(device, preset) {
  editingDevice = device || null;
  chosenKind = device ? device.kind : null;
  const form = $("device-form");
  form.reset();
  $("device-dialog-title").textContent = device ? "Edit equipment" : "Add equipment";
  $("device-save").textContent = device ? "Save changes" : "Add equipment";
  $("model-q").value = "";
  $("model-results").replaceChildren();
  form.elements.name.placeholder = "What you call it";
  $("more").open = Boolean(device && (device.ip || device.mac || device.serial || device.admin_url || device.notes));
  $("f-rack").replaceChildren(
    h("option", { value: "" }, "Not in a rack"),
    ...state.racks.map((rack) => h("option", { value: String(rack.id) }, `${rack.name} (${rack.height_u}U)`)));
  const from = device || preset || {};
  form.elements.rack_id.value = from.rack_id ? String(from.rack_id) : (device ? "" : (state.rackId ? String(state.rackId) : ""));
  form.elements.face.value = from.face || state.face;
  if (device) {
    for (const key of ["name", "manufacturer", "model", "ip", "mac", "serial", "admin_url", "notes"]) form.elements[key].value = device[key];
    form.elements.u_height.value = device.u_height;
    form.elements.full_depth.value = String(device.full_depth);
  }
  if (!device && preset) {
    for (const key of ["name", "ip", "mac", "admin_url"]) if (preset[key]) form.elements[key].value = preset[key];
    if (preset.ip || preset.mac) $("more").open = true;
    if (preset.kind) chosenKind = preset.kind;
  }
  setError("device-error", "");
  modelMode = device ? (device.kind ? "chosen" : "custom") : (chosenKind ? "chosen" : "search");
  setModelMode(modelMode);
  if (!device && preset && preset.searchText && !chosenKind) {
    $("model-q").value = preset.searchText;
    $("model-q").dispatchEvent(new Event("input"));
  }
  refreshPositions(from.position_u !== null && from.position_u !== undefined ? from.position_u : undefined);
  $("device-dialog").showModal();
  (device ? form.elements.name : $("model-q")).focus();
}

function pickKind(kind) {
  chosenKind = kind;
  const form = $("device-form");
  if (!form.elements.name.value.trim()) form.elements.name.placeholder = `${kind.manufacturer} ${kind.model}`;
  setModelMode("chosen");
}

$("model-q").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const text = $("model-q").value.trim();
  const list = $("model-results");
  if (text.length < 2) { list.replaceChildren(); return; }
  searchTimer = setTimeout(async () => {
    const seq = ++searchSeq;
    let results;
    try {
      results = (await api(`/api/catalog?q=${encodeURIComponent(text)}`)).results;
    } catch (err) {
      if (seq === searchSeq) list.replaceChildren(h("li", { class: "hint", text: err.message }));
      return;
    }
    if (seq !== searchSeq) return;
    if (!results.length) {
      list.replaceChildren(h("li", { class: "hint", text: "Nothing matches. Try fewer words, or enter it by hand below." }));
      return;
    }
    list.replaceChildren(...results.map((kind) => h("li", {},
      h("button", { type: "button", class: "row-button", onclick: () => pickKind(kind) },
        h("span", { class: "label-name", text: `${kind.manufacturer} ${kind.model}` }),
        h("span", { class: "label-meta", text: [kind.u_height > 0 ? `${Number(kind.u_height)}U` : "not rack-mounted", kind.has_front_image ? "picture" : "drawn", kind.part_number].filter(Boolean).join(" · ") })))));
  }, 200);
});

$("model-manual").addEventListener("click", () => { chosenKind = null; setModelMode("custom"); $("device-form").elements.manufacturer.focus(); });
$("model-back").addEventListener("click", () => { setModelMode("search"); $("model-q").focus(); });
$("model-change").addEventListener("click", () => { chosenKind = null; setModelMode("search"); $("model-q").focus(); });
$("f-rack").addEventListener("change", () => refreshPositions());
$("f-face").addEventListener("change", () => refreshPositions());
$("device-form").elements.u_height.addEventListener("change", () => refreshPositions());
$("device-form").elements.full_depth.addEventListener("change", () => refreshPositions());

$("device-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const value = (key) => form.elements[key].value.trim();
  if (modelMode === "search") return setError("device-error", "Pick a model from the search results, or choose “enter it by hand”.");
  const body = { name: value("name"), ip: value("ip"), mac: value("mac"), serial: value("serial"), admin_url: value("admin_url"), notes: form.elements.notes.value };
  if (modelMode === "chosen") {
    body.type_slug = chosenKind.slug;
    body.u_height = chosenKind.u_height;
  } else {
    body.type_slug = null;
    body.manufacturer = value("manufacturer");
    body.model = value("model");
    body.u_height = Number(form.elements.u_height.value);
    body.full_depth = form.elements.full_depth.value === "true";
    if (!body.name && !body.manufacturer && !body.model) return setError("device-error", "Enter a name, or the maker and model.");
    if (!(body.u_height >= 0)) return setError("device-error", "Height must be a number of rack units (0 if it doesn't mount in a rack).");
  }
  const rackId = form.elements.rack_id.value;
  if (rackId) {
    if (!form.elements.position_u.value) return setError("device-error", "Choose a position in the rack, or set the rack to “Not in a rack”.");
    body.rack_id = Number(rackId);
    body.position_u = Number(form.elements.position_u.value);
    body.face = form.elements.face.value;
  } else {
    body.rack_id = null;
    body.position_u = null;
  }
  $("device-save").disabled = true;
  try {
    const saved = editingDevice ? await api(`/api/devices/${editingDevice.id}`, body, "PATCH") : await api("/api/devices", body);
    state.selectedId = saved.id;
    if (saved.rack_id) state.rackId = saved.rack_id;
    $("device-dialog").close("");
    if (state.view === "discovery") { await reload(); await loadDiscovery(); } else { await reload(); }
    setTimeout(refreshLive, 4000); // the server re-checks soon after equipment changes
  } catch (err) {
    setError("device-error", err.message);
  } finally {
    $("device-save").disabled = false;
  }
});

/* ---------- discovery ---------- */

let scanTimer = null;

function hostFacts(host) {
  const facts = [];
  if (host.vendor) facts.push(host.vendor);
  facts.push(host.looks_like);
  if (host.services.length) facts.push([...new Set(host.services)].join(", "));
  return facts.join(" · ");
}

function hostSays(host) {
  return [host.sys_descr, host.http_title && `Web page: “${host.http_title}”`].filter(Boolean).join(" · ");
}

async function addFromHost(host) {
  let kind = null;
  if (host.suggested_slug) {
    try { kind = await api(`/api/catalog/${encodeURIComponent(host.suggested_slug)}`); } catch (err) { kind = null; }
  }
  const ports = new Set(host.ports);
  const adminUrl = ports.has(443) ? `https://${host.ip}` : ports.has(80) ? `http://${host.ip}` : ports.has(8443) ? `https://${host.ip}:8443` : ports.has(8080) ? `http://${host.ip}:8080` : "";
  const realVendor = host.vendor && !host.vendor.startsWith("Private") ? host.vendor.split(" ")[0] : "";
  openDeviceDialog(null, { name: host.best_name, ip: host.ip, mac: host.mac, admin_url: adminUrl, kind, searchText: realVendor, rack_id: state.rackId, face: "front" });
}

function hostRow(host) {
  const title = host.best_name ? `${host.best_name}` : host.ip;
  const actions = [];
  if (host.device) {
    actions.push(h("button", { type: "button", class: "secondary", onclick: () => { state.view = "racks"; state.selectedId = host.device.id; const d = findDevice(host.device.id); if (d && d.rack_id) state.rackId = d.rack_id; showView(); } }, "Show in rack view"));
  } else if (host.ignored) {
    actions.push(h("button", { type: "button", class: "secondary", onclick: () => ignoreHost(host, false) }, "Stop ignoring"));
  } else {
    actions.push(h("button", { type: "button", class: "primary", onclick: () => addFromHost(host) }, "Add…"));
    actions.push(h("button", { type: "button", class: "secondary", onclick: () => ignoreHost(host, true) }, "Ignore"));
  }
  return h("li", { class: "host card" },
    h("div", { class: "host-main" },
      h("div", { class: "host-title" },
        h("strong", { text: title }),
        h("span", { class: "mono", text: host.best_name ? host.ip : "" }),
        host.mac ? h("span", { class: "mono muted", text: host.mac }) : null),
      h("p", { class: "host-facts", text: hostFacts(host) }),
      hostSays(host) ? h("p", { class: "hint host-says", text: hostSays(host) }) : null,
      host.device ? h("p", { class: "hint", text: `Recorded as “${host.device.name}”.` }) : null,
      !host.device && host.suggested_slug ? h("p", { class: "hint", text: "rackmap recognised the model; Add… will fill it in." }) : null),
    h("div", { class: "host-actions" }, actions));
}

async function ignoreHost(host, ignored) {
  try {
    await api(`/api/discovery/hosts/${host.id}/ignore`, { ignored });
    await reload();
    await loadDiscovery();
  } catch (err) { await failed(err); }
}

function renderDiscovery() {
  const view = state.discovery;
  if (!view || state.view !== "discovery") return;
  const job = view.job;
  const running = job.state === "running";
  $("scan-go").disabled = running;
  $("scan-go").textContent = running ? "Scanning…" : "Scan now";
  const progress = $("scan-progress");
  progress.hidden = !running;
  if (running) {
    progress.max = job.total;
    progress.value = job.done;
    $("scan-status").textContent = `${job.done} of ${job.total} addresses checked, ${job.found} answering`;
  } else if (job.state === "finished") {
    const seconds = Math.max(1, Math.round(job.finished - job.started));
    $("scan-status").textContent = `Last scan: ${job.found} of ${job.total} addresses answered (${seconds}s). ${job.note || ""}`.trim();
  } else if (job.state === "failed") {
    $("scan-status").textContent = job.note;
  } else {
    $("scan-status").textContent = "";
  }

  const fresh = view.hosts.filter((x) => !x.ignored && !x.device);
  const known = view.hosts.filter((x) => x.device);
  const ignored = view.hosts.filter((x) => x.ignored && !x.device);
  $("found-title").textContent = view.hosts.length ? `${fresh.length} new, ${known.length} already recorded` : "Nothing found yet";
  $("found-empty").hidden = view.hosts.length > 0;
  $("h-new").hidden = fresh.length === 0;
  $("h-known").hidden = known.length === 0;
  $("h-ignored").hidden = !(state.showIgnored && ignored.length);
  $("show-ignored").parentElement.hidden = ignored.length === 0;
  $("show-ignored").parentElement.lastChild.textContent = ` Show ignored (${ignored.length})`;
  $("hosts-new").replaceChildren(...fresh.map(hostRow));
  $("hosts-known").replaceChildren(...known.map(hostRow));
  $("hosts-ignored").replaceChildren(...(state.showIgnored ? ignored.map(hostRow) : []));
}

async function loadDiscovery(fillForm) {
  state.discovery = await api("/api/discovery");
  if (state.view !== "discovery") return;
  if (fillForm) {
    const form = $("scan-form");
    const settings = state.discovery.settings;
    form.elements.networks.value = settings.networks || state.discovery.suggested_networks.join("\n");
    form.elements.router.value = settings.router;
    $("community-note").hidden = !settings.community_set;
    $("router-box").open = Boolean(settings.router);
    const suggest = state.discovery.suggested_networks;
    $("net-suggest").hidden = suggest.length === 0;
    $("net-suggest").textContent = suggest.length ? `rackmap itself is on ${suggest.join(", ")}. One network per line; private ranges only.` : "";
  }
  renderDiscovery();
  clearTimeout(scanTimer);
  if (state.discovery.job.state === "running") {
    scanTimer = setTimeout(async () => {
      try { await loadDiscovery(); if (state.discovery.job.state !== "running") await reload(); } catch (err) { /* next click will report it */ }
    }, 1500);
  }
}

function discoveryScreen() {
  show("t-discovery");
  $("version").textContent = state.version;
  $("show-ignored").checked = state.showIgnored;
  $("show-ignored").addEventListener("change", (event) => { state.showIgnored = event.target.checked; renderDiscovery(); });
  $("scan-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    setError("scan-error", "");
    if (!form.elements.networks.value.trim()) return setError("scan-error", "Enter at least one network to scan, for example 192.168.1.0/24.");
    const body = { networks: form.elements.networks.value, router: form.elements.router.value.trim() };
    if (form.elements.community.value) body.community = form.elements.community.value;
    $("scan-go").disabled = true;
    try {
      await api("/api/discovery/settings", body, "PUT");
      await api("/api/discovery/scan", {});
      form.elements.community.value = "";
      await loadDiscovery(true);
    } catch (err) {
      $("scan-go").disabled = false;
      if (err.status === 401) return start();
      setError("scan-error", err.message);
    }
  });
}

/* ---------- consoles ---------- */

let terminal = null;
let fitAddon = null;
let socket = null;
let editingConsole = null;
let editingPort = null;

function allDevices() {
  return [...state.racks.flatMap((r) => r.devices), ...state.unplaced].sort((a, b) => a.name.localeCompare(b.name));
}

function findPort(portId) {
  for (const server of state.consoles) {
    const port = server.ports.find((p) => p.id === portId);
    if (port) return { server, port };
  }
  return null;
}

function portName(port) {
  return port.label || (port.device ? port.device.name : "");
}

function closeTerminal() {
  if (socket) { socket.onclose = null; socket.onmessage = null; try { socket.close(); } catch (err) { /* already closed */ } socket = null; }
  if (terminal) { terminal.dispose(); terminal = null; fitAddon = null; }
  state.openPort = null;
  const card = $("term-card");
  if (card) card.hidden = true;
}

function setTermStatus(text, canReconnect) {
  if (!$("term-status")) return;
  $("term-status").textContent = text;
  $("term-reconnect").hidden = !canReconnect;
}

function openTerminal(portId) {
  const isController = typeof portId === "string";
  let title;
  let path;
  if (isController) {
    const device = findDevice(Number(portId.slice(1)));
    if (!device) return;
    title = `${device.name} · text console`;
    path = `/ws/controller/${device.id}`;
  } else {
    const found = findPort(portId);
    if (!found) return;
    const name = portName(found.port);
    title = `${found.server.name} · port ${found.port.number}${name ? ` · ${name}` : ""}`;
    path = `/ws/console/${portId}`;
  }
  closeTerminal();
  state.openPort = portId;
  $("term-card").hidden = false;
  $("term-title").textContent = title;
  setTermStatus("Connecting…", false);
  terminal = new Terminal({ cursorBlink: true, convertEol: false, scrollback: 5000, fontSize: 14,
    fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace', theme: { background: "#0e1110", foreground: "#e8ece9", cursor: "#4fc3a6" } });
  fitAddon = new FitAddon.FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open($("term"));
  fitAddon.fit();
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}${path}`);
  ws.binaryType = "arraybuffer";
  socket = ws;
  const send = (message) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message)); };
  terminal.onData((data) => send({ t: "in", d: data }));
  terminal.onResize(({ cols, rows }) => send({ t: "size", cols, rows }));
  let ended = false;
  ws.onmessage = (event) => {
    if (typeof event.data !== "string") { terminal.write(new Uint8Array(event.data)); return; }
    const note = JSON.parse(event.data);
    if (note.state === "open") {
      setTermStatus("Connected. Press Enter if the screen is blank.", false);
      send({ t: "size", cols: terminal.cols, rows: terminal.rows });
      terminal.focus();
    } else if (note.state === "failed" || note.state === "closed") {
      ended = true;
      setTermStatus(note.message || "Disconnected.", true);
    }
  };
  ws.onclose = (event) => {
    if (ended) return;
    setTermStatus(event.code === 4401 ? "Your sign-in has expired. Sign in again to use the console." : "The connection to rackmap was lost.", true);
  };
  $("term-card").scrollIntoView({ block: "start", behavior: "smooth" });
}

window.addEventListener("resize", () => { if (fitAddon && state.view === "consoles") { try { fitAddon.fit(); } catch (err) { /* not laid out yet */ } } });

function renderConsoles() {
  if (state.view !== "consoles" || !$("console-servers")) return;
  $("no-consoles").hidden = state.consoles.length > 0 || typeof state.openPort === "string";
  $("console-actions").hidden = state.consoles.length === 0;
  $("console-servers").replaceChildren(...state.consoles.map((server) => h("section", { class: "card console-server" },
    h("div", { class: "rack-head" },
      h("div", {},
        h("h1", { text: server.name }),
        h("p", { class: "hint", text: `${server.kind_name} · ${server.host}${server.port === 22 ? "" : `:${server.port}`} · ${server.port_count} ports` })),
      h("button", { type: "button", class: "link", onclick: () => openConsoleDialog(server) }, "Edit")),
    h("ul", { class: "ports" }, server.ports.map((port) => h("li", { class: port.id === state.openPort ? "port open" : "port" },
      h("button", { type: "button", class: "port-open", onclick: () => openTerminal(port.id), "aria-label": `Open port ${port.number}${portName(port) ? `, ${portName(port)}` : ""}` },
        h("span", { class: "port-number", text: String(port.number) }),
        h("span", { class: "port-label", text: portName(port) || "Not labelled" })),
      h("button", { type: "button", class: "link port-edit", onclick: () => openPortDialog(server, port), "aria-label": `Label port ${port.number}` }, "Label")))))));
}

async function loadConsoles() {
  const view = await api("/api/consoles");
  state.consoles = view.servers;
  state.kinds = view.kinds;
  renderConsoles();
}

function openConsoleDialog(server, forDevice) {
  // Started from a piece of equipment: if rackmap already knows one console server that isn't
  // tied to equipment yet, this is most likely it, so offer that one instead of a second copy.
  const loose = state.consoles.filter((s) => s.device_id === null);
  const note = $("console-existing-note");
  note.hidden = true;
  if (!server && forDevice && loose.length === 1) {
    server = loose[0];
    note.textContent = `rackmap already has the console server “${server.name}”, not yet tied to equipment. Saving ties it to “${forDevice.name}”.`;
    note.hidden = false;
  }
  editingConsole = server || null;
  const form = $("console-form");
  form.reset();
  $("console-device").replaceChildren(h("option", { value: "" }, "Not in the rack yet"),
    ...allDevices().map((device) => h("option", { value: String(device.id) }, device.name)));
  $("console-kind").replaceChildren(...Object.entries(state.kinds).map(([value, label]) => h("option", { value }, label)));
  $("console-dialog-title").textContent = server ? "Edit console server" : "Connect a console server";
  $("console-save").textContent = server ? "Save" : "Connect";
  $("console-delete").hidden = !server;
  $("console-pw-note").hidden = !server;
  if (server) {
    for (const key of ["name", "kind", "host", "port", "username", "port_count"]) form.elements[key].value = server[key];
    form.elements.device_id.value = server.device_id === null ? "" : String(server.device_id);
  }
  if (forDevice) {
    form.elements.device_id.value = String(forDevice.id);
    if (!server) {
      form.elements.name.value = forDevice.name.slice(0, 80);
      form.elements.host.value = forDevice.ip || "";
    }
  }
  setError("console-error", "");
  $("console-dialog").showModal();
  form.elements.name.focus();
}

$("console-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = { name: form.elements.name.value.trim(), kind: form.elements.kind.value, host: form.elements.host.value.trim(),
    port: Number(form.elements.port.value) || 22, username: form.elements.username.value.trim(), password: form.elements.password.value,
    port_count: Number(form.elements.port_count.value), device_id: form.elements.device_id.value ? Number(form.elements.device_id.value) : null };
  if (!body.name) return setError("console-error", "Give the console server a name.");
  if (!body.host) return setError("console-error", "Enter the console server's address.");
  if (!body.username) return setError("console-error", "Enter the account rackmap signs in with.");
  if (!editingConsole && !body.password) return setError("console-error", "Enter the password for that account.");
  if (!(body.port_count >= 1 && body.port_count <= 96)) return setError("console-error", "A console server has between 1 and 96 serial ports.");
  const button = $("console-save");
  button.disabled = true;
  button.textContent = "Signing in to check…";
  try {
    if (editingConsole) await api(`/api/consoles/${editingConsole.id}`, body, "PUT"); else await api("/api/consoles", body);
    $("console-dialog").close("");
    await reload();
    if (state.view === "consoles") await loadConsoles();
  } catch (err) {
    setError("console-error", err.message);
  } finally {
    button.disabled = false;
    button.textContent = editingConsole ? "Save" : "Connect";
  }
});

$("console-delete").addEventListener("click", async () => {
  const server = editingConsole;
  $("console-dialog").close("");
  if (!(await confirmDialog(`Remove “${server.name}”?`, "Its port labels and saved password are deleted from rackmap. The console server itself isn't changed.", "Remove console server"))) return;
  try {
    if (state.openPort !== null && server.ports.some((p) => p.id === state.openPort)) closeTerminal();
    await api(`/api/consoles/${server.id}`, undefined, "DELETE");
    await loadConsoles();
    await reload();
  } catch (err) { await failed(err); }
});

function openPortDialog(server, port) {
  editingPort = port;
  const form = $("port-form");
  form.reset();
  $("port-dialog-title").textContent = `${server.name} · port ${port.number}`;
  $("port-device").replaceChildren(h("option", { value: "" }, "Not tied to equipment"),
    ...allDevices().map((device) => h("option", { value: String(device.id) }, device.name)));
  form.elements.label.value = port.label;
  form.elements.device_id.value = port.device ? String(port.device.id) : "";
  setError("port-error", "");
  $("port-dialog").showModal();
  form.elements.label.focus();
}

$("port-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  $("port-save").disabled = true;
  try {
    await api(`/api/consoles/ports/${editingPort.id}`, { label: form.elements.label.value.trim(), device_id: form.elements.device_id.value ? Number(form.elements.device_id.value) : null }, "PUT");
    $("port-dialog").close("");
    await loadConsoles();
    await reload();
  } catch (err) {
    setError("port-error", err.message);
  } finally {
    $("port-save").disabled = false;
  }
});

function consolesScreen() {
  show("t-consoles");
  $("version").textContent = state.version;
  $("add-first-console").addEventListener("click", () => openConsoleDialog(null));
  $("add-console").addEventListener("click", () => openConsoleDialog(null));
  $("term-close").addEventListener("click", () => { closeTerminal(); renderConsoles(); });
  $("term-reconnect").addEventListener("click", () => { const id = state.openPort; if (id !== null) { openTerminal(id); renderConsoles(); } });
}

$("controller-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const device = editingControllerDevice;
  const body = { host: form.elements.host.value.trim(), username: form.elements.username.value.trim(), password: form.elements.password.value,
    console_command: form.elements.console_command.value.trim(), kvm_url: form.elements.kvm_url.value.trim() };
  if (!body.host) return setError("controller-error", "Enter the controller's address.");
  if (!body.username) return setError("controller-error", "Enter the account rackmap signs in with.");
  if (!form.dataset.editing && !body.password) return setError("controller-error", "Enter the password for that account.");
  const button = $("controller-save");
  button.disabled = true;
  button.textContent = "Signing in to check…";
  try {
    await api(`/api/devices/${device.id}/controller`, body, "PUT");
    $("controller-dialog").close("");
    state.selectedId = device.id;
    await reload();
  } catch (err) {
    setError("controller-error", err.message);
  } finally {
    button.disabled = false;
    button.textContent = form.dataset.editing ? "Save" : "Connect";
  }
});

$("controller-delete").addEventListener("click", async () => {
  const device = editingControllerDevice;
  $("controller-dialog").close("");
  if (!(await confirmDialog(`Disconnect the controller for “${device.name}”?`, "Its address and saved password are deleted from rackmap. The controller itself isn't changed.", "Disconnect controller"))) return;
  try {
    await api(`/api/devices/${device.id}/controller`, undefined, "DELETE");
    await reload();
  } catch (err) { await failed(err); }
});

/* ---------- start-up ---------- */

function racksScreen() {
  show("t-home");
  $("version").textContent = state.version;
  $("add-rack").addEventListener("click", () => openRackDialog(null));
  $("add-first-rack").addEventListener("click", () => openRackDialog(null));
  $("add-device").addEventListener("click", () => openDeviceDialog(null));
  $("edit-rack").addEventListener("click", () => openRackDialog(currentRack()));
  $("face-front").addEventListener("click", () => { state.face = "front"; render(); });
  $("face-rear").addEventListener("click", () => { state.face = "rear"; render(); });
}

async function showView() {
  bar.hidden = false;
  clearTimeout(scanTimer);
  clearTimeout(liveTimer);
  if (state.view !== "consoles" || !$("term-card")) { const keep = state.view === "consoles" ? state.openPort : null; closeTerminal(); state.openPort = keep; }
  renderNav();
  try {
    if (state.view === "discovery") {
      discoveryScreen();
      await loadDiscovery(true);
    } else if (state.view === "consoles") {
      const wanted = state.openPort;
      consolesScreen();
      await loadConsoles();
      if (wanted !== null) { openTerminal(wanted); renderConsoles(); }
    } else {
      racksScreen();
      render();
      refreshLive();
    }
  } catch (err) {
    if (err.status === 401) return start();
    message(err.message);
  }
}

$("nav-racks").addEventListener("click", () => { state.view = "racks"; showView(); });
$("nav-discovery").addEventListener("click", () => { state.view = "discovery"; showView(); });
$("nav-consoles").addEventListener("click", () => { if (state.view !== "consoles") { state.view = "consoles"; showView(); } });

async function start() {
  let status;
  try {
    status = await api("/api/status");
  } catch (err) {
    message(err.message);
    return;
  }
  state.version = status.version;
  if (status.needs_setup) return setupScreen();
  if (!status.user) return loginScreen();
  try {
    await reload();
  } catch (err) {
    if (err.status === 401) return loginScreen();
    message(err.message);
    return;
  }
  await showView();
}

$("sign-out").addEventListener("click", async () => {
  closeTerminal();
  try { await api("/api/logout", {}); } catch (err) { /* signed out either way */ }
  await start();
});

start();
