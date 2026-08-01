"use strict";

const $ = selector => document.querySelector(selector);
const SHARE_PREFIX = "#result=";
const ENCRYPTED_SHARE_PREFIX = "enc.";
const MAX_SHARE_LINK_LENGTH = 60000;
const MAX_SHARED_JSON_BYTES = 2_000_000;
const MIN_SHARED_GROUP_SIZE = 5;
const MAX_CURVES = 80;
const PBKDF2_ITERATIONS = 250000;

const colors = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#7A5B00", "#000000"];
const dashPatterns = ["", "9 4", "3 3", "12 4 2 4", "1 4", "8 3 1 3", "14 4", "5 2"];
const columns = [
  ["segment", "Segment"],
  ["density_mode", "Dichtegipfel"],
  ["mode", "Diskreter Modus"],
  ["median", "Median"],
  ["mean", "Mittelwert"],
  ["q1", "Q1"],
  ["q3", "Q3"],
  ["iqr", "IQR"],
  ["mad", "MAD"],
  ["ci_low", "KI unten"],
  ["ci_high", "KI oben"],
  ["std", "Std.Abw."],
  ["variance", "Varianz"],
  ["range", "Spannweite"],
  ["minimum", "Minimum"],
  ["maximum", "Maximum"],
  ["modality", "Modalität"],
  ["skew", "Schiefe"],
  ["kurtosis", "Kurtosis"],
  ["count", "Umfang"]
];

const state = {
  file: null,
  meta: null,
  uploadToken: null,
  columnConfig: {},
  filterTree: {type: "group", logic: "AND", children: []},
  statistics: [],
  lastResult: null,
  shareMaterial: null,
  lastFilterSummary: "Keine Filter",
  sort: {key: null, direction: 1},
  hiddenSeries: new Set(),
  chartDomain: null,
  activeAbort: null,
  progressTimer: null,
  segmentTopN: "",
  estimate: null,
  estimateUnavailable: false,
  estimateTimer: null,
  estimateAbort: null,
  estimateGeneration: 0,
  sharePreflight: null
};

function toast(message, error = false) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = message;
  el.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.className = "toast"; }, 4200);
}

function setText(selector, value) {
  const el = typeof selector === "string" ? $(selector) : selector;
  if (el) el.textContent = value ?? "";
}

function loading(show, stage = "Daten werden verarbeitet …", cancellable = false) {
  const overlay = $("#loading");
  const wasHidden = overlay?.hidden !== false;
  if (show && wasHidden) state.loadingReturnFocus = document.activeElement;
  if (overlay) overlay.hidden = !show;
  setText("#loading-stage", stage);
  const fallback = overlay?.querySelector("strong");
  if (fallback && !$("#loading-stage")) fallback.textContent = stage;
  const cancel = $("#cancel-analysis");
  if (cancel) cancel.hidden = !show || !cancellable;
  const main = $("main");
  if (show) {
    main?.setAttribute("aria-busy", "true");
    if (wasHidden) requestAnimationFrame(() => {
      if (cancellable && cancel) cancel.focus();
      else if (overlay) {
        overlay.tabIndex = -1;
        overlay.focus();
      }
    });
  } else {
    main?.removeAttribute("aria-busy");
    const target = state.loadingReturnFocus;
    state.loadingReturnFocus = null;
    if (target?.isConnected && typeof target.focus === "function") target.focus();
  }
}

function startProgress() {
  const stages = [
    "Datei und Filter werden vorbereitet …",
    "Segmente werden gebildet …",
    "Kennzahlen werden berechnet …",
    "Dichtekurven werden erstellt …"
  ];
  let index = 0;
  loading(true, stages[index], true);
  const progress = $("#loading-progress");
  if (progress) progress.value = 15;
  clearInterval(state.progressTimer);
  state.progressTimer = setInterval(() => {
    index = Math.min(index + 1, stages.length - 1);
    if (progress) progress.value = [15, 40, 70, 90][index];
    setText("#loading-stage", stages[index]);
    const fallback = $("#loading")?.querySelector("strong");
    if (fallback && !$("#loading-stage")) fallback.textContent = stages[index];
  }, 1800);
}

function stopProgress() {
  clearInterval(state.progressTimer);
  state.progressTimer = null;
  $("#loading-progress")?.removeAttribute("value");
  loading(false);
}

function option(value, label = value) {
  const el = document.createElement("option");
  el.value = value;
  el.textContent = label;
  return el;
}

function button(label, className, handler) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = className;
  el.textContent = label;
  el.addEventListener("click", handler);
  return el;
}

function humanBytes(size) {
  if (!Number.isFinite(size)) return "";
  return size < 1024 * 1024
    ? `${(size / 1024).toFixed(1)} KB`
    : `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function formatDelimiter(value) {
  if (value === "\t" || value === "tab" || value === "Tabulator") return "Tabulator";
  if (value === "," || value === "comma") return "Komma";
  if (value === ";" || value === "semicolon") return "Semikolon";
  if (value === "|" || value === "pipe") return "Pipe";
  return value || "automatisch";
}

function formForData({forceFile = false} = {}) {
  const form = new FormData();
  if (!forceFile && state.uploadToken) form.append("upload_token", state.uploadToken);
  else if (state.file) form.append("file", state.file);
  return form;
}

async function api(path, form, {signal, method = "POST"} = {}) {
  const response = await fetch(path, {method, body: form, signal});
  const data = await response.json().catch(() => ({error: "Ungültige Serverantwort."}));
  if (!response.ok) {
    const error = new Error(data.error || "Die Anfrage ist fehlgeschlagen.");
    error.status = response.status;
    error.details = data;
    throw error;
  }
  return data;
}

function columnInfo(name) {
  return state.meta?.columns?.find(column => column.name === name);
}

function uniqueCount(column) {
  const value = column?.unique_count ?? column?.cardinality ?? column?.nunique;
  if (Number.isFinite(Number(value))) return Number(value);
  return Array.isArray(column?.unique) ? column.unique.length : 0;
}

function missingCount(column) {
  const value = column?.missing_count ?? column?.missing ?? column?.null_count;
  if (Number.isFinite(Number(value))) return Number(value);
  return Math.max(0, Number(state.meta?.rows || 0) - Number(column?.valid_count ?? state.meta?.rows ?? 0));
}

function invalidCount(column) {
  const value = column?.invalid_count ?? column?.invalid_numeric_count ?? column?.parse_errors;
  return Number.isFinite(Number(value)) ? Number(value) : 0;
}

function displayColumn(name, withUnit = true) {
  const config = state.columnConfig[name] || {};
  const alias = String(config.alias || name || "").trim();
  const unit = String(config.unit || "").trim();
  return withUnit && unit ? `${alias} [${unit}]` : alias;
}

function normalizedColumnConfig() {
  const result = {};
  for (const [name, config] of Object.entries(state.columnConfig)) {
    const alias = String(config.alias || "").trim();
    const unit = String(config.unit || "").trim();
    if (!alias && !unit) continue;
    result[name] = {alias, unit};
  }
  return result;
}

function xDisplayLabel(result = state.lastResult) {
  const raw = result?.x_column ?? result?.raw_x_column ?? result?.reproducibility?.x_column;
  if (raw && state.columnConfig[raw]) return displayColumn(raw);
  if (result?.display_x_label) return result.display_x_label;
  const label = result?.x_label || raw || $("#x-column")?.value || "Wert";
  if (state.columnConfig[label]) return displayColumn(label);
  return label;
}

function inspectForm(forceFile = false) {
  const form = formForData({forceFile});
  const fields = [
    ["delimiter", "#parse-delimiter"],
    ["encoding", "#parse-encoding"],
    ["decimal", "#parse-decimal"],
    ["thousands", "#parse-thousands"]
  ];
  fields.forEach(([name, selector]) => {
    const value = $(selector)?.value;
    if (value && value !== "auto") form.append(name, value);
  });
  return form;
}

async function loadFile(file) {
  if (!file) return;
  if (!/\.(tsv|csv|txt)$/i.test(file.name)) {
    toast("Bitte eine TSV-, CSV- oder TXT-Datei auswählen.", true);
    return;
  }
  state.file = file;
  state.uploadToken = null;
  await inspectData({forceFile: true, initial: true});
}

async function inspectData({forceFile = false, initial = false} = {}) {
  if (!state.file && !state.uploadToken) return;
  loading(true, initial ? "Datei wird geprüft …" : "Import wird erneut geprüft …");
  try {
    const data = await api("/api/inspect", inspectForm(forceFile));
    applyInspectResult(data, {initial});
    toast(initial ? "Datei erfolgreich eingelesen." : "Importeinstellungen wurden übernommen.");
  } catch (error) {
    if (state.uploadToken && !forceFile && state.file && [400, 404, 410, 422].includes(error.status)) {
      try {
        const data = await api("/api/inspect", inspectForm(true));
        applyInspectResult(data, {initial});
        toast("Datei wurde mit den neuen Importeinstellungen geprüft.");
        return;
      } catch (fallbackError) {
        toast(fallbackError.message, true);
      }
    } else {
      if (initial) state.file = null;
      toast(error.message, true);
    }
  } finally {
    loading(false);
  }
}

function normalizeInspect(data) {
  const columnsValue = Array.isArray(data.columns) ? data.columns : [];
  data.columns = columnsValue.map(column => ({
    ...column,
    name: String(column.name ?? column.column ?? ""),
    numeric: Boolean(column.numeric ?? column.is_numeric ?? ["number", "numeric", "integer", "float"].includes(String(column.type || column.dtype).toLowerCase())),
    unique: Array.isArray(column.unique) ? column.unique : Array.isArray(column.sample_values) ? column.sample_values : []
  }));
  data.rows = Number(data.rows ?? data.row_count ?? 0);
  data.preview = Array.isArray(data.preview) ? data.preview : [];
  const parse = data.effective_parse_options || data.parse_options || {};
  data.encoding = parse.encoding ?? data.encoding;
  data.delimiter_value = parse.delimiter ?? data.delimiter_value;
  data.delimiter_label = parse.delimiter_label ?? data.delimiter_label ?? data.delimiter;
  data.decimal = parse.decimal_separator ?? parse.decimal ?? data.decimal;
  data.thousands = parse.thousands_separator ?? parse.thousands ?? data.thousands;
  return data;
}

function applyInspectResult(rawData, {initial = false} = {}) {
  const data = normalizeInspect(rawData);
  state.meta = data;
  state.uploadToken = data.upload_token || data.token || state.uploadToken;
  const priorConfig = state.columnConfig;
  state.columnConfig = {};
  data.columns.forEach(column => {
    const server = data.column_config?.[column.name] || {};
    state.columnConfig[column.name] = {
      alias: priorConfig[column.name]?.alias ?? server.alias ?? column.alias ?? "",
      unit: priorConfig[column.name]?.unit ?? server.unit ?? column.unit ?? ""
    };
  });
  state.filterTree = {type: "group", logic: "AND", children: []};
  state.lastResult = null;
  state.shareMaterial = null;
  state.statistics = [];
  state.hiddenSeries.clear();
  state.chartDomain = null;
  state.estimate = null;
  state.estimateUnavailable = false;

  setText("#file-name", data.filename || state.file?.name || "Datensatz");
  const metaParts = [
    `${data.rows.toLocaleString("de-DE")} Zeilen`,
    `${data.columns.length} Spalten`,
    formatDelimiter(data.delimiter_label || data.delimiter),
    data.encoding,
    humanBytes(state.file?.size)
  ].filter(Boolean);
  setText("#file-meta", metaParts.join(" · "));
  initializeParseControls(data, initial);
  renderDataCheck();
  populateAnalysisSelects();
  renderFilterBuilder();
  updateSegmentEstimate();

  const dataCheck = $("#data-check");
  if (dataCheck) dataCheck.hidden = false;
  const uploadView = $("#upload-view");
  const workspace = $("#workspace");
  if (uploadView) uploadView.hidden = true;
  if (workspace) workspace.hidden = false;
  if ($("#stats-card")) $("#stats-card").hidden = true;
  if ($("#chart-wrap")) $("#chart-wrap").hidden = true;
  if ($("#chart-toolbar")) $("#chart-toolbar").hidden = true;
  if ($("#empty-chart")) $("#empty-chart").hidden = false;
  scheduleEstimate();
  window.scrollTo?.({top: 0, behavior: "smooth"});
}

function initializeParseControls(data, initial) {
  const delimiter = $("#parse-delimiter");
  const encoding = $("#parse-encoding");
  const decimal = $("#parse-decimal");
  const thousands = $("#parse-thousands");
  const setIfAvailable = (el, value) => {
    if (!el || value == null) return;
    const raw = String(value);
    const values = [...el.options].map(item => item.value);
    if (values.includes(raw)) el.value = raw;
  };
  if (initial || delimiter?.value === "auto") setIfAvailable(delimiter, data.delimiter_value ?? data.separator ?? data.delimiter);
  if (initial || encoding?.value === "auto") setIfAvailable(encoding, data.encoding_value ?? data.encoding);
  if (initial && decimal) setIfAvailable(decimal, data.decimal ?? ".");
  if (initial && thousands) setIfAvailable(thousands, data.thousands ?? "");
}

function renderDataCheck() {
  renderQualitySummary();
  renderPreview();
  renderColumnProfile();
  renderColumnConfig();
}

function renderQualitySummary() {
  const root = $("#data-quality-summary");
  if (!root || !state.meta) return;
  root.replaceChildren();
  const totalMissing = state.meta.columns.reduce((sum, column) => sum + missingCount(column), 0);
  const totalInvalid = state.meta.columns.reduce((sum, column) => sum + invalidCount(column), 0);
  const numeric = state.meta.columns.filter(column => column.numeric).length;
  const summary = document.createElement("p");
  summary.textContent = `${state.meta.rows.toLocaleString("de-DE")} Zeilen geprüft · ${numeric} numerische Spalten · ${totalMissing.toLocaleString("de-DE")} fehlende · ${totalInvalid.toLocaleString("de-DE")} ungültige Werte`;
  root.append(summary);
  const parse = state.meta.effective_parse_options || state.meta.parse_options;
  if (parse) {
    const sources = parse.sources || {};
    const detected = key => sources[key] === "detected" || sources[key] === "auto" ? "automatisch" : "manuell";
    const parseSummary = document.createElement("small");
    parseSummary.textContent = `Import: ${parse.encoding} (${detected("encoding")}) · ${parse.delimiter_label || formatDelimiter(parse.delimiter)} (${detected("delimiter")}) · Dezimal „${parse.decimal_separator}“ (${detected("decimal_separator")}) · Tausender ${parse.thousands_separator == null ? "keines" : `„${parse.thousands_separator}“`} (${detected("thousands_separator")})`;
    root.append(parseSummary);
  }
  const warnings = [
    ...(Array.isArray(state.meta.warnings) ? state.meta.warnings : []),
    ...(Array.isArray(state.meta.quality?.warnings) ? state.meta.quality.warnings : []),
    ...(Array.isArray(state.meta.data_quality_warnings) ? state.meta.data_quality_warnings : [])
  ];
  const nonFinite = state.meta.columns.reduce((sum, column) => sum + Number(column.non_finite_count || 0), 0);
  if (nonFinite) warnings.push(`${nonFinite.toLocaleString("de-DE")} nicht-endliche Zahlen (Infinity/−Infinity) werden von der Analyse ausgeschlossen.`);
  if (!numeric) warnings.push("Keine Spalte wurde sicher als numerisch erkannt. Prüfen Sie Dezimal- und Tausendertrennzeichen.");
  const distinctWarnings = [...new Map(warnings.map(value => {
    const message = typeof value === "string" ? value : value.message || JSON.stringify(value);
    return [message, message];
  })).values()];
  if (distinctWarnings.length) {
    const list = document.createElement("ul");
    distinctWarnings.forEach(value => {
      const item = document.createElement("li");
      item.textContent = value;
      list.append(item);
    });
    root.append(list);
  }
}

function renderPreview() {
  const table = $("#preview-table");
  if (!table || !state.meta) return;
  table.replaceChildren();
  const caption = document.createElement("caption");
  caption.className = "sr-only";
  caption.textContent = "Vorschau der importierten Datensätze";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  state.meta.columns.forEach(column => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = displayColumn(column.name);
    headRow.append(th);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  state.meta.preview.forEach(row => {
    const tr = document.createElement("tr");
    state.meta.columns.forEach(column => {
      const td = document.createElement("td");
      const value = row[column.name];
      td.textContent = value == null ? "–" : String(value);
      tr.append(td);
    });
    body.append(tr);
  });
  table.append(caption, head, body);
}

function topValues(column) {
  const source = column.top_values ?? column.value_counts;
  if (Array.isArray(source)) return source.map(item => {
    if (item && typeof item === "object") return [item.value ?? item.label ?? item.key, item.count ?? item.n ?? ""];
    return [item, ""];
  }).filter(([value]) => value != null && value !== "").slice(0, 5);
  if (source && typeof source === "object") return Object.entries(source).filter(([value]) => value !== "null" && value !== "undefined").slice(0, 5);
  return [];
}

function renderColumnProfile() {
  const root = $("#column-profile");
  if (!root || !state.meta) return;
  root.replaceChildren();
  state.meta.columns.forEach(column => {
    const article = document.createElement("article");
    article.className = "column-profile-item";
    const title = document.createElement("strong");
    title.textContent = displayColumn(column.name);
    const type = column.datatype || column.type || column.dtype || (column.numeric ? "Numerisch" : "Text");
    const facts = document.createElement("span");
    facts.textContent = `${type} · ${uniqueCount(column).toLocaleString("de-DE")} eindeutig · ${missingCount(column).toLocaleString("de-DE")} fehlend${invalidCount(column) ? ` · ${invalidCount(column).toLocaleString("de-DE")} ungültig` : ""}`;
    article.append(title, facts);
    const numericFacts = [
      ["Min", column.minimum ?? column.min],
      ["Max", column.maximum ?? column.max],
      ["Mittel", column.mean]
    ].filter(([, value]) => value != null && value !== "" && Number.isFinite(Number(value)));
    if (numericFacts.length) {
      const detail = document.createElement("small");
      detail.textContent = numericFacts.map(([label, value]) => `${label}: ${format(Number(value), "profile")}`).join(" · ");
      article.append(detail);
    }
    const top = topValues(column);
    if (top.length) {
      const detail = document.createElement("small");
      detail.textContent = `Häufig: ${top.map(([value, count]) => count === "" ? value : `${value} (${Number(count).toLocaleString("de-DE")})`).join(", ")}`;
      article.append(detail);
    }
    root.append(article);
  });
}

function renderColumnConfig() {
  const root = $("#column-config");
  if (!root || !state.meta) return;
  root.replaceChildren();
  state.meta.columns.forEach(column => {
    const row = document.createElement("div");
    row.className = "column-config-row";
    const name = document.createElement("strong");
    name.textContent = column.name;
    const alias = document.createElement("input");
    alias.type = "text";
    alias.placeholder = "Anzeigename";
    alias.value = state.columnConfig[column.name]?.alias || "";
    alias.setAttribute("aria-label", `Anzeigename für ${column.name}`);
    const unit = document.createElement("input");
    unit.type = "text";
    unit.placeholder = "Einheit";
    unit.value = state.columnConfig[column.name]?.unit || "";
    unit.setAttribute("aria-label", `Einheit für ${column.name}`);
    const update = () => {
      state.columnConfig[column.name] = {alias: alias.value, unit: unit.value};
      refreshColumnLabels();
    };
    alias.addEventListener("input", update);
    unit.addEventListener("input", update);
    row.append(name, alias, unit);
    root.append(row);
  });
}

function refreshColumnLabels() {
  const selected = {
    x: $("#x-column")?.value,
    h1: $("#hue-1")?.value,
    h2: $("#hue-2")?.value
  };
  populateAnalysisSelects(selected);
  renderFilterBuilder();
  renderPreview();
  renderColumnProfile();
  if (state.lastResult) {
    setText("#chart-title", `Verteilung von ${xDisplayLabel()}`);
    drawChart();
    renderReproducibility();
  }
}

function xRecommendation() {
  const requested = state.meta?.recommended_x;
  const explicit = typeof requested === "string" ? requested : Array.isArray(requested) ? requested[0] : requested?.name;
  if (explicit && columnInfo(explicit)) return explicit;
  const rows = Math.max(1, Number(state.meta?.rows || 1));
  const candidates = state.meta?.columns.filter(column => column.numeric) || [];
  return candidates.map((column, index) => {
    const name = column.name.toLowerCase();
    const ratio = uniqueCount(column) / rows;
    let score = 100 - index;
    if (column.recommended_x) score += 1000;
    if (/(^|[_\s-])(id|index|row|key|nr|nummer)([_\s-]|$)/i.test(name)) score -= 500;
    if (ratio > 0.98 && (column.monotonic || column.id_like || /id|index|nr|nummer/i.test(name))) score -= 400;
    if (uniqueCount(column) <= 1) score -= 1000;
    if (uniqueCount(column) >= 5 && ratio < 0.95) score += 30;
    return {name: column.name, score};
  }).sort((a, b) => b.score - a.score)[0]?.name;
}

function selectLabel(column, segment = false) {
  if (!segment) return displayColumn(column.name);
  const cardinality = uniqueCount(column);
  const warning = cardinality > MAX_CURVES ? " ⚠" : "";
  return `${displayColumn(column.name)} · ${cardinality.toLocaleString("de-DE")} Ausprägungen${warning}`;
}

function populateAnalysisSelects(selected = {}) {
  if (!state.meta) return;
  const x = $("#x-column"), h1 = $("#hue-1"), h2 = $("#hue-2");
  const previousX = selected.x ?? x?.value;
  const previousH1 = selected.h1 ?? h1?.value;
  const previousH2 = selected.h2 ?? h2?.value;
  const names = state.meta.columns;
  const numeric = names.filter(column => column.numeric);
  if (x) {
    x.replaceChildren();
    (numeric.length ? numeric : names).forEach(column => x.append(option(column.name, selectLabel(column))));
    const next = [...x.options].some(item => item.value === previousX) ? previousX : xRecommendation();
    if (next) x.value = next;
  }
  [h1, h2].forEach((el, index) => {
    if (!el) return;
    const previous = index ? previousH2 : previousH1;
    el.replaceChildren(option("", "– Keine –"));
    names.forEach(column => el.append(option(column.name, selectLabel(column, true))));
    if ([...el.options].some(item => item.value === previous)) el.value = previous;
  });
  preventDuplicateSegments();
}

function preventDuplicateSegments(changed) {
  const h1 = $("#hue-1"), h2 = $("#hue-2");
  if (!h1 || !h2) return;
  if (h1.value && h1.value === h2.value) {
    if (changed === h2) h1.value = "";
    else h2.value = "";
    toast("Eine Segmentspalte kann nur einmal verwendet werden.", true);
  }
  [...h1.options].forEach(item => { item.disabled = Boolean(item.value && item.value === h2.value); });
  [...h2.options].forEach(item => { item.disabled = Boolean(item.value && item.value === h1.value); });
}

function ensureTopNControl(root) {
  let select = root.querySelector("[data-segment-top-n]");
  if (select) return select;
  const label = document.createElement("label");
  label.className = "segment-top-n";
  const caption = document.createElement("span");
  caption.textContent = "Ausprägungen je Segment";
  select = document.createElement("select");
  select.dataset.segmentTopN = "true";
  [["", "Alle"], ["5", "Top 5 + Sonstige"], ["10", "Top 10 + Sonstige"], ["20", "Top 20 + Sonstige"]].forEach(([value, textValue]) => select.append(option(value, textValue)));
  select.value = state.segmentTopN;
  select.addEventListener("change", () => {
    state.segmentTopN = select.value;
    updateSegmentEstimate();
    scheduleEstimate();
  });
  label.append(caption, select);
  root.append(label);
  return select;
}

function localCurveEstimate() {
  const topN = Number(state.segmentTopN) || Infinity;
  const selected = [$("#hue-1")?.value, $("#hue-2")?.value].filter(Boolean);
  if (!selected.length) return 1;
  return selected.reduce((total, name) => {
    const cardinality = uniqueCount(columnInfo(name));
    const reduced = Number.isFinite(topN) && cardinality > topN ? topN + 1 : cardinality;
    return total * Math.max(1, reduced);
  }, 1);
}

function updateSegmentEstimate() {
  const root = $("#segment-estimate");
  if (!root || !state.meta) return;
  let summary = root.querySelector("[data-estimate-summary]");
  if (!summary) {
    summary = document.createElement("div");
    summary.dataset.estimateSummary = "true";
    root.prepend(summary);
  }
  ensureTopNControl(root);
  const estimate = state.estimate;
  const curves = Number(estimate?.curve_count ?? estimate?.curves ?? localCurveEstimate());
  const rows = estimate?.filtered_rows;
  const small = Number(estimate?.share_blocked_group_count ?? estimate?.small_group_count ?? estimate?.small_groups?.length ?? 0);
  const high = curves > MAX_CURVES || Boolean(estimate?.exceeds_curve_limit);
  summary.className = `segment-estimate-summary${high || small ? " warning" : ""}`;
  const parts = [
    `${estimate ? "Voraussichtlich" : "Bis zu"} ${curves.toLocaleString("de-DE")} Kurve${curves === 1 ? "" : "n"}`,
    Number.isFinite(Number(rows)) ? `${Number(rows).toLocaleString("de-DE")} gefilterte Zeilen` : "",
    small ? `${small} Kleingruppe${small === 1 ? "" : "n"} (n < ${MIN_SHARED_GROUP_SIZE})` : ""
  ].filter(Boolean);
  summary.textContent = parts.join(" · ");
  if (high) summary.textContent += ` · Maximum ${MAX_CURVES}: Top-N wählen oder Segmentierung reduzieren.`;
}

function appendAnalysisFields(form) {
  form.append("x_column", $("#x-column")?.value || "");
  form.append("hue1", $("#hue-1")?.value || "");
  form.append("hue2", $("#hue-2")?.value || "");
  form.append("filter_tree", JSON.stringify(state.filterTree));
  form.append("column_config", JSON.stringify(normalizedColumnConfig()));
  const bandwidth = Number($("#bandwidth")?.value || 1);
  form.append("bandwidth", Number.isFinite(bandwidth) && bandwidth > 0 ? String(bandwidth) : "1");
  form.append("segment_top_n", state.segmentTopN || "");
  const expiry = $("#share-expiry-days")?.value;
  if (expiry) form.append("share_expiry_days", expiry);
  return form;
}

function scheduleEstimate() {
  clearTimeout(state.estimateTimer);
  state.estimateGeneration += 1;
  state.estimateAbort?.abort();
  state.estimateAbort = null;
  state.estimate = null;
  updateSegmentEstimate();
  const generation = state.estimateGeneration;
  state.estimateTimer = setTimeout(() => requestEstimate(generation), 400);
}

async function requestEstimate(generation = state.estimateGeneration) {
  if (generation !== state.estimateGeneration) return;
  if (!state.uploadToken || state.estimateUnavailable || !state.meta) {
    state.estimate = null;
    updateSegmentEstimate();
    return;
  }
  const controller = new AbortController();
  state.estimateAbort = controller;
  try {
    const data = await api("/api/estimate", appendAnalysisFields(formForData()), {signal: controller.signal});
    if (generation !== state.estimateGeneration) return;
    state.estimate = data;
    updateSegmentEstimate();
  } catch (error) {
    if (error.name === "AbortError" || generation !== state.estimateGeneration) return;
    if ([404, 405, 501].includes(error.status)) state.estimateUnavailable = true;
    if ([400, 410].includes(error.status) && /abgelaufen|temporär|upload/i.test(error.message)) state.uploadToken = null;
    state.estimate = null;
    updateSegmentEstimate();
  } finally {
    if (state.estimateAbort === controller) state.estimateAbort = null;
  }
}

function newCondition() {
  const column = state.meta?.columns?.[0]?.name || "";
  return {type: "condition", column, operator: "==", value: ""};
}

function newGroup() {
  return {type: "group", logic: "AND", children: []};
}

function renderFilterBuilder() {
  const builder = $("#filter-builder");
  if (!builder || !state.meta) return;
  builder.replaceChildren(renderGroup(state.filterTree, true, 0));
}

function renderGroup(group, isRoot, depth) {
  const wrapper = document.createElement("div");
  wrapper.className = `filter-group${isRoot ? " root" : " nested"}`;
  const header = document.createElement("div");
  header.className = "filter-group-header";
  const title = document.createElement("span");
  title.className = "filter-group-title";
  title.textContent = isRoot ? "Hauptgruppe" : `Untergruppe · Ebene ${depth}`;
  const logic = document.createElement("select");
  logic.className = "group-logic";
  logic.setAttribute("aria-label", isRoot ? "Verknüpfung der Hauptgruppe" : `Verknüpfung der Untergruppe auf Ebene ${depth}`);
  logic.append(option("AND", "Alle (UND)"), option("OR", "Mind. eine (ODER)"));
  logic.value = group.logic;
  logic.addEventListener("change", () => {
    group.logic = logic.value;
    scheduleEstimate();
  });
  header.append(title, logic);
  wrapper.append(header);

  const children = document.createElement("div");
  children.className = "filter-children";
  if (!group.children.length) {
    const empty = document.createElement("div");
    empty.className = "filter-empty";
    empty.textContent = "Noch keine Filter in dieser Gruppe";
    children.append(empty);
  }
  group.children.forEach((child, index) => {
    children.append(child.type === "group" ? renderGroup(child, false, depth + 1) : renderCondition(child, group, index, depth));
  });
  wrapper.append(children);

  const actions = document.createElement("div");
  actions.className = "filter-group-actions";
  actions.append(
    button("+ Bedingung", "filter-action", () => {
      group.children.push(newCondition());
      renderFilterBuilder();
      scheduleEstimate();
    }),
    button("+ Untergruppe", "filter-action", () => {
      if (depth >= 5) {
        toast("Maximal sechs Filterebenen sind möglich.", true);
        return;
      }
      group.children.push(newGroup());
      renderFilterBuilder();
      scheduleEstimate();
    })
  );
  if (!isRoot) {
    actions.append(button("Gruppe entfernen", "filter-action remove", () => {
      removeNode(state.filterTree, group);
      renderFilterBuilder();
      scheduleEstimate();
    }));
  }
  wrapper.append(actions);
  return wrapper;
}

function renderCondition(condition, parent, index, depth = 0) {
  const row = document.createElement("div");
  row.className = "filter-condition";
  const labelPrefix = `${depth ? `Untergruppe Ebene ${depth}` : "Hauptgruppe"}, Bedingung ${index + 1}`;
  const column = document.createElement("select");
  column.setAttribute("aria-label", `${labelPrefix}: Spalte`);
  state.meta.columns.forEach(item => column.append(option(item.name, displayColumn(item.name))));
  column.value = condition.column;
  const operator = document.createElement("select");
  operator.setAttribute("aria-label", `${labelPrefix}: Vergleichsoperator`);
  const setOperators = () => {
    const current = condition.operator;
    const values = columnInfo(condition.column)?.numeric ? ["==", "!=", ">", "<"] : ["==", "!="];
    operator.replaceChildren(...values.map(value => option(value)));
    condition.operator = values.includes(current) ? current : "==";
    operator.value = condition.operator;
  };
  setOperators();
  const value = document.createElement("input");
  value.type = "text";
  value.autocomplete = "off";
  value.placeholder = "Wert";
  value.setAttribute("aria-label", `${labelPrefix}: Vergleichswert`);
  value.value = condition.value;
  const listId = `filter-values-${Math.random().toString(36).slice(2)}`;
  const suggestions = document.createElement("datalist");
  suggestions.id = listId;
  value.setAttribute("list", listId);
  const updateSuggestions = () => {
    suggestions.replaceChildren();
    if (![">", "<"].includes(condition.operator)) {
      (columnInfo(condition.column)?.unique || []).forEach(item => suggestions.append(option(item)));
    }
  };
  updateSuggestions();
  column.addEventListener("change", () => {
    condition.column = column.value;
    setOperators();
    updateSuggestions();
    scheduleEstimate();
  });
  operator.addEventListener("change", () => {
    condition.operator = operator.value;
    updateSuggestions();
    scheduleEstimate();
  });
  value.addEventListener("input", () => {
    condition.value = value.value;
    scheduleEstimate();
  });
  const remove = button("×", "condition-remove", () => {
    parent.children.splice(index, 1);
    renderFilterBuilder();
    scheduleEstimate();
  });
  remove.setAttribute("aria-label", "Bedingung entfernen");
  row.append(column, operator, value, suggestions, remove);
  return row;
}

function removeNode(group, target) {
  const index = group.children.indexOf(target);
  if (index >= 0) {
    group.children.splice(index, 1);
    return true;
  }
  return group.children.some(child => child.type === "group" && removeNode(child, target));
}

function validateFilterTree(node, isRoot = true) {
  if (node.type === "condition") {
    if (!node.column || !String(node.value).trim()) return "Jede Filterbedingung benötigt Spalte und Wert.";
    return null;
  }
  if (!isRoot && node.children.length === 0) return "Leere Untergruppen bitte befüllen oder entfernen.";
  for (const child of node.children) {
    const error = validateFilterTree(child, false);
    if (error) return error;
  }
  return null;
}

function filterExpression(node, isRoot = true) {
  const operators = {"==": "=", "!=": "≠", ">": ">", "<": "<"};
  if (node.type === "condition") return `${displayColumn(node.column)} ${operators[node.operator]} "${node.value}"`;
  if (!node.children.length) return "Keine Filter";
  const connector = node.logic === "AND" ? " UND " : " ODER ";
  const expression = node.children.map(child => filterExpression(child, false)).join(connector);
  return (!isRoot || node.children.length > 1) ? `(${expression})` : expression;
}

async function analyze() {
  if (!state.file && !state.uploadToken) return;
  const validationError = validateFilterTree(state.filterTree);
  if (validationError) {
    toast(validationError, true);
    return;
  }
  if (!$("#x-column")?.value) {
    toast("Bitte eine numerische X-Achse auswählen.", true);
    return;
  }
  const estimatedCurves = Number(state.estimate?.curve_count ?? localCurveEstimate());
  if (estimatedCurves > MAX_CURVES || state.estimate?.exceeds_curve_limit) {
    toast(`Die Auswahl kann mehr als ${MAX_CURVES} Kurven erzeugen. Bitte Top-N wählen oder Segmentierung reduzieren.`, true);
    return;
  }
  state.activeAbort?.abort();
  const controller = new AbortController();
  state.activeAbort = controller;
  startProgress();
  try {
    const form = appendAnalysisFields(formForData());
    let data;
    try {
      data = await api("/api/analyze", form, {signal: controller.signal});
    } catch (error) {
      if (state.uploadToken && state.file && [400, 404, 410, 422].includes(error.status) && !controller.signal.aborted) {
        state.uploadToken = null;
        data = await api("/api/analyze", appendAnalysisFields(inspectForm(true)), {signal: controller.signal});
      } else throw error;
    }
    applyAnalysisResult(data);
    toast("Analyse wurde erstellt.");
  } catch (error) {
    if (error.name === "AbortError" || controller.signal.aborted) toast("Analyse wurde abgebrochen.");
    else toast(error.message, true);
  } finally {
    if (state.activeAbort === controller) state.activeAbort = null;
    stopProgress();
  }
}

function applyAnalysisResult(data) {
  state.lastResult = data;
  if (data.column_config && typeof data.column_config === "object") {
    Object.entries(data.column_config).forEach(([name, config]) => {
      state.columnConfig[name] = {
        alias: config?.alias ?? state.columnConfig[name]?.alias ?? "",
        unit: config?.unit ?? state.columnConfig[name]?.unit ?? ""
      };
    });
  }
  state.shareMaterial = data.share || null;
  state.statistics = Array.isArray(data.statistics) ? data.statistics : [];
  state.lastFilterSummary = filterExpression(state.filterTree);
  state.sort = {key: null, direction: 1};
  state.hiddenSeries.clear();
  state.chartDomain = null;
  state.sharePreflight = null;
  const label = xDisplayLabel(data);
  setText("#chart-title", `Verteilung von ${label}`);
  setText("#result-count", resultCoverageText(data));
  if ($("#empty-chart")) $("#empty-chart").hidden = true;
  if ($("#chart-wrap")) $("#chart-wrap").hidden = false;
  if ($("#chart-toolbar")) $("#chart-toolbar").hidden = false;
  if ($("#stats-card")) $("#stats-card").hidden = false;
  const shareButton = $("#share-result");
  if (shareButton) {
    shareButton.disabled = !data.share;
    shareButton.title = data.share ? "Signierten Freigabelink erstellen" : (data.share_blocked_reason || "Für dieses Ergebnis ist keine Freigabe verfügbar.");
  }
  renderReproducibility();
  drawChart();
  renderTable();
}

function resultCoverageText(result) {
  const sourceRows = Math.max(0, Number(result?.source_rows ?? result?.filtered_rows ?? 0));
  const plottedRows = Number(result?.plotted_rows);
  const omittedRows = Math.max(0, Number(result?.omitted_small_group_rows ?? 0));
  const omittedGroups = Math.max(0, Number(result?.omitted_small_group_count ?? 0));
  if (Number.isFinite(plottedRows) && (omittedRows || omittedGroups || plottedRows !== sourceRows)) {
    const groupText = omittedGroups ? ` in ${omittedGroups.toLocaleString("de-DE")} Kleingruppe${omittedGroups === 1 ? "" : "n"}` : "";
    return `${Math.max(0, plottedRows).toLocaleString("de-DE")} von ${sourceRows.toLocaleString("de-DE")} dargestellt · ${omittedRows.toLocaleString("de-DE")} Zeile${omittedRows === 1 ? "" : "n"}${groupText} ausgelassen`;
  }
  return `${sourceRows.toLocaleString("de-DE")} Datensätze`;
}

function reproducibilityEntries(result = state.lastResult) {
  if (!result) return [];
  const repro = result.reproducibility || result.metadata || {};
  const parse = repro.parse_options || {};
  const methodology = result.methodology || {};
  const bandwidth = repro.bandwidth_label ?? repro.bandwidth ?? methodology.kde?.bandwidth ?? result.bandwidth ?? $("#bandwidth")?.value;
  const sampledCurves = curveSeries(result).filter(curve => curve.kde_sampled).length;
  const segmentNames = (result.segment_columns || []).map(item => item.display_name || displayColumn(item.name)).filter(Boolean);
  const exclusions = result.exclusions || repro.exclusions;
  const exclusionText = exclusions && typeof exclusions === "object"
    ? Object.entries(exclusions).filter(([, value]) => Number(value)).map(([key, value]) => `${key}: ${Number(value).toLocaleString("de-DE")}`).join(", ")
    : exclusions;
  const entries = [
    ["X-Achse", xDisplayLabel(result)],
    ["Segmentierung", segmentNames.length ? segmentNames.join(" × ") : "Keine"],
    ["Datenabdeckung", resultCoverageText(result)],
    ["Methode", repro.method ?? repro.kde_method ?? result.kde_method ?? (methodology.kde ? "Kern-Dichteschätzung (KDE)" : undefined)],
    ["Bandbreite", bandwidth],
    ["KDE-Stichprobe", sampledCurves ? `${sampledCurves} Kurve(n) deterministisch auf max. ${repro.kde_max_sample_size ?? methodology.kde?.max_sample_size ?? "–"} Werte reduziert` : undefined],
    ["Konfidenzintervall", repro.confidence_level ? `${Number(repro.confidence_level) * (Number(repro.confidence_level) <= 1 ? 100 : 1)} %` : undefined],
    ["Bootstrap", repro.bootstrap_iterations],
    ["Mittelwert-KI", methodology.mean_ci],
    ["Modalität", methodology.modality?.note],
    ["Ausschlüsse", exclusionText],
    ["Dezimaltrennzeichen", parse.decimal_separator ?? repro.decimal ?? state.meta?.decimal],
    ["Tausendertrennzeichen", parse.thousands_separator ?? repro.thousands ?? state.meta?.thousands],
    ["Trennzeichen", parse.delimiter_label ?? parse.delimiter ?? repro.delimiter ?? state.meta?.delimiter_label ?? state.meta?.delimiter],
    ["Encoding", parse.encoding ?? repro.encoding ?? state.meta?.encoding],
    ["App-Version", repro.app_version ?? result.app_version],
    ["Rechenzeit", result.timing_ms?.total != null ? `${result.timing_ms.total} ms` : undefined]
  ];
  return entries.filter(([, value]) => value !== undefined && value !== null && value !== "");
}

function renderReproducibility() {
  const root = $("#export-filter-summary");
  if (!root) return;
  root.replaceChildren();
  const filterStrong = document.createElement("strong");
  filterStrong.textContent = document.body.classList.contains("shared-mode") ? "Geteilter Filter:" : "Exportfilter:";
  root.append(filterStrong, document.createTextNode(` ${state.lastFilterSummary}`));
  const entries = reproducibilityEntries();
  if (entries.length) {
    const detail = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = "Reproduzierbarkeit:";
    detail.append(strong, document.createTextNode(` ${entries.map(([key, value]) => `${key} ${value}`).join(" · ")}`));
    root.append(detail);
  }
}

function curveSeries(result = state.lastResult) {
  if (!result) return [];
  const histograms = Array.isArray(result.histograms) ? result.histograms : [];
  return (Array.isArray(result.curves) ? result.curves : []).map((curve, index) => {
    const label = String(curve.label ?? curve.segment ?? `Segment ${index + 1}`);
    const histogram = curve.histogram || histograms.find(item => (item.label ?? item.segment) === label) || histograms[index];
    const rug = curve.rug ?? curve.sample_values ?? curve.values ?? result.rug?.[label] ?? result.rug_samples?.[label] ?? [];
    return {...curve, label, key: `${index}:${label}`, histogram, rug: Array.isArray(rug) ? rug.filter(Number.isFinite) : []};
  });
}

function histogramPoints(histogram) {
  if (!histogram) return [];
  const edges = histogram.bin_edges ?? histogram.edges ?? histogram.bins;
  const values = histogram.density ?? histogram.densities ?? histogram.y ?? histogram.counts ?? histogram.frequencies;
  if (Array.isArray(edges) && Array.isArray(values) && edges.length === values.length + 1) {
    return values.map((value, index) => ({left: Number(edges[index]), right: Number(edges[index + 1]), value: Number(value)})).filter(item => Object.values(item).every(Number.isFinite));
  }
  const x = histogram.x ?? histogram.centers ?? edges;
  if (Array.isArray(x) && Array.isArray(values) && x.length === values.length) {
    return values.map((value, index) => {
      const center = Number(x[index]);
      const leftNeighbor = index ? Number(x[index - 1]) : center - (Number(x[index + 1]) - center || 1);
      const rightNeighbor = index + 1 < x.length ? Number(x[index + 1]) : center + (center - Number(x[index - 1]) || 1);
      return {left: (leftNeighbor + center) / 2, right: (center + rightNeighbor) / 2, value: Number(value)};
    }).filter(item => Object.values(item).every(Number.isFinite));
  }
  return [];
}

function toggleOn(selector) {
  return $(selector)?.getAttribute("aria-pressed") === "true";
}

function drawChart() {
  const svg = $("#chart"), legend = $("#legend"), result = state.lastResult;
  if (!svg || !legend || !result) return;
  svg.replaceChildren();
  legend.replaceChildren();
  const series = curveSeries(result);
  if (!series.length) return;
  const width = Math.max(320, Math.round(svg.getBoundingClientRect().width || svg.clientWidth || 900));
  const compact = width < 520;
  const height = compact ? 350 : 420;
  const margin = {top: 24, right: compact ? 12 : 24, bottom: 53, left: compact ? 48 : 62};
  const innerW = Math.max(1, width - margin.left - margin.right);
  const innerH = height - margin.top - margin.bottom;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.setAttribute("aria-label", `Dichteverteilung von ${xDisplayLabel(result)} mit ${series.length} Segmenten`);

  const allX = series.flatMap(curve => curve.x || []).map(Number).filter(Number.isFinite);
  if (!allX.length) return;
  let fullMin = Math.min(...allX), fullMax = Math.max(...allX);
  if (fullMin === fullMax) {
    const padding = Math.max(Math.abs(fullMin) * 0.05, 1);
    fullMin -= padding;
    fullMax += padding;
  }
  const domain = state.chartDomain || [fullMin, fullMax];
  const xMin = Math.max(fullMin, Math.min(domain[0], domain[1]));
  const xMax = Math.min(fullMax, Math.max(domain[0], domain[1]));
  const visible = series.filter(curve => !state.hiddenSeries.has(curve.key));
  const yCandidates = visible.flatMap(curve => (curve.x || []).map((x, index) => Number(x) >= xMin && Number(x) <= xMax ? Number(curve.y?.[index]) : NaN)).filter(Number.isFinite);
  if (toggleOn("#toggle-histogram")) {
    visible.forEach(curve => histogramPoints(curve.histogram).forEach(bin => {
      if (bin.right >= xMin && bin.left <= xMax) yCandidates.push(bin.value);
    }));
  }
  const yMax = Math.max(...yCandidates, 1e-12) * 1.08;
  const sx = value => margin.left + (Number(value) - xMin) / (xMax - xMin || 1) * innerW;
  const sy = value => margin.top + innerH - Number(value) / yMax * innerH;
  const ns = "http://www.w3.org/2000/svg";
  const make = (tag, attrs = {}, parent = svg) => {
    const el = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
    parent.append(el);
    return el;
  };
  const defs = make("defs");
  const clip = make("clipPath", {id: "chart-clip"}, defs);
  make("rect", {x: margin.left, y: margin.top, width: innerW, height: innerH}, clip);
  const plot = make("g", {"clip-path": "url(#chart-clip)"});

  for (let index = 0; index <= 5; index++) {
    const y = margin.top + innerH * index / 5;
    make("line", {x1: margin.left, y1: y, x2: width - margin.right, y2: y, stroke: "#e8ecf1"});
    const text = make("text", {x: margin.left - 9, y: y + 4, "text-anchor": "end", fill: "#667085", "font-size": 10});
    text.textContent = formatAxis(yMax * (1 - index / 5));
  }
  const xTicks = compact ? 4 : 6;
  for (let index = 0; index <= xTicks; index++) {
    const x = margin.left + innerW * index / xTicks;
    make("line", {x1: x, y1: margin.top + innerH, x2: x, y2: margin.top + innerH + 5, stroke: "#aeb8c4"});
    const text = make("text", {x, y: height - 25, "text-anchor": "middle", fill: "#667085", "font-size": 10});
    text.textContent = formatAxis(xMin + (xMax - xMin) * index / xTicks);
  }
  make("line", {x1: margin.left, y1: margin.top + innerH, x2: width - margin.right, y2: margin.top + innerH, stroke: "#aeb8c4"});
  const label = make("text", {x: margin.left + innerW / 2, y: height - 3, "text-anchor": "middle", fill: "#475467", "font-size": 11, "font-weight": 700});
  label.textContent = xDisplayLabel(result);

  if (toggleOn("#toggle-histogram")) {
    visible.forEach(curve => {
      const index = series.indexOf(curve);
      histogramPoints(curve.histogram).forEach(bin => {
        const left = Math.max(margin.left, sx(bin.left));
        const right = Math.min(width - margin.right, sx(bin.right));
        make("rect", {x: left, y: sy(bin.value), width: Math.max(0, right - left - 0.5), height: Math.max(0, margin.top + innerH - sy(bin.value)), fill: colors[index % colors.length], opacity: visible.length === 1 ? 0.2 : 0.09}, plot);
      });
    });
  }

  const statByLabel = new Map(state.statistics.map(row => [String(row.segment), row]));
  if (toggleOn("#toggle-reference")) {
    visible.forEach(curve => {
      const index = series.indexOf(curve);
      const row = statByLabel.get(curve.label) || state.statistics[index];
      [["mean", "M"], ["median", "Md"]].forEach(([key, short], refIndex) => {
        if (!Number.isFinite(Number(row?.[key])) || Number(row[key]) < xMin || Number(row[key]) > xMax) return;
        const x = sx(row[key]);
        make("line", {x1: x, y1: margin.top, x2: x, y2: margin.top + innerH, stroke: colors[index % colors.length], "stroke-width": 1.2, "stroke-dasharray": refIndex ? "2 4" : "6 4", opacity: 0.65}, plot);
        const text = make("text", {x: x + 3, y: margin.top + 11 + refIndex * 11, fill: colors[index % colors.length], "font-size": 9}, plot);
        text.textContent = short;
      });
    });
  }

  visible.forEach(curve => {
    const index = series.indexOf(curve);
    const points = (curve.x || []).map((x, pointIndex) => [Number(x), Number(curve.y?.[pointIndex])]).filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y) && x >= xMin && x <= xMax);
    if (points.length === 1) {
      make("circle", {cx: sx(points[0][0]), cy: sy(points[0][1]), r: 3, fill: colors[index % colors.length]}, plot);
    } else if (points.length) {
      make("polyline", {points: points.map(([x, y]) => `${sx(x)},${sy(y)}`).join(" "), fill: "none", stroke: colors[index % colors.length], "stroke-width": 2.5, "stroke-dasharray": dashPatterns[index % dashPatterns.length], "stroke-linejoin": "round", "stroke-linecap": "round"}, plot);
    }
    if (toggleOn("#toggle-rug")) {
      const rug = curve.rug.length > 600 ? curve.rug.filter((_, rugIndex) => rugIndex % Math.ceil(curve.rug.length / 600) === 0) : curve.rug;
      rug.forEach(value => {
        if (value >= xMin && value <= xMax) make("line", {x1: sx(value), x2: sx(value), y1: margin.top + innerH - 9, y2: margin.top + innerH, stroke: colors[index % colors.length], opacity: 0.28}, plot);
      });
    }
  });

  series.forEach((curve, index) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "legend-item";
    item.setAttribute("aria-pressed", String(!state.hiddenSeries.has(curve.key)));
    item.setAttribute("aria-label", `${curve.label} ${state.hiddenSeries.has(curve.key) ? "einblenden" : "ausblenden"}`);
    item.style.opacity = state.hiddenSeries.has(curve.key) ? "0.45" : "1";
    item.style.border = "0";
    item.style.background = "transparent";
    item.style.cursor = "pointer";
    const swatch = document.createElement("i");
    swatch.className = "legend-swatch";
    swatch.style.background = "transparent";
    swatch.style.borderTop = `3px ${dashPatterns[index % dashPatterns.length] ? "dashed" : "solid"} ${colors[index % colors.length]}`;
    item.append(swatch, document.createTextNode(curve.label));
    item.addEventListener("click", () => {
      if (state.hiddenSeries.has(curve.key)) state.hiddenSeries.delete(curve.key);
      else state.hiddenSeries.add(curve.key);
      drawChart();
    });
    legend.append(item);
  });

  const selection = make("rect", {x: 0, y: margin.top, width: 0, height: innerH, fill: "#0072B2", opacity: 0.14, "pointer-events": "none"});
  const overlay = make("rect", {x: margin.left, y: margin.top, width: innerW, height: innerH, fill: "transparent", cursor: "crosshair"});
  wireChartPointer(overlay, selection, {svg, series: visible, sx, sy, xMin, xMax, margin, innerW, innerH});
  updateChartControlAvailability(series);
  renderChartSummary(series, [xMin, xMax]);
}

function formatAxis(value) {
  const absolute = Math.abs(value);
  if ((absolute > 0 && absolute < 0.001) || absolute >= 1e6) return value.toExponential(1);
  return value.toLocaleString("de-DE", {maximumFractionDigits: absolute < 10 ? 3 : 2});
}

function wireChartPointer(overlay, selection, chart) {
  let startX = null;
  let currentX = null;
  const tooltip = $("#chart-tooltip");
  const coordinate = event => {
    const rect = chart.svg.getBoundingClientRect();
    return Math.max(chart.margin.left, Math.min(chart.margin.left + chart.innerW, (event.clientX - rect.left) * chart.svg.viewBox.baseVal.width / rect.width));
  };
  const domainValue = pixel => chart.xMin + (pixel - chart.margin.left) / chart.innerW * (chart.xMax - chart.xMin);
  const showTooltip = event => {
    if (!tooltip || startX !== null) return;
    const px = coordinate(event);
    const xValue = domainValue(px);
    const values = chart.series.map(curve => {
      let best = -1, distance = Infinity;
      (curve.x || []).forEach((value, index) => {
        const candidate = Math.abs(Number(value) - xValue);
        if (candidate < distance) { distance = candidate; best = index; }
      });
      return best < 0 ? null : {label: curve.label, x: Number(curve.x[best]), y: Number(curve.y[best])};
    }).filter(Boolean).slice(0, 12);
    tooltip.replaceChildren();
    const strong = document.createElement("strong");
    strong.textContent = `${xDisplayLabel()}: ${formatAxis(values[0]?.x ?? xValue)}`;
    tooltip.append(strong);
    values.forEach(item => {
      const row = document.createElement("span");
      row.textContent = `${item.label}: ${formatAxis(item.y)}`;
      tooltip.append(row);
    });
    const rect = chart.svg.getBoundingClientRect();
    tooltip.style.left = `${Math.min(rect.width - 180, Math.max(4, event.clientX - rect.left + 12))}px`;
    tooltip.style.top = `${Math.max(4, event.clientY - rect.top - 20)}px`;
    tooltip.hidden = false;
  };
  overlay.addEventListener("pointerdown", event => {
    startX = coordinate(event);
    currentX = startX;
    overlay.setPointerCapture?.(event.pointerId);
    if (tooltip) tooltip.hidden = true;
  });
  overlay.addEventListener("pointermove", event => {
    if (startX === null) {
      showTooltip(event);
      return;
    }
    currentX = coordinate(event);
    selection.setAttribute("x", Math.min(startX, currentX));
    selection.setAttribute("width", Math.abs(currentX - startX));
  });
  const finish = event => {
    if (startX === null) return;
    currentX = coordinate(event);
    const distance = Math.abs(currentX - startX);
    if (distance > 12) {
      state.chartDomain = [domainValue(Math.min(startX, currentX)), domainValue(Math.max(startX, currentX))];
      drawChart();
    } else {
      selection.setAttribute("width", 0);
    }
    startX = null;
  };
  overlay.addEventListener("pointerup", finish);
  overlay.addEventListener("pointercancel", () => {
    startX = null;
    selection.setAttribute("width", 0);
  });
  overlay.addEventListener("pointerleave", () => {
    if (tooltip && startX === null) tooltip.hidden = true;
  });
}

function updateChartControlAvailability(series) {
  const histogram = $("#toggle-histogram");
  const rug = $("#toggle-rug");
  if (histogram) {
    histogram.disabled = !series.some(curve => histogramPoints(curve.histogram).length);
    histogram.title = histogram.disabled ? "Für dieses Ergebnis sind keine Histogrammdaten vorhanden." : "Histogramm ein- oder ausblenden";
  }
  if (rug) {
    rug.disabled = !series.some(curve => curve.rug.length);
    rug.title = rug.disabled ? "Für dieses Ergebnis ist keine Rohdatenstichprobe vorhanden." : "Rug-Plot ein- oder ausblenden";
  }
  const reset = $("#reset-zoom");
  if (reset) reset.disabled = !state.chartDomain;
}

function renderChartSummary(series, domain) {
  const root = $("#chart-summary");
  if (!root) return;
  root.replaceChildren();
  const visible = series.filter(curve => !state.hiddenSeries.has(curve.key));
  const intro = document.createElement("p");
  const segmentNames = (state.lastResult?.segment_columns || []).map(item => item.display_name || displayColumn(item.name)).filter(Boolean);
  intro.textContent = `${visible.length} von ${series.length} Segmenten sichtbar${segmentNames.length ? ` (${segmentNames.join(" × ")})` : ""}. X-Bereich ${formatAxis(domain[0])} bis ${formatAxis(domain[1])}. Ziehen Sie im Diagramm, um einen Bereich zu vergrößern.`;
  root.append(intro);
  const list = document.createElement("ul");
  state.statistics.slice(0, 12).forEach(row => {
    const item = document.createElement("li");
    item.textContent = `${row.segment}: n = ${format(row.count, "count")}, Median ${format(row.median, "median")}, Mittelwert ${format(row.mean, "mean")}${row.modality ? `, ${row.modality}` : ""}`;
    list.append(item);
  });
  root.append(list);
}

function format(value, key) {
  if (value === null || value === undefined || (typeof value === "number" && !Number.isFinite(value))) return "–";
  if (key === "count") return Number(value).toLocaleString("de-DE");
  if (typeof value === "number") {
    const precise = ["std", "variance", "skew", "kurtosis", "density_mode"].includes(key);
    return value.toLocaleString("de-DE", {minimumFractionDigits: precise ? 4 : 2, maximumFractionDigits: 4});
  }
  return String(value);
}

function formatStatistic(statistic, key) {
  if (key === "mode" && statistic?.mode_tied) {
    const values = Array.isArray(statistic.mode_values) ? statistic.mode_values : [];
    const visibleValues = values.slice(0, 8).map(value => format(value, "mode"));
    const suffix = values.length > visibleValues.length ? `; +${values.length - visibleValues.length} weitere` : "";
    return visibleValues.length ? `Mehrdeutig (${visibleValues.join("; ")}${suffix})` : "Mehrdeutig";
  }
  return format(statistic?.[key], key);
}

function sortedStatistics() {
  const rows = [...state.statistics], {key, direction} = state.sort;
  if (!key) return rows;
  return rows.sort((a, b) => {
    const av = a[key], bv = b[key];
    if (av == null && bv != null) return 1;
    if (bv == null && av != null) return -1;
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * direction;
    return String(av ?? "").localeCompare(String(bv ?? ""), "de", {numeric: true}) * direction;
  });
}

function renderTable() {
  const head = $("#stats-table thead"), body = $("#stats-table tbody");
  if (!head || !body) return;
  head.replaceChildren();
  body.replaceChildren();
  const row = document.createElement("tr");
  columns.forEach(([key, label]) => {
    const th = document.createElement("th");
    th.scope = "col";
    if (state.sort.key === key) th.setAttribute("aria-sort", state.sort.direction === 1 ? "ascending" : "descending");
    else th.setAttribute("aria-sort", "none");
    const control = document.createElement("button");
    control.type = "button";
    control.className = "table-sort-button";
    control.textContent = label + (state.sort.key === key ? (state.sort.direction === 1 ? " ▲" : " ▼") : "");
    control.addEventListener("click", () => {
      state.sort = state.sort.key === key ? {key, direction: state.sort.direction * -1} : {key, direction: 1};
      renderTable();
    });
    th.append(control);
    row.append(th);
  });
  head.append(row);
  sortedStatistics().forEach(statistic => {
    const tr = document.createElement("tr");
    columns.forEach(([key]) => {
      const td = document.createElement("td");
      td.textContent = formatStatistic(statistic, key);
      tr.append(td);
    });
    body.append(tr);
  });
}

function tsvCell(value) {
  const text = String(value ?? "");
  const trimmed = text.trimStart();
  const formulaLike = /^[=+\-@]/.test(trimmed) || /^[\t\r]/.test(text);
  const safeText = formulaLike ? `'${text}` : text;
  return /[\t\r\n"]/.test(safeText) ? `"${safeText.replaceAll('"', '""')}"` : safeText;
}

function tableTsv() {
  const metadata = [
    ["Filter", state.lastFilterSummary],
    ...reproducibilityEntries()
  ].map(([key, value]) => `${tsvCell(key)}\t${tsvCell(value)}`);
  const header = columns.map(column => tsvCell(column[1])).join("\t");
  const rows = sortedStatistics().map(row => columns.map(([key]) => tsvCell(formatStatistic(row, key))).join("\t"));
  return [...metadata, "", header, ...rows].join("\n");
}

function downloadBlob(blob, filename) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 0);
}

function downloadTable() {
  downloadBlob(new Blob([`\ufeff${tableTsv()}`], {type: "text/tab-separated-values;charset=utf-8"}), "histo-maker-statistik.tsv");
}

function exportedSvgText() {
  const source = $("#chart");
  if (!source) throw new Error("Es ist kein Diagramm zum Exportieren vorhanden.");
  const clone = source.cloneNode(true);
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  const width = source.viewBox.baseVal.width || 1200;
  const chartHeight = source.viewBox.baseVal.height || 420;
  clone.setAttribute("width", width);
  clone.querySelectorAll("rect[fill='transparent']").forEach(node => node.remove());
  const visibleSeries = curveSeries().filter(curve => !state.hiddenSeries.has(curve.key));
  const legend = document.createElementNS("http://www.w3.org/2000/svg", "g");
  legend.setAttribute("data-export-legend", "true");
  legend.setAttribute("font-family", "system-ui, -apple-system, Segoe UI, sans-serif");
  const heading = document.createElementNS("http://www.w3.org/2000/svg", "text");
  heading.setAttribute("x", "20");
  heading.setAttribute("y", String(chartHeight + 25));
  heading.setAttribute("fill", "#344054");
  heading.setAttribute("font-size", "12");
  heading.setAttribute("font-weight", "700");
  heading.textContent = "Segmente";
  legend.append(heading);
  const maxChars = Math.max(24, Math.floor((width - 76) / 7));
  let legendY = chartHeight + 48;
  visibleSeries.forEach(curve => {
    const index = curveSeries().findIndex(candidate => candidate.key === curve.key);
    const label = String(curve.label);
    const chunks = [];
    for (let offset = 0; offset < label.length; offset += maxChars) chunks.push(label.slice(offset, offset + maxChars));
    if (!chunks.length) chunks.push("–");
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", "20");
    line.setAttribute("x2", "43");
    line.setAttribute("y1", String(legendY - 4));
    line.setAttribute("y2", String(legendY - 4));
    line.setAttribute("stroke", colors[index % colors.length]);
    line.setAttribute("stroke-width", "3");
    if (dashPatterns[index % dashPatterns.length]) line.setAttribute("stroke-dasharray", dashPatterns[index % dashPatterns.length]);
    legend.append(line);
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", "51");
    text.setAttribute("y", String(legendY));
    text.setAttribute("fill", "#475467");
    text.setAttribute("font-size", "11");
    text.setAttribute("data-segment-label", label);
    text.setAttribute("aria-label", label);
    chunks.forEach((chunk, chunkIndex) => {
      const tspan = document.createElementNS("http://www.w3.org/2000/svg", "tspan");
      tspan.setAttribute("x", "51");
      tspan.setAttribute("dy", chunkIndex ? "15" : "0");
      tspan.textContent = chunk;
      text.append(tspan);
    });
    legend.append(text);
    legendY += Math.max(21, chunks.length * 15 + 5);
  });
  clone.append(legend);
  const totalHeight = Math.ceil(legendY + 8);
  clone.setAttribute("height", totalHeight);
  clone.setAttribute("viewBox", `0 0 ${width} ${totalHeight}`);
  const background = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  background.setAttribute("width", "100%");
  background.setAttribute("height", "100%");
  background.setAttribute("fill", "white");
  clone.prepend(background);
  const metadata = document.createElementNS("http://www.w3.org/2000/svg", "metadata");
  metadata.textContent = JSON.stringify(Object.fromEntries(reproducibilityEntries()));
  clone.prepend(metadata);
  return `<?xml version="1.0" encoding="UTF-8"?>\n${new XMLSerializer().serializeToString(clone)}`;
}

function exportSvg() {
  try {
    downloadBlob(new Blob([exportedSvgText()], {type: "image/svg+xml;charset=utf-8"}), "histo-maker-diagramm.svg");
  } catch (error) {
    toast(error.message, true);
  }
}

async function exportPng() {
  try {
    const svgText = exportedSvgText();
    const exportedRoot = new DOMParser().parseFromString(svgText, "image/svg+xml").documentElement;
    const width = Number(exportedRoot.getAttribute("width")) || 1200;
    const height = Number(exportedRoot.getAttribute("height")) || 420;
    const scale = Math.max(2, window.devicePixelRatio || 1);
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    const context = canvas.getContext("2d");
    context.scale(scale, scale);
    context.fillStyle = "white";
    context.fillRect(0, 0, width, height);
    const image = new Image();
    const url = URL.createObjectURL(new Blob([svgText], {type: "image/svg+xml"}));
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("Das Diagramm konnte nicht in PNG umgewandelt werden."));
      image.src = url;
    });
    context.drawImage(image, 0, 0, width, height);
    URL.revokeObjectURL(url);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
    if (!blob) throw new Error("PNG-Export wird von diesem Browser nicht unterstützt.");
    downloadBlob(blob, "histo-maker-diagramm.png");
  } catch (error) {
    toast(error.message, true);
  }
}

function base64urlFromBytes(bytes) {
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function bytesFromBase64url(value) {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("Der Freigabelink enthält ungültige Zeichen.");
  const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - value.length % 4) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, character => character.charCodeAt(0));
}

async function compressText(value) {
  if (!("CompressionStream" in window)) throw new Error("Dieser Browser unterstützt keine komprimierten Freigabelinks.");
  const stream = new Blob([new TextEncoder().encode(value)]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function decompressBytes(compressed) {
  if (!("DecompressionStream" in window)) throw new Error("Dieser Browser kann den komprimierten Freigabelink nicht öffnen.");
  if (compressed.byteLength > MAX_SHARE_LINK_LENGTH) throw new Error("Der Freigabelink ist zu groß.");
  const reader = new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip")).getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const {value: chunk, done} = await reader.read();
    if (done) break;
    total += chunk.byteLength;
    if (total > MAX_SHARED_JSON_BYTES) {
      await reader.cancel();
      throw new Error("Die entpackten Freigabedaten überschreiten das Sicherheitslimit.");
    }
    chunks.push(chunk);
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  chunks.forEach(chunk => { joined.set(chunk, offset); offset += chunk.byteLength; });
  return new TextDecoder("utf-8", {fatal: true}).decode(joined);
}

async function decompressText(value) {
  return decompressBytes(bytesFromBase64url(value));
}

async function sha256Base64url(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return base64urlFromBytes(new Uint8Array(digest));
}

async function deriveShareKey(password, salt, usages) {
  const material = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    {name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256"},
    material,
    {name: "AES-GCM", length: 256},
    false,
    usages
  );
}

async function encryptCompressed(compressed, password) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await deriveShareKey(password, salt, ["encrypt"]);
  const ciphertext = await crypto.subtle.encrypt({name: "AES-GCM", iv}, key, compressed);
  const envelope = {
    v: 1,
    kdf: "PBKDF2-SHA256",
    iterations: PBKDF2_ITERATIONS,
    cipher: "AES-256-GCM",
    salt: base64urlFromBytes(salt),
    iv: base64urlFromBytes(iv),
    ciphertext: base64urlFromBytes(new Uint8Array(ciphertext))
  };
  return ENCRYPTED_SHARE_PREFIX + base64urlFromBytes(new TextEncoder().encode(JSON.stringify(envelope)));
}

async function decryptCompressed(value, password) {
  let envelope;
  try {
    envelope = JSON.parse(new TextDecoder().decode(bytesFromBase64url(value)));
  } catch {
    throw new Error("Der verschlüsselte Freigabelink ist beschädigt.");
  }
  if (envelope.v !== 1 || envelope.kdf !== "PBKDF2-SHA256" || envelope.cipher !== "AES-256-GCM" || envelope.iterations !== PBKDF2_ITERATIONS) {
    throw new Error("Das Verschlüsselungsformat dieses Links wird nicht unterstützt.");
  }
  try {
    const salt = bytesFromBase64url(envelope.salt), iv = bytesFromBase64url(envelope.iv);
    if (salt.length !== 16 || iv.length !== 12) throw new Error();
    const key = await deriveShareKey(password, salt, ["decrypt"]);
    const plaintext = await crypto.subtle.decrypt({name: "AES-GCM", iv}, key, bytesFromBase64url(envelope.ciphertext));
    return new Uint8Array(plaintext);
  } catch {
    throw new Error("Das Passwort ist falsch oder der Link wurde verändert.");
  }
}

function requestSharePassword(previousError = "") {
  const dialog = $("#unlock-dialog"), input = $("#unlock-password"), submit = $("#unlock-share"), error = $("#unlock-error");
  if (!dialog || !input || !submit || typeof dialog.showModal !== "function") {
    const password = window.prompt("Dieser Freigabelink ist verschlüsselt. Bitte Passwort eingeben:");
    if (password === null) return Promise.reject(new Error("Öffnen des verschlüsselten Links wurde abgebrochen."));
    return Promise.resolve(password);
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    input.value = "";
    if (error) { error.hidden = !previousError; error.textContent = previousError; }
    const finish = () => {
      settled = true;
      cleanup();
      dialog.close();
      resolve(input.value);
    };
    const cancel = event => {
      if (event.target === submit) return;
      cleanup();
      reject(new Error("Öffnen des verschlüsselten Links wurde abgebrochen."));
    };
    const keydown = event => {
      if (event.key === "Enter") { event.preventDefault(); finish(); }
    };
    const cleanup = () => {
      submit.removeEventListener("click", finish);
      dialog.removeEventListener("cancel", cancel);
      dialog.removeEventListener("close", closed);
      input.removeEventListener("keydown", keydown);
    };
    const closed = () => {
      if (settled) return;
      cleanup();
      reject(new Error("Öffnen des verschlüsselten Links wurde abgebrochen."));
    };
    submit.addEventListener("click", finish);
    dialog.addEventListener("cancel", cancel, {once: true});
    dialog.addEventListener("close", closed, {once: true});
    input.addEventListener("keydown", keydown);
    dialog.showModal();
    input.focus();
  });
}

async function getShareVerificationKey(expectedKeyId) {
  const response = await fetch("/api/share-key", {cache: "no-store"});
  if (!response.ok) throw new Error("Der Signaturschlüssel konnte nicht geladen werden.");
  const keyInfo = await response.json();
  if (keyInfo.algorithm && keyInfo.algorithm !== "Ed25519") throw new Error("Die Signatur verwendet einen unbekannten Algorithmus.");
  const candidates = [];
  if (keyInfo.key_id && keyInfo.public_key) candidates.push(keyInfo);
  if (Array.isArray(keyInfo.keys)) candidates.push(...keyInfo.keys);
  else if (keyInfo.keys && typeof keyInfo.keys === "object") Object.entries(keyInfo.keys).forEach(([key_id, value]) => candidates.push(typeof value === "string" ? {key_id, public_key: value} : {key_id, ...value}));
  if (keyInfo.keyring && typeof keyInfo.keyring === "object") Object.entries(keyInfo.keyring).forEach(([key_id, value]) => candidates.push(typeof value === "string" ? {key_id, public_key: value} : {key_id, ...value}));
  const match = candidates.find(candidate => candidate.key_id === expectedKeyId);
  if (!match?.public_key) throw new Error("Der Link wurde nicht von dieser Histo-Maker-Installation signiert oder sein Schlüssel ist nicht mehr verfügbar.");
  try {
    return await crypto.subtle.importKey("raw", bytesFromBase64url(match.public_key), {name: "Ed25519"}, false, ["verify"]);
  } catch {
    throw new Error("Dieser Browser kann Ed25519-Signaturen nicht prüfen.");
  }
}

async function verifyText(key, value, signature) {
  return crypto.subtle.verify({name: "Ed25519"}, key, bytesFromBase64url(signature), new TextEncoder().encode(value));
}

function validateSharedResult(value) {
  const result = value?.result;
  if (![1, 2].includes(value?.v) || !result || typeof result.x_label !== "string" || result.x_label.length > 500) throw new Error("Das signierte Ergebnisformat ist ungültig.");
  if (!Number.isInteger(result.source_rows) || result.source_rows < 0 || !Array.isArray(result.curves) || !Array.isArray(result.statistics)) throw new Error("Das signierte Ergebnis ist unvollständig.");
  for (const key of ["plotted_rows", "omitted_small_group_count", "omitted_small_group_rows"]) {
    if (result[key] !== undefined && (!Number.isInteger(result[key]) || result[key] < 0)) throw new Error("Die signierten Abdeckungsangaben sind ungültig.");
  }
  if (result.plotted_rows !== undefined && result.plotted_rows > result.source_rows) throw new Error("Die signierten Abdeckungsangaben sind widersprüchlich.");
  if (!result.curves.length || result.curves.length > MAX_CURVES || result.statistics.length !== result.curves.length) throw new Error("Das signierte Ergebnis enthält eine ungültige Anzahl an Segmenten.");
  result.curves.forEach(curve => {
    if (typeof curve.label !== "string" || curve.label.length > 500 || !Array.isArray(curve.x) || !Array.isArray(curve.y) || curve.x.length !== curve.y.length || !curve.x.length || curve.x.length > 1000) throw new Error("Eine signierte Kurve ist ungültig.");
    if (![...curve.x, ...curve.y].every(Number.isFinite)) throw new Error("Eine signierte Kurve enthält ungültige Zahlen.");
  });
  result.statistics.forEach(row => {
    if (!row || typeof row.segment !== "string" || row.segment.length > 500 || !Number.isInteger(row.count) || row.count < 0) throw new Error("Eine signierte Statistikzeile ist ungültig.");
  });
  return result;
}

function expiryFromPayload(payload) {
  const raw = payload?.expires_at ?? payload?.expiry ?? payload?.expires;
  if (!raw) return null;
  const date = new Date(raw);
  if (!Number.isFinite(date.getTime())) throw new Error("Das signierte Hinweisdatum des Freigabelinks ist ungültig.");
  return date;
}

function shareEnvelope(includeContext = false) {
  const material = state.shareMaterial;
  const envelope = {v: 1, algorithm: material.algorithm, key_id: material.key_id, payload: material.payload, signature: material.signature};
  if (includeContext && material.context_payload) {
    envelope.context_payload = material.context_payload;
    envelope.context_signature = material.context_signature;
  }
  return envelope;
}

function smallestSharedGroup() {
  return state.statistics.length ? Math.min(...state.statistics.map(row => row.count)) : 0;
}

async function updateSharePreflight() {
  if (!state.shareMaterial) return;
  const requestId = Symbol("share-preflight");
  state.sharePreflightRequest = requestId;
  const note = $("#share-expiry-note");
  const warning = $("#share-small-group-warning");
  const blocked = smallestSharedGroup() < MIN_SHARED_GROUP_SIZE;
  const baseNote = note?.dataset.baseText || note?.textContent || "";
  try {
    const includeContext = Boolean($("#share-filter")?.checked);
    const passwordProtected = Boolean($("#share-password")?.value);
    const compressed = await compressText(JSON.stringify(shareEnvelope(includeContext)));
    if (state.sharePreflightRequest !== requestId) return;
    const plainLength = Math.ceil(compressed.length * 4 / 3);
    const encodedLength = passwordProtected
      ? Math.ceil((Math.ceil((compressed.length + 16) * 4 / 3) + 190) * 4 / 3) + ENCRYPTED_SHARE_PREFIX.length
      : plainLength;
    const projectedLength = location.origin.length + location.pathname.length + SHARE_PREFIX.length + encodedLength;
    const tooLarge = projectedLength > MAX_SHARE_LINK_LENGTH;
    state.sharePreflight = {compressed, projectedLength, includeContext, passwordProtected};
    if (note) note.textContent = `${baseNote} Voraussichtliche Linkgröße: ${(projectedLength / 1024).toFixed(1)} KB${passwordProtected ? " (verschlüsselt)" : ""}.`;
    if (tooLarge && warning) {
      warning.hidden = false;
      warning.textContent = "Das Ergebnis ist für einen zuverlässigen Freigabelink zu groß. Bitte weniger Segmente verwenden oder die TSV-Datei exportieren.";
    } else if (!blocked && warning) {
      warning.hidden = true;
      warning.textContent = "";
    }
    if ($("#create-share-link")) $("#create-share-link").disabled = blocked || tooLarge;
  } catch (error) {
    if (state.sharePreflightRequest !== requestId) return;
    if (note) note.textContent = `${baseNote} Größenprüfung nicht möglich: ${error.message}`;
    if ($("#create-share-link")) $("#create-share-link").disabled = true;
  }
}

async function openShareDialog() {
  if (!state.lastResult || !state.shareMaterial) return;
  const minimum = smallestSharedGroup();
  const blocked = minimum < MIN_SHARED_GROUP_SIZE;
  const warning = $("#share-small-group-warning");
  if (warning) {
    warning.hidden = !blocked;
    warning.textContent = blocked ? `Freigabe gesperrt: Mindestens ein Segment enthält nur ${minimum} Beobachtungen. Signierte Links sind erst ab n = ${MIN_SHARED_GROUP_SIZE} je Segment möglich.` : "";
  }
  if ($("#share-confirm")) $("#share-confirm").checked = false;
  if ($("#share-filter")) {
    $("#share-filter").checked = false;
    $("#share-filter").disabled = !state.shareMaterial.context_payload;
  }
  if ($("#share-password")) $("#share-password").value = "";
  if ($("#share-link-wrap")) $("#share-link-wrap").hidden = true;
  const note = $("#share-expiry-note");
  try {
    const signed = JSON.parse(state.shareMaterial.payload);
    const expiry = expiryFromPayload(signed);
    if (note) note.textContent = expiry ? `Lokale Öffnungsfrist bis ${expiry.toLocaleString("de-DE")}; der Linkinhalt wird nicht gelöscht oder widerrufen.` : "Dieser Link hat keine lokale Öffnungsfrist und bleibt bis zu seiner Löschung beim Empfänger lesbar.";
  } catch {
    if (note) note.textContent = "Das signierte Hinweisdatum konnte nicht gelesen werden; der Linkinhalt wird dadurch nicht widerrufen.";
  }
  if (note) note.dataset.baseText = note.textContent;
  if ($("#create-share-link")) $("#create-share-link").disabled = true;
  $("#share-dialog")?.showModal();
  await updateSharePreflight();
}

async function createShareLink() {
  if (!$("#share-confirm")?.checked) {
    toast("Bitte den Vertraulichkeitshinweis bestätigen.", true);
    return;
  }
  if (smallestSharedGroup() < MIN_SHARED_GROUP_SIZE) return;
  const includeContext = Boolean($("#share-filter")?.checked);
  const password = $("#share-password")?.value || "";
  if (password && password.length < 8) {
    toast("Das optionale Link-Passwort muss mindestens acht Zeichen lang sein.", true);
    return;
  }
  try {
    const compressed = await compressText(JSON.stringify(shareEnvelope(includeContext)));
    const encoded = password ? await encryptCompressed(compressed, password) : base64urlFromBytes(compressed);
    const url = `${location.origin}${location.pathname}${SHARE_PREFIX}${encoded}`;
    if (url.length > MAX_SHARE_LINK_LENGTH) throw new Error("Das Ergebnis ist für einen zuverlässigen Freigabelink zu groß. Bitte weniger Segmente verwenden oder die TSV-Datei exportieren.");
    if ($("#share-link")) $("#share-link").value = url;
    if ($("#share-link-wrap")) $("#share-link-wrap").hidden = false;
    try {
      await navigator.clipboard.writeText(url);
      toast(password ? "Verschlüsselter, signierter Freigabelink wurde kopiert." : "Signierter Freigabelink wurde kopiert.");
    } catch {
      toast("Freigabelink wurde erstellt. Bitte kopieren Sie ihn aus dem Textfeld.");
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function serializedEnvelopeFromHash(fragment) {
  if (fragment.length > MAX_SHARE_LINK_LENGTH) throw new Error("Der Freigabelink ist zu groß.");
  if (!fragment.startsWith(ENCRYPTED_SHARE_PREFIX)) return decompressText(fragment);
  const encrypted = fragment.slice(ENCRYPTED_SHARE_PREFIX.length);
  let previousError = "";
  while (true) {
    const password = await requestSharePassword(previousError);
    try {
      return await decompressBytes(await decryptCompressed(encrypted, password));
    } catch (error) {
      previousError = error.message;
      if (!$("#unlock-dialog")) throw error;
      toast(error.message, true);
    }
  }
}

async function loadSharedLink() {
  if (!location.hash.startsWith(SHARE_PREFIX)) return;
  loading(true, "Signatur wird geprüft …");
  try {
    const serialized = await serializedEnvelopeFromHash(location.hash.slice(SHARE_PREFIX.length));
    const envelope = JSON.parse(serialized);
    if (envelope.v !== 1 || envelope.algorithm !== "Ed25519" || typeof envelope.payload !== "string" || typeof envelope.signature !== "string") throw new Error("Der Freigabelink hat ein unbekanntes Format.");
    const key = await getShareVerificationKey(envelope.key_id);
    if (!await verifyText(key, envelope.payload, envelope.signature)) throw new Error("Signatur ungültig: Die Ergebnisdaten wurden verändert.");
    const signedPayload = JSON.parse(envelope.payload);
    const expiry = expiryFromPayload(signedPayload);
    if (expiry && expiry.getTime() <= Date.now()) throw new Error(`Die lokal geprüfte Öffnungsfrist endete am ${expiry.toLocaleString("de-DE")}. Der Inhalt bleibt im Link und wurde nicht serverseitig widerrufen.`);
    const result = validateSharedResult(signedPayload);
    if (signedPayload.reproducibility && !result.reproducibility) result.reproducibility = signedPayload.reproducibility;
    if (signedPayload.app_version && !result.app_version) result.app_version = signedPayload.app_version;
    let filterSummary = "Nicht mitgeteilt";
    if (envelope.context_payload || envelope.context_signature) {
      if (typeof envelope.context_payload !== "string" || typeof envelope.context_signature !== "string" || !await verifyText(key, envelope.context_payload, envelope.context_signature)) throw new Error("Die signierten Filterangaben sind ungültig.");
      const context = JSON.parse(envelope.context_payload);
      if (context.v !== 1 || context.result_digest !== await sha256Base64url(envelope.payload) || typeof context.filter_summary !== "string" || context.filter_summary.length > 10000) throw new Error("Die Filterangaben gehören nicht zu diesem Ergebnis.");
      filterSummary = context.filter_summary;
    }
    state.lastResult = result;
    state.columnConfig = result.column_config && typeof result.column_config === "object" ? result.column_config : {};
    state.statistics = result.statistics;
    state.lastFilterSummary = filterSummary;
    state.shareMaterial = {algorithm: envelope.algorithm, key_id: envelope.key_id, payload: envelope.payload, signature: envelope.signature, context_payload: envelope.context_payload || null, context_signature: envelope.context_signature || null};
    state.sort = {key: null, direction: 1};
    state.hiddenSeries.clear();
    state.chartDomain = null;
    document.body.classList.add("shared-mode");
    if ($("#upload-view")) $("#upload-view").hidden = true;
    if ($("#workspace")) $("#workspace").hidden = false;
    if ($("#shared-banner")) $("#shared-banner").hidden = false;
    const created = signedPayload.created_at ? new Date(signedPayload.created_at).toLocaleString("de-DE") : "unbekannt";
    setText("#shared-verification", `Ed25519-Signatur gültig · Schlüssel ${envelope.key_id} · erstellt ${created}${expiry ? ` · lokale Öffnungsfrist bis ${expiry.toLocaleString("de-DE")}` : ""}`);
    setText("#chart-title", `Verteilung von ${xDisplayLabel(result)}`);
    setText("#result-count", resultCoverageText(result));
    if ($("#empty-chart")) $("#empty-chart").hidden = true;
    if ($("#chart-wrap")) $("#chart-wrap").hidden = false;
    if ($("#chart-toolbar")) $("#chart-toolbar").hidden = false;
    if ($("#stats-card")) $("#stats-card").hidden = false;
    renderReproducibility();
    drawChart();
    renderTable();
    toast("Signatur geprüft: Ergebnis ist unverändert.");
  } catch (error) {
    history.replaceState(null, "", location.pathname + location.search);
    toast(error.message, true);
  } finally {
    loading(false);
  }
}

function wireToggle(selector) {
  const control = $(selector);
  if (!control) return;
  if (!control.hasAttribute("aria-pressed")) control.setAttribute("aria-pressed", "false");
  control.addEventListener("click", () => {
    control.setAttribute("aria-pressed", String(control.getAttribute("aria-pressed") !== "true"));
    drawChart();
  });
}

function wireEvents() {
  $("#file-input")?.addEventListener("change", event => loadFile(event.target.files[0]));
  $("#change-file")?.addEventListener("click", () => $("#file-input")?.click());
  const drop = $("#drop-zone");
  if (drop) {
    ["dragenter", "dragover"].forEach(type => drop.addEventListener(type, event => { event.preventDefault(); drop.classList.add("drag"); }));
    ["dragleave", "drop"].forEach(type => drop.addEventListener(type, event => { event.preventDefault(); drop.classList.remove("drag"); }));
    drop.addEventListener("drop", event => loadFile(event.dataTransfer.files[0]));
    drop.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) $("#file-input")?.click(); });
  }
  $("#reinspect-button")?.addEventListener("click", () => inspectData({forceFile: false}));
  $("#analyze-button")?.addEventListener("click", analyze);
  $("#cancel-analysis")?.addEventListener("click", () => state.activeAbort?.abort());
  $("#download-table")?.addEventListener("click", downloadTable);
  $("#copy-table")?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(tableTsv());
    toast("Statistik mit Filter- und Methodenangaben als TSV kopiert.");
  });
  $("#share-result")?.addEventListener("click", openShareDialog);
  $("#create-share-link")?.addEventListener("click", createShareLink);
  $("#share-filter")?.addEventListener("change", updateSharePreflight);
  $("#share-password")?.addEventListener("input", () => {
    clearTimeout(state.sharePreflightTimer);
    state.sharePreflightTimer = setTimeout(updateSharePreflight, 180);
  });
  $("#new-analysis")?.addEventListener("click", () => { location.href = location.pathname; });
  $("#export-svg")?.addEventListener("click", exportSvg);
  $("#export-png")?.addEventListener("click", exportPng);
  $("#reset-zoom")?.addEventListener("click", () => { state.chartDomain = null; drawChart(); });
  ["#toggle-histogram", "#toggle-rug", "#toggle-reference"].forEach(wireToggle);
  [$("#hue-1"), $("#hue-2")].forEach(control => control?.addEventListener("change", () => {
    preventDuplicateSegments(control);
    updateSegmentEstimate();
    scheduleEstimate();
  }));
  $("#x-column")?.addEventListener("change", scheduleEstimate);
  const bandwidth = $("#bandwidth");
  if (bandwidth) {
    const update = () => setText("#bandwidth-value", `${Number(bandwidth.value).toLocaleString("de-DE", {maximumFractionDigits: 2})}×`);
    bandwidth.addEventListener("input", update);
    update();
  }
  window.addEventListener("resize", () => {
    clearTimeout(window.resizeTimer);
    window.resizeTimer = setTimeout(() => {
      if (state.lastResult && !$("#chart-wrap")?.hidden) drawChart();
    }, 150);
  });
}

function wirePwa() {
  let installPrompt;
  window.addEventListener("beforeinstallprompt", event => {
    event.preventDefault();
    installPrompt = event;
    if ($("#install-button")) $("#install-button").hidden = false;
  });
  $("#install-button")?.addEventListener("click", async () => {
    if (!installPrompt) return;
    installPrompt.prompt();
    await installPrompt.userChoice;
    installPrompt = null;
    $("#install-button").hidden = true;
  });
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").then(registration => {
      registration.addEventListener("updatefound", () => {
        const worker = registration.installing;
        worker?.addEventListener("statechange", () => {
          if (worker.state === "installed" && navigator.serviceWorker.controller) toast("Neue Version verfügbar – bitte Seite neu laden.");
        });
      });
    }).catch(() => {});
  }
}

wireEvents();
wirePwa();
loadSharedLink();
