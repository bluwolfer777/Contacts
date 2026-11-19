"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = { me: null, items: [], tags: [], selectedTags: new Set(), editing: null, editTags: [] };

// ------------------------------------------------------------------ util
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function telHref(p) { return "tel:" + p.replace(/[^\d+]/g, ""); }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "X-Requested-With": "rubrica", ...(opts.body ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) },
    credentials: "same-origin",
  });
  if (res.status === 401) { location.href = "/"; throw new Error("Sessione scaduta"); }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg.map(d => (d.msg || "").replace(/^Value error, /, "")).join("; ");
    throw new Error(msg || `Errore ${res.status}`);
  }
  return data;
}

// ------------------------------------------------------------------ filtri
function filterParams() {
  const p = new URLSearchParams();
  const q = $("#q").value.trim();
  if (q) p.set("q", q);
  $$("[data-f]").forEach(i => { if (i.value.trim()) p.set(i.dataset.f, i.value.trim()); });
  state.selectedTags.forEach(t => p.append("tag", t));
  if ($("#untagged").checked) p.set("untagged", "true");
  return p;
}

function activeFilterCount() {
  let n = $$("[data-f]").filter(i => i.value.trim()).length + state.selectedTags.size;
  if ($("#untagged").checked) n++;
  return n;
}

async function load() {
  const p = filterParams();
  const qs = p.toString();
  $("#export").href = "/api/export.csv" + (qs ? "?" + qs : "");
  const n = activeFilterCount();
  $("#filter-badge").hidden = n === 0;
  $("#filter-badge").textContent = n;
  try {
    const [data, tags] = await Promise.all([api("/api/contacts?" + qs), api("/api/tags")]);
    state.items = data.items;
    state.tags = tags;
    render(data.total);
    renderTagFilter();
  } catch (e) {
    $("#status").textContent = e.message;
  }
}
const reload = debounce(load, 180);

function renderTagFilter() {
  const box = $("#tag-list");
  // mantiene selezionati anche tag che non esistono più per evitare filtri "fantasma"
  [...state.selectedTags].forEach(t => { if (!state.tags.some(x => x.name.toLowerCase() === t.toLowerCase())) state.selectedTags.delete(t); });
  box.innerHTML = state.tags.length
    ? state.tags.map(t => `<button type="button" class="chip ${state.selectedTags.has(t.name) ? "on" : ""}" data-tag="${esc(t.name)}">${esc(t.name)} <small>${t.count}</small></button>`).join("")
    : '<span class="muted small">Nessun tag</span>';
  $("#tag-suggest").innerHTML = state.tags.map(t => `<option value="${esc(t.name)}">`).join("");
}

function render(total) {
  const rows = $("#rows");
  const items = state.items;
  $("#status").textContent = items.length === total
    ? `${total} contatt${total === 1 ? "o" : "i"}`
    : `${items.length} di ${total} contatti`;
  $("#empty").hidden = items.length > 0;
  $("#table").hidden = items.length === 0;
  rows.innerHTML = items.map(c => `
    <tr data-id="${c.id}" tabindex="0">
      <td class="name" data-label="Nome">${esc(c.name)}</td>
      <td data-label="Telefono">${c.phones.map(p => `<a href="${esc(telHref(p))}">${esc(p)}</a>`).join("<br>") || '<span class="muted">—</span>'}</td>
      <td data-label="Email">${c.emails.map(m => `<a href="mailto:${esc(m)}">${esc(m)}</a>`).join("<br>") || '<span class="muted">—</span>'}</td>
      <td data-label="Tag">${c.tags.map(t => `<span class="chip sm" data-tag="${esc(t)}">${esc(t)}</span>`).join(" ")}</td>
      <td class="notes" data-label="Note">${esc(c.notes)}</td>
    </tr>`).join("");
}

// ------------------------------------------------------------------ dettaglio (sola lettura)
function openView(c) {
  $("#view-title").textContent = c.name;
  const list = (arr, fn) => arr.length ? arr.map(fn).join("") : '<li class="muted">—</li>';
  $("#view-body").innerHTML = `
    <h3>Telefono</h3><ul class="plain">${list(c.phones, p => `<li><a href="${esc(telHref(p))}">${esc(p)}</a></li>`)}</ul>
    <h3>Email</h3><ul class="plain">${list(c.emails, m => `<li><a href="mailto:${esc(m)}">${esc(m)}</a></li>`)}</ul>
    <h3>Tag</h3><div class="chips">${c.tags.map(t => `<span class="chip sm">${esc(t)}</span>`).join(" ") || '<span class="muted">—</span>'}</div>
    <h3>Note</h3><p class="pre">${esc(c.notes) || '<span class="muted">—</span>'}</p>`;
  $("#view").showModal();
}

// ------------------------------------------------------------------ editor
function multiRow(kind, value = "") {
  const type = kind === "emails" ? "email" : "tel";
  const ph = kind === "emails" ? "nome@azienda.it" : "+41 00 000 00 00";
  const row = document.createElement("div");
  row.className = "multi-row";
  row.innerHTML = `<input type="${type}" value="${esc(value)}" placeholder="${ph}" maxlength="${kind === "emails" ? 254 : 50}">
    <button type="button" class="icon" title="Rimuovi" aria-label="Rimuovi">×</button>`;
  row.querySelector("button").onclick = () => row.remove();
  return row;
}

function renderEditTags() {
  $("#tag-chips").innerHTML = state.editTags.map((t, i) =>
    `<span class="chip on">${esc(t)} <button type="button" data-rm="${i}" aria-label="Rimuovi tag">×</button></span>`).join("");
}

function addEditTag(raw) {
  raw.split(",").map(s => s.trim()).filter(Boolean).forEach(t => {
    if (!state.editTags.some(x => x.toLowerCase() === t.toLowerCase())) state.editTags.push(t.slice(0, 50));
  });
  renderEditTags();
}

function openEditor(c) {
  state.editing = c ? c.id : null;
  const f = $("#editor");
  f.reset();
  $("#dlg-title").textContent = c ? "Modifica contatto" : "Nuovo contatto";
  f.name.value = c ? c.name : "";
  f.notes.value = c ? c.notes : "";
  for (const kind of ["phones", "emails"]) {
    const box = $(`[data-list="${kind}"]`);
    box.innerHTML = "";
    const vals = c && c[kind].length ? c[kind] : [""];
    vals.forEach(v => box.appendChild(multiRow(kind, v)));
  }
  state.editTags = c ? [...c.tags] : [];
  renderEditTags();
  $("#tag-entry").value = "";
  $("#delete").hidden = !c;
  $("#dlg-error").hidden = true;
  $("#dlg-meta").textContent = c && c.updated_at
    ? `Ultima modifica: ${new Date(c.updated_at).toLocaleString("it-IT")}${c.updated_by ? " da " + c.updated_by : ""}` : "";
  $("#dlg").showModal();
  f.name.focus();
}

async function save(ev) {
  ev.preventDefault();
  const f = $("#editor");
  if ($("#tag-entry").value.trim()) addEditTag($("#tag-entry").value);
  const collect = kind => $$(`[data-list="${kind}"] input`).map(i => i.value.trim()).filter(Boolean);
  const body = { name: f.name.value.trim(), notes: f.notes.value, phones: collect("phones"), emails: collect("emails"), tags: state.editTags };
  if (!body.name) return showErr("Il nome è obbligatorio.");
  $("#save").disabled = true;
  try {
    if (state.editing) await api(`/api/contacts/${state.editing}`, { method: "PUT", body: JSON.stringify(body) });
    else await api("/api/contacts", { method: "POST", body: JSON.stringify(body) });
    $("#dlg").close();
    load();
  } catch (e) { showErr(e.message); }
  finally { $("#save").disabled = false; }
}

function showErr(msg) { const p = $("#dlg-error"); p.textContent = msg; p.hidden = false; }

async function remove() {
  const c = state.items.find(x => x.id === state.editing);
  if (!confirm(`Eliminare definitivamente "${c ? c.name : "questo contatto"}"?`)) return;
  try {
    await api(`/api/contacts/${state.editing}`, { method: "DELETE" });
    $("#dlg").close();
    load();
  } catch (e) { showErr(e.message); }
}

// ------------------------------------------------------------------ init
async function init() {
  try { state.me = await api("/api/me"); } catch { return; }
  $("#who-name").textContent = state.me.display_name;
  $("#who-role").textContent = state.me.is_admin ? "Modifica" : "Sola lettura";
  $("#who-role").classList.toggle("admin", state.me.is_admin);
  $("#new").hidden = !state.me.is_admin;

  $("#q").addEventListener("input", reload);
  $$("[data-f]").forEach(i => i.addEventListener("input", reload));
  $("#untagged").addEventListener("change", load);
  $("#toggle-filters").onclick = () => {
    const box = $("#filters");
    box.hidden = !box.hidden;
    $("#toggle-filters").setAttribute("aria-expanded", String(!box.hidden));
  };
  $("#clear-filters").onclick = () => {
    $$("[data-f]").forEach(i => (i.value = ""));
    $("#untagged").checked = false;
    state.selectedTags.clear();
    load();
  };
  $("#tag-list").addEventListener("click", e => {
    const b = e.target.closest("[data-tag]");
    if (!b) return;
    const t = b.dataset.tag;
    state.selectedTags.has(t) ? state.selectedTags.delete(t) : state.selectedTags.add(t);
    load();
  });

  const openRow = tr => {
    const c = state.items.find(x => x.id === Number(tr.dataset.id));
    if (c) state.me.is_admin ? openEditor(c) : openView(c);
  };
  $("#rows").addEventListener("click", e => {
    if (e.target.closest("a")) return;
    const chip = e.target.closest("[data-tag]");
    if (chip) { // clic su un tag nella tabella = filtra per quel tag
      state.selectedTags.add(chip.dataset.tag);
      $("#filters").hidden = false;
      load();
      return;
    }
    const tr = e.target.closest("tr[data-id]");
    if (tr) openRow(tr);
  });
  $("#rows").addEventListener("keydown", e => {
    const tr = e.target.closest("tr[data-id]");
    if (tr && e.key === "Enter") openRow(tr);
  });

  $("#new").onclick = () => openEditor(null);
  $$("[data-add]").forEach(b => (b.onclick = () => {
    const row = multiRow(b.dataset.add);
    $(`[data-list="${b.dataset.add}"]`).appendChild(row);
    row.querySelector("input").focus();
  }));
  $("#tag-entry").addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addEditTag(e.target.value);
      e.target.value = "";
    } else if (e.key === "Backspace" && !e.target.value && state.editTags.length) {
      state.editTags.pop();
      renderEditTags();
    }
  });
  $("#tag-entry").addEventListener("change", e => { // selezione dal datalist
    if (state.tags.some(t => t.name === e.target.value)) { addEditTag(e.target.value); e.target.value = ""; }
  });
  $("#tag-chips").addEventListener("click", e => {
    const b = e.target.closest("[data-rm]");
    if (b) { state.editTags.splice(Number(b.dataset.rm), 1); renderEditTags(); }
  });
  $("#editor").addEventListener("submit", save);
  $("#delete").onclick = remove;
  $$("[data-close]").forEach(b => (b.onclick = () => b.closest("dialog").close()));
  $$("dialog").forEach(d => d.addEventListener("click", e => { if (e.target === d) d.close(); }));
  document.addEventListener("keydown", e => {
    if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") {
      e.preventDefault();
      $("#q").focus();
    }
  });

  load();
}

init();
