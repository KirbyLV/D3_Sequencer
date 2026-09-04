"use strict";

/* ---------------------------------------------------------------------- */
/* small utilities                                                        */
/* ---------------------------------------------------------------------- */

async function api(path, opts) {
  const resp = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  let body = null;
  try {
    body = await resp.json();
  } catch (e) {
    /* no body */
  }
  if (!resp.ok) {
    const msg = (body && (body.message || body.error)) || resp.statusText;
    const err = new Error(msg);
    err.body = body;
    throw err;
  }
  return body;
}

function el(tag, attrs, children) {
  const e = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([k, v]) => {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  });
  (children || []).forEach((c) => e.appendChild(c));
  return e;
}

function fmtJson(v) {
  return JSON.stringify(v, null, 2);
}

// Mapping identifiers aren't always numeric -- D3-reported filenames can use
// letter codes (e.g. "a"/"b"/"c") alongside plain numbers (see naming.py's
// module docstring). Numeric ones sort first in numeric order; everything
// else sorts after, alphabetically -- mirrors naming.py's _mapping_sort_key.
function compareMappingNos(a, b) {
  const na = Number(a);
  const nb = Number(b);
  const aIsNum = a.trim() !== "" && Number.isFinite(na);
  const bIsNum = b.trim() !== "" && Number.isFinite(nb);
  if (aIsNum && bIsNum) return na - nb;
  if (aIsNum !== bIsNum) return aIsNum ? -1 : 1;
  return String(a).localeCompare(String(b));
}

/* ---------------------------------------------------------------------- */
/* tabs                                                                    */
/* ---------------------------------------------------------------------- */

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

/* ---------------------------------------------------------------------- */
/* connection status                                                      */
/* ---------------------------------------------------------------------- */

document.getElementById("btnTestConn").addEventListener("click", testConnection);

async function testConnection() {
  const dot = document.getElementById("connDot");
  const label = document.getElementById("connLabel");
  label.textContent = "testing...";
  dot.className = "dot dot-unknown";
  try {
    await api("/api/test_connection", { method: "POST" });
    dot.className = "dot dot-ok";
    label.textContent = "connected";
  } catch (e) {
    dot.className = "dot dot-bad";
    label.textContent = e.message;
  }
}

/* ---------------------------------------------------------------------- */
/* setup tab: server settings                                             */
/* ---------------------------------------------------------------------- */

let currentConfig = null;

async function loadConfig() {
  currentConfig = await api("/api/config");
  document.getElementById("cfgScheme").value = currentConfig.server.scheme;
  document.getElementById("cfgHost").value = currentConfig.server.host;
  document.getElementById("cfgPort").value = currentConfig.server.port;
  document.getElementById("cfgTimeout").value = currentConfig.server.timeout_seconds;
  renderMappingTable(Object.keys(currentConfig.mapping_map || {}), currentConfig.mapping_map || {});
  renderExpressionEditors(currentConfig.expressions);
}

document.getElementById("btnSaveServer").addEventListener("click", async () => {
  await api("/api/config", {
    method: "POST",
    body: JSON.stringify({
      server: {
        scheme: document.getElementById("cfgScheme").value,
        host: document.getElementById("cfgHost").value.trim(),
        port: parseInt(document.getElementById("cfgPort").value, 10) || 80,
        timeout_seconds: parseFloat(document.getElementById("cfgTimeout").value) || 15,
      },
    }),
  });
  await loadConfig();
  testConnection();
});

/* ---------------------------------------------------------------------- */
/* setup tab: mapping number configuration                                */
/* ---------------------------------------------------------------------- */

// Each entry: {name, resource_path} -- resource_path is the stable
// str(projection.path) identifier that mapping_map values are stored as
// (see config.py); name is only for display.
let fetchedMappings = [];

function mappingDisplayName(resourcePath) {
  if (!resourcePath) return null;
  const found = fetchedMappings.find((m) => m.resource_path === resourcePath);
  return found ? found.name : resourcePath;
}

function renderMappingTable(mappingNumbers, mappingMap) {
  const tbody = document.getElementById("mappingTableBody");
  tbody.innerHTML = "";
  const nums = Array.from(new Set(mappingNumbers)).sort(compareMappingNos);
  nums.forEach((mn) => {
    // The input's value is always the resource_path (what gets saved);
    // once mappings have been fetched we show the friendly name instead
    // and stash the resource_path on the element itself.
    const savedPath = mappingMap[mn] || "";
    // Stale = this row points at a resource_path that isn't among the
    // mappings D3 just reported -- the usual reason is you're on a new
    // show and this row is still pointing at the old one's Projection.
    // Only flag once we've actually fetched at least once this session,
    // otherwise every row would look stale before you ever click Fetch.
    const isStale = Boolean(savedPath) && fetchedMappings.length > 0 && !fetchedMappings.some((m) => m.resource_path === savedPath);
    const input = el("input", {
      type: "text",
      value: mappingDisplayName(savedPath) || savedPath,
      "data-mapping-no": mn,
      "data-resource-path": savedPath,
      list: "fetchedMappingsList",
      class: isStale ? "stale" : "",
      title: isStale ? "Not found in the current show -- reassign this mapping" : "",
    });
    input.addEventListener("input", () => {
      // If what's typed matches a known mapping's name exactly, resolve it
      // to a resource_path immediately; otherwise treat the typed text as
      // the resource_path itself (lets you paste one directly).
      const match = fetchedMappings.find((m) => m.name === input.value);
      input.dataset.resourcePath = match ? match.resource_path : input.value;
      input.classList.remove("stale");
    });

    const removeBtn = el("button", {
      class: "btn-remove-row",
      type: "button",
      title: "Remove this mapping number",
      text: "×", // ×
      onclick: () => {
        const map = currentMappingMapFromRows();
        delete map[mn];
        renderMappingTable(Object.keys(map), map);
      },
    });

    const tr = el("tr", { "data-mapping-no": mn }, [el("td", { text: mn }), el("td", {}, [input]), el("td", {}, [removeBtn])]);

    // Drag-and-drop target: dropping a chip from "Fetch Mappings from D3"
    // onto this row assigns it, same as typing + picking a suggestion.
    tr.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
      tr.classList.add("drop-target");
    });
    tr.addEventListener("dragleave", () => tr.classList.remove("drop-target"));
    tr.addEventListener("drop", (ev) => {
      ev.preventDefault();
      tr.classList.remove("drop-target");
      const raw = ev.dataTransfer.getData("application/json");
      if (!raw) return;
      try {
        const dropped = JSON.parse(raw);
        input.value = dropped.name;
        input.dataset.resourcePath = dropped.resource_path;
      } catch (e) {
        /* ignore malformed drag payload */
      }
    });

    tbody.appendChild(tr);
  });
  if (!document.getElementById("fetchedMappingsList")) {
    const datalist = el("datalist", { id: "fetchedMappingsList" });
    document.body.appendChild(datalist);
  }
  refreshMappingDatalist();
  renderMappingChipSource();
}

function refreshMappingDatalist() {
  const datalist = document.getElementById("fetchedMappingsList");
  if (!datalist) return;
  datalist.innerHTML = "";
  fetchedMappings.forEach((m) => datalist.appendChild(el("option", { value: m.name })));
}

// Draggable chips for every mapping D3 reported, so a mapping can be
// dragged straight onto a table row instead of typed.
function renderMappingChipSource() {
  const container = document.getElementById("fetchedMappingsChips");
  if (!container) return;
  container.innerHTML = "";
  if (fetchedMappings.length === 0) {
    container.appendChild(
      el("span", { class: "hint-inline", text: "Click “Fetch Mappings from D3” to list mappings here." })
    );
    return;
  }
  fetchedMappings.forEach((m) => {
    const chip = el("span", { class: "drag-chip", draggable: "true" }, [
      el("span", { class: "grip", text: "⠇⠇" }),
      document.createTextNode(m.name),
    ]);
    chip.addEventListener("dragstart", (ev) => {
      ev.dataTransfer.effectAllowed = "copy";
      ev.dataTransfer.setData("application/json", JSON.stringify({ name: m.name, resource_path: m.resource_path }));
      ev.dataTransfer.setData("text/plain", m.name);
      chip.classList.add("dragging");
    });
    chip.addEventListener("dragend", () => chip.classList.remove("dragging"));
    container.appendChild(chip);
  });
}

function currentMappingMapFromRows() {
  const rows = Array.from(document.querySelectorAll("#mappingTableBody input[data-mapping-no]"));
  const map = {};
  rows.forEach((r) => (map[r.dataset.mappingNo] = r.dataset.resourcePath || ""));
  return map;
}

document.getElementById("btnAddMappingRow").addEventListener("click", () => {
  const input = document.getElementById("newMappingNo");
  const mn = input.value.trim();
  if (!mn) return;
  const map = currentMappingMapFromRows();
  const existing = new Set(Object.keys(map));
  existing.add(mn);
  renderMappingTable(Array.from(existing), map);
  input.value = "";
});

document.getElementById("btnScanMappingNumbers").addEventListener("click", async () => {
  const hint = document.getElementById("fetchedMappingsHint");
  hint.textContent = "Scanning media library...";
  try {
    const data = await api("/api/media");
    const map = currentMappingMapFromRows();
    const nums = new Set([...Object.keys(map), ...data.mapping_numbers_in_files]);
    renderMappingTable(Array.from(nums), map);
    hint.textContent = "Found mapping numbers: " + data.mapping_numbers_in_files.join(", ") || "none found.";
  } catch (e) {
    hint.textContent = "Error: " + e.message;
  }
});

document.getElementById("btnFetchMappings").addEventListener("click", async () => {
  const hint = document.getElementById("fetchedMappingsHint");
  hint.textContent = "Fetching mappings from D3...";
  try {
    const mappings = await api("/api/mappings");
    fetchedMappings = mappings.filter((m) => m.name && m.resource_path);
    // Re-render the table (and the draggable chip list / datalist) so any
    // already-saved resource_path values pick up their friendly display
    // name now that we know it.
    renderMappingTable(Object.keys(currentMappingMapFromRows()), currentMappingMapFromRows());
    hint.textContent =
      "From D3: " + (fetchedMappings.map((m) => m.name).join(", ") || "(none returned -- check the mapping_list expression below)");
  } catch (e) {
    hint.textContent = "Error (the mapping_list expression probably needs correcting -- try it in the Discovery Console): " + e.message;
  }
});

document.getElementById("btnSaveMappingMap").addEventListener("click", async () => {
  const map = currentMappingMapFromRows();
  currentConfig = await api("/api/config", { method: "POST", body: JSON.stringify({ mapping_map: map }) });
  document.getElementById("fetchedMappingsHint").textContent = "Saved.";
});

// New show: the mapping numbers are the same, but the disguise Projection
// each one pointed to lives in the OLD show and won't exist in this one.
// Clears the table (not saved until you click Save) so you can rebuild it
// against "Fetch Mappings from D3" for the new show.
document.getElementById("btnClearMappingMap").addEventListener("click", () => {
  if (!confirm("Clear all mapping rows? (Not saved until you click \"Save Mapping Configuration\".)")) return;
  renderMappingTable([], {});
  document.getElementById("fetchedMappingsHint").textContent = "Cleared -- click \"Fetch Mappings from D3\" to rebuild for this show, then Save.";
});

/* ---------------------------------------------------------------------- */
/* setup tab: expression templates                                        */
/* ---------------------------------------------------------------------- */

function renderExpressionEditors(expressions) {
  const container = document.getElementById("expressionEditors");
  container.innerHTML = "";
  Object.entries(expressions).forEach(([key, value]) => {
    const ta = el("textarea", { "data-expr-key": key, spellcheck: "false" });
    ta.value = value;
    container.appendChild(el("div", { class: "expr-editor" }, [el("label", { text: key }), ta]));
  });
}

document.getElementById("btnSaveExpressions").addEventListener("click", async () => {
  const areas = Array.from(document.querySelectorAll("#expressionEditors textarea[data-expr-key]"));
  const expressions = {};
  areas.forEach((a) => (expressions[a.dataset.exprKey] = a.value));
  currentConfig = await api("/api/config", { method: "POST", body: JSON.stringify({ expressions }) });
  alert("Expression templates saved.");
});

document.getElementById("btnResetExpressions").addEventListener("click", async () => {
  if (!confirm("Discard your saved expression templates and restore the app's built-in defaults?")) return;
  currentConfig = await api("/api/config/reset_expressions", { method: "POST" });
  renderExpressionEditors(currentConfig.expressions);
  alert("Restored the built-in expression templates.");
});

/* ---------------------------------------------------------------------- */
/* insert tab: media browser                                              */
/* ---------------------------------------------------------------------- */

let mediaData = null;
let selectedAssetId = null;
let selectedMappingNos = new Set();
let mediaSearchQuery = "";

document.getElementById("btnRefreshMedia").addEventListener("click", refreshMedia);
document.getElementById("mediaSearch").addEventListener("input", (ev) => {
  mediaSearchQuery = ev.target.value.trim().toLowerCase();
  if (mediaData) renderAssetGroups();
});

async function refreshMedia() {
  const container = document.getElementById("assetGroups");
  container.innerHTML = '<p class="hint">Loading...</p>';
  try {
    mediaData = await api("/api/media");
    renderAssetGroups();
  } catch (e) {
    container.innerHTML = '<p class="hint">Error loading media: ' + e.message + "</p>";
  }
}

// Matches on asset id, description, or any variant's filename -- covers
// the common ways someone would try to find an asset ("042", "cityscape",
// or the actual .mov name).
function assetMatchesSearch(g, assetId, query) {
  if (!query) return true;
  if (assetId.toLowerCase().includes(query)) return true;
  if ((g.description || "").toLowerCase().includes(query)) return true;
  return Object.values(g.variants || {}).some((variants) =>
    variants.some((v) => (v.filename || "").toLowerCase().includes(query))
  );
}

function renderAssetGroups() {
  const container = document.getElementById("assetGroups");
  container.innerHTML = "";
  const groups = mediaData.asset_groups || {};
  const ids = Object.keys(groups)
    .filter((assetId) => assetMatchesSearch(groups[assetId], assetId, mediaSearchQuery))
    .sort();
  if (ids.length === 0) {
    const msg = mediaSearchQuery ? "No assets match “" + mediaSearchQuery + "”." : "No assets matching the naming convention were found.";
    container.appendChild(el("p", { class: "hint", text: msg }));
  }
  ids.forEach((assetId) => {
    const g = groups[assetId];
    const card = el("div", { class: "asset-card", "data-asset-id": assetId });
    if (assetId === selectedAssetId) card.classList.add("selected");
    card.appendChild(
      el("div", { class: "title" }, [el("span", { class: "asset-id", text: "#" + assetId }), document.createTextNode(g.description)])
    );
    g.mapping_numbers.forEach((mn) => {
      const variants = g.variants[mn] || [];
      const latest = variants[0];
      const mappedPath = (mediaData.mapping_map || {})[mn];
      const mapped = mappedPath ? mappingDisplayName(mappedPath) : null;
      const chip = el("label", { class: "mapping-chip" + (mapped ? "" : " unmapped") });
      const cb = el("input", { type: "checkbox" });
      cb.checked = assetId === selectedAssetId ? selectedMappingNos.has(mn) : true;
      cb.addEventListener("change", (ev) => {
        ev.stopPropagation();
        selectAsset(assetId);
        if (cb.checked) selectedMappingNos.add(mn);
        else selectedMappingNos.delete(mn);
        updateSelectedSummary();
      });
      chip.appendChild(cb);
      chip.appendChild(document.createTextNode("mapping " + mn + (mapped ? " → " + mapped : " (unmapped!)")));
      card.appendChild(chip);
      card.appendChild(el("div", { class: "filename", text: latest ? latest.filename : "" }));
    });
    card.addEventListener("click", () => selectAsset(assetId));
    container.appendChild(card);
  });

  const unparsed = mediaData.unparsed_filenames || [];
  const details = document.getElementById("unparsedDetails");
  document.getElementById("unparsedCount").textContent = unparsed.length;
  const list = document.getElementById("unparsedList");
  list.innerHTML = "";
  unparsed.forEach((fn) => list.appendChild(el("li", { text: fn })));
  details.hidden = unparsed.length === 0;
}

function selectAsset(assetId) {
  if (selectedAssetId !== assetId) {
    selectedAssetId = assetId;
    const g = mediaData.asset_groups[assetId];
    selectedMappingNos = new Set(g.mapping_numbers);
    renderAssetGroups();
  }
  updateSelectedSummary();
}

function updateSelectedSummary() {
  const summary = document.getElementById("selectedAssetSummary");
  const btns = [document.getElementById("btnInsertLoop"), document.getElementById("btnInsertPause")];
  if (!selectedAssetId || selectedMappingNos.size === 0) {
    summary.textContent = "None selected. Click an asset card, then choose which mapping numbers to insert.";
    btns.forEach((b) => (b.disabled = true));
    return;
  }
  const g = mediaData.asset_groups[selectedAssetId];
  summary.textContent =
    "#" +
    selectedAssetId +
    " " +
    g.description +
    " — mapping(s): " +
    Array.from(selectedMappingNos).sort(compareMappingNos).join(", ");
  btns.forEach((b) => (b.disabled = false));
}

/* ---------------------------------------------------------------------- */
/* insert tab: tracks (REST-confirmed -- no "Timeline" level, see README) */
/* ---------------------------------------------------------------------- */

document.getElementById("btnRefreshTracks").addEventListener("click", refreshTracks);
document.getElementById("trackSelect").addEventListener("change", refreshTrackSections);

async function refreshTracks() {
  const trackSel = document.getElementById("trackSelect");
  trackSel.innerHTML = "";
  try {
    const tracks = await api("/api/tracks");
    tracks.forEach((t) => trackSel.appendChild(el("option", { value: t.name, text: t.name })));
    if (tracks.length) refreshTrackSections();
  } catch (e) {
    trackSel.appendChild(el("option", { value: "", text: "error: " + e.message }));
  }
}

async function refreshTrackSections() {
  const trackSel = document.getElementById("trackSelect");
  const hint = document.getElementById("trackSectionsHint");
  if (!trackSel.value) {
    hint.textContent = "";
    return;
  }
  hint.textContent = "Loading existing sections...";
  try {
    const sections = await api("/api/sections?track=" + encodeURIComponent(trackSel.value));
    if (sections.length === 0) {
      hint.textContent = "No sections on this track yet.";
    } else {
      hint.textContent =
        "Existing sections (seconds): " + sections.map((s) => s.time).sort((a, b) => a - b).join(", ");
    }
  } catch (e) {
    hint.textContent = "Could not load sections: " + e.message;
  }
}

/* ---------------------------------------------------------------------- */
/* insert tab: timecode (NTSC/frame-rate aware Start field)                */
/* ---------------------------------------------------------------------- */

let _timecodeLabel = "";

async function loadTimecodeInfo() {
  try {
    const info = await api("/api/timecode_info");
    _timecodeLabel = "Project timecode: " + info.label;
  } catch (e) {
    _timecodeLabel = "Could not read project timecode setting: " + e.message;
  }
  updateTimecodeHint();
}

let _timecodePreviewTimer = null;

document.getElementById("startTimecode").addEventListener("input", () => {
  clearTimeout(_timecodePreviewTimer);
  _timecodePreviewTimer = setTimeout(updateTimecodeHint, 300);
});
document.getElementById("lengthSeconds").addEventListener("input", () => {
  clearTimeout(_timecodePreviewTimer);
  _timecodePreviewTimer = setTimeout(updateTimecodeHint, 300);
});

document.getElementById("btnAdvance15").addEventListener("click", async () => {
  const input = document.getElementById("startTimecode");
  const hint = document.getElementById("timecodeHint");
  try {
    const res = await api("/api/timecode_add", {
      method: "POST",
      body: JSON.stringify({ timecode: input.value, delta_seconds: 15 }),
    });
    input.value = res.result;
    updateTimecodeHint();
  } catch (e) {
    hint.textContent = "Could not advance: " + e.message;
  }
});

async function updateTimecodeHint() {
  const hint = document.getElementById("timecodeHint");
  const raw = document.getElementById("startTimecode").value;
  const lengthSeconds = parseFloat(document.getElementById("lengthSeconds").value);
  let preview = "";
  if (raw && raw.trim()) {
    try {
      const body = { timecode: raw };
      if (!isNaN(lengthSeconds)) body.length_seconds = lengthSeconds;
      const res = await api("/api/timecode_preview", { method: "POST", body: JSON.stringify(body) });
      preview = " — " + raw + " = " + res.seconds.toFixed(3) + "s real time";
      if (res.end_normalized) {
        preview += "; ends at " + res.end_normalized + " (" + res.end_seconds.toFixed(3) + "s)";
      }
    } catch (e) {
      preview = " — " + (e.message || "invalid timecode");
    }
  }
  hint.textContent = _timecodeLabel + preview;
}

/* ---------------------------------------------------------------------- */
/* insert tab: mode toggle + insert actions                               */
/* ---------------------------------------------------------------------- */

document.getElementById("modeToggle").addEventListener("click", (ev) => {
  const opt = ev.target.closest(".toggle-option");
  if (!opt) return;
  const toggle = document.getElementById("modeToggle");
  toggle.dataset.mode = opt.dataset.value;
});

document.getElementById("btnInsertLoop").addEventListener("click", () => doInsert("loop"));
document.getElementById("btnInsertPause").addEventListener("click", () => doInsert("pause"));

document.querySelectorAll(".duration-preset").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.getElementById("lengthSeconds").value = btn.dataset.seconds;
    updateTimecodeHint();
  });
});

async function doInsert(endMode) {
  const resultEl = document.getElementById("insertResult");
  resultEl.textContent = "Inserting...";
  const body = {
    track_name: document.getElementById("trackSelect").value,
    start_timecode: document.getElementById("startTimecode").value,
    length_seconds: parseFloat(document.getElementById("lengthSeconds").value) || 15,
    mode: document.getElementById("modeToggle").dataset.mode,
    end_mode: endMode,
    create_section: document.getElementById("createSection").checked,
    asset_id: selectedAssetId,
    mapping_numbers: Array.from(selectedMappingNos),
  };
  try {
    const res = await api("/api/insert", { method: "POST", body: JSON.stringify(body) });
    resultEl.textContent = fmtJson(res);
  } catch (e) {
    resultEl.textContent = "Error: " + e.message + (e.body ? "\n\n" + fmtJson(e.body) : "");
  }
}

/* ---------------------------------------------------------------------- */
/* discovery console                                                       */
/* ---------------------------------------------------------------------- */

document.getElementById("btnInspect").addEventListener("click", async () => {
  const out = document.getElementById("inspectResult");
  out.textContent = "Inspecting...";
  try {
    const data = await api("/api/discover", {
      method: "POST",
      body: JSON.stringify({ target_expr: document.getElementById("inspectExpr").value }),
    });
    out.textContent = fmtJson(data.attributes);
  } catch (e) {
    out.textContent = "Error: " + e.message + (e.body ? "\n\n" + fmtJson(e.body) : "");
  }
});

document.getElementById("btnRunConsole").addEventListener("click", async () => {
  const retEl = document.getElementById("consoleReturn");
  const pyEl = document.getElementById("consolePyLog");
  const d3El = document.getElementById("consoleD3Log");
  retEl.textContent = "Running...";
  pyEl.textContent = "";
  d3El.textContent = "";
  try {
    const data = await api("/api/console", {
      method: "POST",
      body: JSON.stringify({ script: document.getElementById("consoleScript").value }),
    });
    retEl.textContent = fmtJson(data.returnValue);
    pyEl.textContent = data.pythonLog || "(empty)";
    d3El.textContent = data.d3Log || "(empty)";
  } catch (e) {
    retEl.textContent = "Error: " + e.message + (e.body ? "\n\n" + fmtJson(e.body) : "");
  }
});

/* ---------------------------------------------------------------------- */
/* init                                                                    */
/* ---------------------------------------------------------------------- */

loadConfig();
testConnection();
refreshTracks();
loadTimecodeInfo();
// Best-effort, silent: populates fetchedMappings so asset chips (and the
// Setup mapping table / drag-and-drop chips) can show friendly mapping
// names even if you haven't clicked "Fetch Mappings from D3" this session.
api("/api/mappings")
  .then((mappings) => {
    fetchedMappings = mappings.filter((m) => m.name && m.resource_path);
    if (currentConfig) {
      renderMappingTable(Object.keys(currentConfig.mapping_map || {}), currentConfig.mapping_map || {});
    }
    if (mediaData) renderAssetGroups();
  })
  .catch(() => {});
