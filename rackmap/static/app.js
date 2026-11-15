"use strict";

const main = document.getElementById("main");
const bar = document.getElementById("bar");
const $ = (id) => document.getElementById(id);

const state = { version: "", racks: [], unplaced: [], rackId: null, face: "front", selectedId: null };

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
  render();
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
        h("span", { class: "label-name", text: device.name }),
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

function fact(label, value) {
  if (!value) return null;
  return [h("dt", { text: label }), h("dd", {}, value)];
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
      fact("MAC address", device.mac && h("span", { class: "mono", text: device.mac })),
      fact("Serial number", device.serial && h("span", { class: "mono", text: device.serial })),
      fact("Admin page", adminLink),
      fact("Notes", device.notes && h("span", { class: "notes", text: device.notes }))),
    h("div", { class: "buttons" },
      h("button", { type: "button", class: "secondary", onclick: () => openDeviceDialog(device) }, "Edit"),
      placed ? h("button", { type: "button", class: "secondary", onclick: () => takeOut(device) }, "Take out of rack") : null,
      h("span", { class: "spacer" }),
      h("button", { type: "button", class: "danger", onclick: () => removeDevice(device) }, "Delete")));
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
  setError("device-error", "");
  modelMode = device ? (device.kind ? "chosen" : "custom") : "search";
  setModelMode(modelMode);
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
    await reload();
  } catch (err) {
    setError("device-error", err.message);
  } finally {
    $("device-save").disabled = false;
  }
});

/* ---------- start-up ---------- */

function homeScreen() {
  bar.hidden = false;
  show("t-home");
  $("version").textContent = state.version;
  $("add-rack").addEventListener("click", () => openRackDialog(null));
  $("add-first-rack").addEventListener("click", () => openRackDialog(null));
  $("add-device").addEventListener("click", () => openDeviceDialog(null));
  $("edit-rack").addEventListener("click", () => openRackDialog(currentRack()));
  $("face-front").addEventListener("click", () => { state.face = "front"; render(); });
  $("face-rear").addEventListener("click", () => { state.face = "rear"; render(); });
}

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
  homeScreen();
  try {
    await reload();
  } catch (err) {
    if (err.status === 401) return loginScreen();
    message(err.message);
  }
}

$("sign-out").addEventListener("click", async () => {
  try { await api("/api/logout", {}); } catch (err) { /* signed out either way */ }
  await start();
});

start();
