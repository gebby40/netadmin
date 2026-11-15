"use strict";

const main = document.getElementById("main");
const bar = document.getElementById("bar");

async function api(path, body) {
  const options = body === undefined
    ? { credentials: "same-origin" }
    : { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
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

function show(templateId) {
  const template = document.getElementById(templateId);
  main.replaceChildren(template.content.cloneNode(true));
}

function showError(message) {
  const el = document.getElementById("error");
  el.textContent = message;
  el.hidden = false;
}

function wireForm(onSubmit) {
  const form = document.getElementById("form");
  const button = form.querySelector("button[type=submit]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    document.getElementById("error").hidden = true;
    const values = Object.fromEntries(new FormData(form));
    button.disabled = true;
    try {
      await onSubmit(values);
    } catch (err) {
      showError(err.message);
    } finally {
      button.disabled = false;
    }
  });
}

function setupScreen() {
  bar.hidden = true;
  show("t-setup");
  wireForm(async ({ name, password, confirm }) => {
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
  wireForm(async ({ name, password }) => {
    if (!name.trim() || !password) throw new Error("Enter your name and password.");
    await api("/api/login", { name: name.trim(), password });
    await start();
  });
}

async function homeScreen(status) {
  const overview = await api("/api/overview");
  bar.hidden = false;
  document.getElementById("who-name").textContent = overview.user;
  show("t-home");
  document.getElementById("n-racks").textContent = overview.racks.length;
  document.getElementById("n-devices").textContent = overview.devices.length;
  document.getElementById("n-consoles").textContent = overview.consoles.length;
  document.getElementById("version").textContent = status.version;
}

async function start() {
  let status;
  try {
    status = await api("/api/status");
  } catch (err) {
    main.replaceChildren(Object.assign(document.createElement("p"), { className: "loading", textContent: err.message }));
    return;
  }
  if (status.needs_setup) return setupScreen();
  if (!status.user) return loginScreen();
  try {
    await homeScreen(status);
  } catch (err) {
    if (err.status === 401) return loginScreen();
    main.replaceChildren(Object.assign(document.createElement("p"), { className: "loading", textContent: err.message }));
  }
}

document.getElementById("sign-out").addEventListener("click", async () => {
  try { await api("/api/logout", {}); } catch (err) { /* signed out either way */ }
  await start();
});

start();
