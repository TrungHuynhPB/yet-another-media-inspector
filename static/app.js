let sessionId = null;
let brandGroups = [];
let uncertainItems = [];
let unavailableMedia = null;
let mode = "unavailable"; // unavailable | uncertain | brand
let cursor = 0;
let reviewing = false;
let contextItem = null;
let uploadedFilename = "";

const RING_CIRCUMFERENCE = 2 * Math.PI * 52;

/** Vercel serverless body limit (~4.5 MB); leave headroom for multipart encoding. */
const MAX_UPLOAD_BYTES = 4.5 * 1024 * 1024;
const CLIENT_MAX_UPLOAD_BYTES = 4 * 1024 * 1024;

function uploadTooLargeMessage(sizeBytes) {
  const fileMb = (sizeBytes / (1024 * 1024)).toFixed(1);
  return (
    `File is ${fileMb} MB — this deployment accepts about 4.5 MB per upload (Vercel limit). ` +
    "Save a smaller Excel/CSV (drop unused columns), or split into multiple files."
  );
}

function httpErrorMessage(status) {
  if (status === 413) {
    return uploadTooLargeMessage(MAX_UPLOAD_BYTES);
  }
  if (status >= 500) {
    return `Server error (${status}). Try again in a moment.`;
  }
  return `Request failed (${status}).`;
}

function isNonRetryableUploadStatus(status) {
  return status === 413 || status === 400 || status === 401 || status === 403 || status === 405;
}

function validateUploadFile(file) {
  if (!file) return "Choose a file first.";
  if (file.size > CLIENT_MAX_UPLOAD_BYTES) {
    return uploadTooLargeMessage(file.size);
  }
  return null;
}

const META_LABELS = {
  brand: "Brand",
  advertiser_name: "Advertiser",
  vendor_brand: "Vendor brand",
  social_description: "Social description",
  social_headline_text: "Headline",
  social_campaign_text: "Campaign text",
  platform: "Platform",
  creative_campaign_name: "Campaign name",
  creative_video_title: "Video title",
  social_page_name: "Page name",
  creative_url: "Creative URL",
};

const $ = (id) => document.getElementById(id);

const uploadSection = $("upload-section");
const reviewSection = $("review-section");
const doneSection = $("done-section");
const card = $("card");
const cardTitle = $("card-title");
const cardSubtitle = $("card-subtitle");
const cardHint = $("card-hint");
const cardGrid = $("card-grid");
const cardSingle = $("card-single");
const cardUnavailable = $("card-unavailable");
const cardCount = $("card-count");
const progressFill = $("progress-fill");
const progressText = $("progress-text");
const modeLabel = $("review-mode-label");
const hintLeft = $("hint-left");
const hintRight = $("hint-right");
const btnFault = $("btn-fault");
const btnOk = $("btn-ok");

const REVIEW_STORE_PREFIX = "yami-review:";
const SOURCE_DB_NAME = "yami-source";
const SOURCE_STORE = "sources";
const SOURCE_DB_VERSION = 1;

function setStatus(msg) {
  $("upload-status").textContent = msg;
}

function setReviewStatus(msg) {
  const el = $("review-status");
  if (!el) return;
  const text = String(msg || "").trim();
  el.textContent = text;
  el.classList.toggle("hidden", !text);
}

function setDoneExportStatus(msg) {
  const el = $("done-export-status");
  if (!el) return;
  const text = String(msg || "").trim();
  el.textContent = text;
  el.classList.toggle("hidden", !text);
}

function setFileSelectedName(name) {
  const el = $("file-selected");
  if (!el) return;
  const trimmed = String(name || "").trim();
  if (!trimmed) {
    el.textContent = "";
    el.classList.add("hidden");
    return;
  }
  el.textContent = `Selected file: ${trimmed}`;
  el.classList.remove("hidden");
}

function resetColumnConfig() {
  const config = $("column-config");
  const hint = $("file-columns-hint");
  const urlSel = $("url-column");
  const brandSel = $("brand-column");
  const btn = $("upload-btn");
  if (config) config.classList.add("hidden");
  if (hint) hint.textContent = "";
  if (urlSel) {
    urlSel.innerHTML = "";
    urlSel.disabled = true;
  }
  if (brandSel) {
    brandSel.innerHTML = "";
    brandSel.disabled = true;
  }
  if (btn) btn.disabled = true;
}

function populateColumnSelect(selectEl, columns, selected) {
  selectEl.innerHTML = "";
  for (const col of columns) {
    const opt = document.createElement("option");
    opt.value = col;
    opt.textContent = col;
    if (col === selected) opt.selected = true;
    selectEl.appendChild(opt);
  }
  if (!selected && columns.length) {
    selectEl.value = columns[0];
  }
  selectEl.disabled = false;
}

async function previewFileColumns(file) {
  resetColumnConfig();
  if (!file) return;

  const sizeErr = validateUploadFile(file);
  if (sizeErr) {
    setStatus(sizeErr);
    return;
  }

  setStatus("Reading column names…");
  const fd = new FormData();
  fd.append("file", file);

  let data;
  try {
    const res = await fetch("/api/preview-columns", { method: "POST", body: fd });
    data = await parseJsonResponse(res);
    if (!res.ok) {
      throw new Error(
        typeof data.detail === "string" ? data.detail : httpErrorMessage(res.status)
      );
    }
  } catch (err) {
    setStatus(err.message || String(err));
    return;
  }

  const columns = data.columns || [];
  if (!columns.length) {
    setStatus("No columns found in file.");
    return;
  }

  const urlSel = $("url-column");
  const brandSel = $("brand-column");
  populateColumnSelect(urlSel, columns, data.defaultUrlColumn || null);
  populateColumnSelect(brandSel, columns, data.defaultBrandColumn || null);

  const hint = $("file-columns-hint");
  if (hint) {
    hint.textContent = `${columns.length} column${columns.length === 1 ? "" : "s"}: ${columns.join(", ")}`;
  }
  $("column-config")?.classList.remove("hidden");
  $("upload-btn").disabled = false;
  setStatus("Choose URL and brand columns, then upload.");
}

function setReviewFilename(name) {
  const el = $("review-filename");
  if (!el) return;
  const trimmed = String(name || "").trim();
  if (!trimmed) {
    el.textContent = "";
    el.classList.add("hidden");
    return;
  }
  el.textContent = `File: ${trimmed}`;
  el.classList.remove("hidden");
}

function setReviewWait(show) {
  const el = $("review-wait");
  if (!el) return;
  el.classList.toggle("hidden", !show);
  btnFault.disabled = Boolean(show);
  btnOk.disabled = Boolean(show);
}

function currentQueue() {
  if (mode === "unavailable") return unavailableMedia ? [unavailableMedia] : [];
  if (mode === "uncertain") return uncertainItems;
  return brandGroups;
}

function unavailableStepCount() {
  return unavailableMedia ? 1 : 0;
}

function totalReviewSteps() {
  return unavailableStepCount() + uncertainItems.length + brandGroups.length;
}

function loadReviewStore() {
  if (!sessionId) return { sessionId: null, rows: {} };
  try {
    const raw = localStorage.getItem(REVIEW_STORE_PREFIX + sessionId);
    if (!raw) return { sessionId, rows: {} };
    return JSON.parse(raw);
  } catch {
    return { sessionId, rows: {} };
  }
}

function saveReviewStore(store) {
  if (!sessionId) return;
  try {
    localStorage.setItem(REVIEW_STORE_PREFIX + sessionId, JSON.stringify(store));
  } catch (err) {
    setReviewStatus(`Could not save review in browser: ${err.message || err}`);
  }
}

function getRowState(rowIndex) {
  const store = loadReviewStore();
  return store.rows[String(rowIndex)] || null;
}

function setRowReviewState(rowIndex, patch) {
  const store = loadReviewStore();
  const key = String(rowIndex);
  store.rows[key] = { ...(store.rows[key] || {}), ...patch };
  saveReviewStore(store);
}

function initReviewStore(data) {
  const rows = {};
  const total = Number(data.totalRows) || 0;
  for (let i = 0; i < total; i++) {
    rows[String(i)] = {
      isFault: false,
      faultManual: false,
      reviewed: false,
      advertiserMatch: null,
      brandName: "",
      advertiserName: "",
    };
  }

  function mergeItem(item) {
    const idx = String(item.rowIndex);
    const meta = item.metadata || {};
    rows[idx] = {
      ...rows[idx],
      isFault: Boolean(item.isFault),
      faultManual: Boolean(item.isFault),
      brandName: meta.brand || rows[idx].brandName || "",
      advertiserName: meta.advertiser_name || rows[idx].advertiserName || "",
    };
  }

  for (const g of data.groups || []) {
    for (const item of g.items || []) mergeItem(item);
  }
  for (const g of data.uncertain || []) {
    for (const item of g.items || []) mergeItem(item);
  }
  for (const entry of data.unavailable?.entries || []) {
    const idx = String(entry.rowIndex);
    rows[idx] = {
      ...rows[idx],
      brandName: entry.brand || rows[idx].brandName || "",
      advertiserName: entry.advertiserName || rows[idx].advertiserName || "",
    };
  }

  saveReviewStore({ sessionId: data.sessionId, filename: uploadedFilename, rows });
}

function openSourceDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(SOURCE_DB_NAME, SOURCE_DB_VERSION);
    req.onerror = () => reject(req.error || new Error("IndexedDB unavailable"));
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(SOURCE_STORE)) {
        db.createObjectStore(SOURCE_STORE, { keyPath: "sessionId" });
      }
    };
    req.onsuccess = () => resolve(req.result);
  });
}

function detectFaultyExportColumn(headers) {
  const lower = headers.map((h) => String(h || "").trim().toLowerCase());
  for (const key of ["isfaulty", "is_faulty"]) {
    const i = lower.indexOf(key);
    if (i >= 0) return headers[i];
  }
  return null;
}

function classifiedExportFilename(originalFilename) {
  const name = String(originalFilename || "upload.xlsx").trim() || "upload.xlsx";
  const m = name.match(/^(.+?)(\.[^.]+)?$/);
  if (!m) return "upload_Classified.xlsx";
  let stem = m[1];
  const ext = m[2] || ".xlsx";
  if (stem.endsWith("_Classified")) return `${stem}.xlsx`;
  return `${stem}_Classified.xlsx`;
}

async function readUploadWorkbook(file) {
  const buf = await file.arrayBuffer();
  const name = (file.name || "").toLowerCase();

  if (name.endsWith(".json")) {
    const text = new TextDecoder("utf-8").decode(buf);
    const payload = JSON.parse(text);
    const rows = Array.isArray(payload)
      ? payload
      : payload.rows || payload.data || [payload];
    const ws = XLSX.utils.json_to_sheet(rows);
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "Sheet1");
    return wb;
  }

  if (name.endsWith(".csv") || name.endsWith(".txt")) {
    const text = new TextDecoder("utf-8").decode(buf);
    return XLSX.read(text, { type: "string" });
  }

  return XLSX.read(new Uint8Array(buf), { type: "array" });
}

async function parseSourceSnapshot(file, urlColumn, brandColumn) {
  if (typeof XLSX === "undefined") {
    throw new Error("Export library failed to load — refresh the page.");
  }
  const wb = await readUploadWorkbook(file);
  const sheetName = wb.SheetNames[0];
  const ws = wb.Sheets[sheetName];
  const aoa = XLSX.utils.sheet_to_json(ws, { header: 1, defval: "" });
  if (!aoa.length) throw new Error("Spreadsheet is empty.");

  const headers = (aoa[0] || []).map((h) => String(h ?? ""));
  const rows = aoa.slice(1).map((row) => {
    const out = headers.map((_, ci) => {
      const v = row?.[ci];
      if (v == null) return "";
      return v;
    });
    return out;
  });

  return {
    filename: file.name || "upload.xlsx",
    urlColumn,
    brandColumn,
    faultyColumn: detectFaultyExportColumn(headers),
    headers,
    rows,
  };
}

async function saveSourceSnapshot(sessionId, snapshot) {
  const db = await openSourceDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SOURCE_STORE, "readwrite");
    tx.objectStore(SOURCE_STORE).put({ ...snapshot, sessionId, savedAt: Date.now() });
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
    tx.onerror = () => {
      db.close();
      reject(tx.error || new Error("Could not save source file in browser"));
    };
  });
}

async function loadSourceSnapshot(sessionId) {
  const db = await openSourceDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SOURCE_STORE, "readonly");
    const req = tx.objectStore(SOURCE_STORE).get(sessionId);
    req.onsuccess = () => {
      db.close();
      resolve(req.result || null);
    };
    req.onerror = () => {
      db.close();
      reject(req.error || new Error("Could not read source file from browser"));
    };
  });
}

function buildClientExportWorkbook(sourceSnap, reviewRows) {
  const headers = [...sourceSnap.headers];
  const rows = sourceSnap.rows.map((r) => [...r]);

  let faultyIdx = sourceSnap.faultyColumn
    ? headers.indexOf(sourceSnap.faultyColumn)
    : -1;
  if (faultyIdx < 0) {
    headers.push("isFaulty");
    faultyIdx = headers.length - 1;
    for (const row of rows) row.push("");
  }

  const extraCols = ["advertiserMatch", "reviewed"];
  const extraIdx = {};
  for (const col of extraCols) {
    let idx = headers.indexOf(col);
    if (idx < 0) {
      headers.push(col);
      idx = headers.length - 1;
      for (const row of rows) row.push("");
    }
    extraIdx[col] = idx;
  }

  for (let i = 0; i < rows.length; i += 1) {
    const state = reviewRows[String(i)] || {};
    rows[i][faultyIdx] = Boolean(state.isFault);
    rows[i][extraIdx.advertiserMatch] =
      state.advertiserMatch == null ? "" : state.advertiserMatch;
    rows[i][extraIdx.reviewed] = Boolean(state.reviewed);
  }

  const ws = XLSX.utils.aoa_to_sheet([headers, ...rows]);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "Results");
  return wb;
}

async function exportResultsClient() {
  if (!sessionId) throw new Error("No active session.");
  const sourceSnap = await loadSourceSnapshot(sessionId);
  if (!sourceSnap?.headers?.length) {
    throw new Error(
      "Source file not found in this browser. Re-upload the same file, then export again."
    );
  }
  const review = loadReviewStore();
  const wb = buildClientExportWorkbook(sourceSnap, review.rows || {});
  const filename = classifiedExportFilename(sourceSnap.filename || uploadedFilename);
  XLSX.writeFile(wb, filename);
}

function hydrateItemFromStore(item) {
  const state = getRowState(item.rowIndex);
  if (state) {
    item.isFault = Boolean(state.isFault);
  }
  return item;
}

function applyReviewStoreToQueues() {
  const hydrateGroup = (g) => {
    if (!g?.items) return;
    for (const item of g.items) hydrateItemFromStore(item);
  };
  for (const g of brandGroups) hydrateGroup(g);
  for (const g of uncertainItems) hydrateGroup(g);
}

function applyUnavailableReview(isFault) {
  for (const entry of unavailableMedia?.entries || []) {
    setRowReviewState(entry.rowIndex, {
      isFault,
      faultManual: isFault,
      reviewed: true,
      advertiserMatch: !isFault,
    });
  }
}

function applyUncertainGroupReview(item, advertiserMatch) {
  const indices = item.memberIndices?.length
    ? item.memberIndices
    : (item.items || []).map((i) => i.rowIndex);
  for (const idx of indices) {
    const existing = getRowState(idx) || {};
    const manual = Boolean(existing.faultManual);
    setRowReviewState(idx, {
      reviewed: true,
      advertiserMatch,
      isFault: !advertiserMatch ? true : manual ? Boolean(existing.isFault) : false,
      faultManual: manual,
    });
  }
}

function applyBrandGroupReview(item) {
  const indices = item.memberIndices?.length
    ? item.memberIndices
    : (item.items || []).map((i) => i.rowIndex);
  for (const idx of indices) {
    const existing = getRowState(idx) || {};
    const manual = Boolean(existing.faultManual);
    setRowReviewState(idx, {
      reviewed: true,
      advertiserMatch: true,
      isFault: manual ? Boolean(existing.isFault) : false,
      faultManual: manual,
    });
  }
}

function renderUnavailableTable(session) {
  const tbody = $("unavailable-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";
  const entries = session.entries || [];
  entries.forEach((row) => {
    const tr = document.createElement("tr");
    const brandTd = document.createElement("td");
    const brand = (row.brand || "").trim();
    const advertiser = (row.advertiserName || "").trim();
    if (brand && advertiser && brand !== advertiser) {
      brandTd.textContent = `${brand} / ${advertiser}`;
    } else {
      brandTd.textContent = brand || advertiser || "—";
    }
    const urlTd = document.createElement("td");
    const link = document.createElement("a");
    link.href = row.creativeUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = row.creativeUrl;
    urlTd.appendChild(link);
    const reasonTd = document.createElement("td");
    const parts = [row.reason || "Unavailable image"];
    if (row.fetchDetail && !parts[0].includes(row.fetchDetail)) {
      parts.push(row.fetchDetail);
    }
    reasonTd.textContent = parts.filter(Boolean).join(" — ");
    tr.appendChild(brandTd);
    tr.appendChild(urlTd);
    tr.appendChild(reasonTd);
    tbody.appendChild(tr);
  });
}

function renderSingleImage(container, items) {
  container.innerHTML = "";
  items.forEach((item, i) => {
    const cell = document.createElement("div");
    cell.className = "single-media-cell";
    cell.setAttribute("aria-label", "Creative — right-click to inspect");

    const img = document.createElement("img");
    img.alt = `Creative ${i + 1}`;
    img.loading = "lazy";
    img.draggable = false;
    img.referrerPolicy = "no-referrer";
    const mediaUrl = item.mediaUrl || item.thumbUrl;
    const thumbUrl = item.thumbUrl;
    const derivedPoster =
      isVideoUrl(mediaUrl) && isAdclarityUrl(mediaUrl)
        ? adclarityPosterFromMp4(mediaUrl)
        : "";
    const ytThumbs = isYoutubeUrl(mediaUrl) ? youtubeThumbCandidates(mediaUrl) : [];
    setImgSrcWithFallback(img, [
      thumbUrl,
      derivedPoster,
      derivedPoster ? adclarityJpgFromJpeg(derivedPoster) : "",
      ...ytThumbs,
      mediaUrl,
    ]);

    cell.appendChild(img);
    cell._item = item;
    cell.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openContextMenu(e.clientX, e.clientY, item);
    });
    container.appendChild(cell);
  });
}

function enrichInspectItem(item) {
  const group = currentQueue()[cursor];
  return {
    ...item,
    title: item.title || group?.title || item.metadata?.brand || "",
    subtitle:
      item.subtitle ||
      group?.subtitle ||
      item.metadata?.advertiser_name ||
      "",
  };
}

/** Accept new uncertain groups and legacy per-row payloads from older sessions. */
function normalizeUncertainItems(raw) {
  if (!Array.isArray(raw) || !raw.length) return [];
  const out = [];
  let nextGroupId = 0;

  for (const entry of raw) {
    if (
      entry.type === "uncertain" &&
      Array.isArray(entry.items) &&
      entry.items.length &&
      entry.groupId != null
    ) {
      out.push(entry);
      nextGroupId = Math.max(nextGroupId, Number(entry.groupId) + 1);
      continue;
    }

    const items = Array.isArray(entry.items) && entry.items.length
      ? entry.items
      : entry.rowIndex != null
        ? [
            {
              rowIndex: entry.rowIndex,
              mediaUrl: entry.mediaUrl,
              thumbUrl: entry.thumbUrl,
              isFault: Boolean(entry.isFault),
              metadata: entry.metadata || {},
            },
          ]
        : [];

    if (!items.length) continue;

    out.push({
      groupId: entry.groupId != null ? entry.groupId : nextGroupId++,
      type: "uncertain",
      title: entry.title || "Unknown brand",
      subtitle: entry.subtitle || "",
      uncertainReason: entry.uncertainReason || "review",
      reasonHint: entry.reasonHint || "Verify brand labels for this group.",
      memberIndices: items.map((i) => Number(i.rowIndex)),
      items,
      count: items.length,
    });
  }
  return out;
}

function isGridReviewMode() {
  return mode === "uncertain" || mode === "brand";
}

const GRID_MIN_CELL_PX = 200;
const GRID_GAP_PX = 12;
const GRID_OVERSCAN_ROWS = 4;
const GRID_MAX_IMAGE_LOADS = 8;
const GRID_SCROLL_DEBOUNCE_MS = 120;
const GRID_LARGE_GROUP = 48;

let gridAllItems = [];
let gridVirtual = null;

/** url → { status: "ok"|"fail", src } */
const thumbImageCache = new Map();

function getGridMetrics(container) {
  const style = getComputedStyle(container);
  const paddingX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  const innerW = Math.max(0, container.clientWidth - paddingX);
  const columns = Math.max(
    1,
    Math.floor((innerW + GRID_GAP_PX) / (GRID_MIN_CELL_PX + GRID_GAP_PX))
  );
  const cellWidth = (innerW - (columns - 1) * GRID_GAP_PX) / columns;
  const rowHeight = cellWidth + GRID_GAP_PX;
  return { columns, cellWidth, rowHeight };
}

function isTikTokPageUrl(url) {
  const u = String(url || "").toLowerCase();
  if (!u.includes("tiktok.com")) return false;
  if (u.includes("tiktokcdn") || u.includes("tiktokv.com")) return false;
  return true;
}

function isImageLoadableUrl(url) {
  if (!url) return false;
  if (isTikTokPageUrl(url)) return false;
  if (isVideoUrl(url) && !/\.(jpe?g|png|gif|webp)(\?|$)/i.test(url)) return false;
  return true;
}

function applyThumbFromCache(cell) {
  const img = cell._img;
  if (!img) return false;
  for (const src of cell._sources || []) {
    const cached = thumbImageCache.get(src);
    if (cached?.status === "ok") {
      img.src = cached.src;
      img.classList.add("loaded");
      cell._imgLoaded = true;
      cell.classList.remove("thumb-load-failed", "thumb-tiktok-placeholder");
      return true;
    }
  }
  return false;
}

function showThumbPlaceholder(cell) {
  const mediaUrl = cell._item?.mediaUrl || cell._item?.thumbUrl || "";
  cell.classList.remove("thumb-load-failed", "thumb-tiktok-placeholder");
  if (isTikTokPageUrl(mediaUrl)) {
    cell.classList.add("thumb-tiktok-placeholder");
  } else {
    cell.classList.add("thumb-load-failed");
  }
}

const thumbImageQueue = (() => {
  let active = 0;
  const waiting = [];

  function pump() {
    waiting.sort((a, b) => (a._queuePriority ?? 0) - (b._queuePriority ?? 0));
    while (active < GRID_MAX_IMAGE_LOADS && waiting.length) {
      const cell = waiting.shift();
      if (!cell?.isConnected) continue;
      if (cell._imgLoaded || cell._imgLoading) continue;
      if (applyThumbFromCache(cell)) continue;
      active += 1;
      cell._imgLoading = true;
      loadThumbImage(cell, () => {
        active -= 1;
        cell._imgLoading = false;
        pump();
      });
    }
  }

  return {
    enqueue(cell, priority = 0) {
      if (cell._imgLoaded) return;
      if (applyThumbFromCache(cell)) return;
      cell._queuePriority = priority;
      if (!waiting.includes(cell)) waiting.push(cell);
      pump();
    },
    detach(cell) {
      const idx = waiting.indexOf(cell);
      if (idx >= 0) waiting.splice(idx, 1);
      // In-flight loads continue and populate thumbImageCache.
    },
    reset() {
      waiting.length = 0;
      active = 0;
    },
  };
})();

function loadThumbImage(cell, onDone) {
  const img = cell._img;
  const sources = (cell._sources || []).filter(isImageLoadableUrl);
  if (!img || !sources.length) {
    if (cell.isConnected) showThumbPlaceholder(cell);
    onDone();
    return;
  }

  let attempt = 0;

  const finish = () => {
    img.onload = null;
    img.onerror = null;
    onDone();
  };

  const tryNext = () => {
    if (attempt >= sources.length) {
      for (const s of sources) {
        if (!thumbImageCache.has(s)) thumbImageCache.set(s, { status: "fail" });
      }
      if (cell.isConnected) showThumbPlaceholder(cell);
      finish();
      return;
    }
    const src = sources[attempt];
    attempt += 1;

    const cached = thumbImageCache.get(src);
    if (cached?.status === "ok") {
      if (cell.isConnected) {
        img.src = cached.src;
        img.classList.add("loaded");
        cell._imgLoaded = true;
        cell.classList.remove("thumb-load-failed", "thumb-tiktok-placeholder");
      }
      finish();
      return;
    }
    if (cached?.status === "fail") {
      tryNext();
      return;
    }

    img.onload = () => {
      thumbImageCache.set(src, { status: "ok", src });
      if (cell.isConnected) {
        img.classList.add("loaded");
        cell._imgLoaded = true;
        cell.classList.remove("thumb-load-failed", "thumb-tiktok-placeholder");
      }
      finish();
    };
    img.onerror = () => {
      thumbImageCache.set(src, { status: "fail" });
      tryNext();
    };
    img.src = src;
    if (img.complete && img.naturalWidth > 0) img.onload();
  };

  tryNext();
}

function thumbSourcesForItem(item) {
  const mediaUrl = item.mediaUrl || item.thumbUrl;
  const thumbUrl = item.thumbUrl;
  const derivedPoster =
    isVideoUrl(mediaUrl) && isAdclarityUrl(mediaUrl)
      ? adclarityPosterFromMp4(mediaUrl)
      : "";
  const ytThumbs = isYoutubeUrl(mediaUrl) ? youtubeThumbCandidates(mediaUrl) : [];
  const raw = [
    thumbUrl,
    derivedPoster,
    derivedPoster ? adclarityJpgFromJpeg(derivedPoster) : "",
    ...ytThumbs,
  ];
  const seen = new Set();
  const out = [];
  for (const u of raw) {
    if (!u || !isImageLoadableUrl(u) || seen.has(u)) continue;
    seen.add(u);
    out.push(u);
  }
  return out;
}

function destroyVirtualGrid() {
  if (!gridVirtual) return;
  gridVirtual.destroy();
  gridVirtual = null;
}

function createVirtualGrid(container, items) {
  destroyVirtualGrid();
  thumbImageQueue.reset();
  gridAllItems = items || [];
  container.innerHTML = "";
  container.scrollTop = 0;
  if (!gridAllItems.length) return;

  const topSpacer = document.createElement("div");
  topSpacer.className = "grid-virtual-spacer";
  topSpacer.setAttribute("aria-hidden", "true");
  const cellsEl = document.createElement("div");
  cellsEl.className = "grid-virtual-cells";
  const bottomSpacer = document.createElement("div");
  bottomSpacer.className = "grid-virtual-spacer";
  bottomSpacer.setAttribute("aria-hidden", "true");

  container.appendChild(topSpacer);
  container.appendChild(cellsEl);
  container.appendChild(bottomSpacer);

  const mounted = new Map();
  let raf = null;
  let scrollDebounceTimer = null;

  function layout() {
    const metrics = getGridMetrics(container);
    const columns = metrics.columns;
    let { rowHeight } = metrics;
    if (!rowHeight || rowHeight <= 0) {
      rowHeight = GRID_MIN_CELL_PX + GRID_GAP_PX;
    }
    const totalRows = Math.ceil(gridAllItems.length / columns);
    const scrollTop = container.scrollTop;
    const viewH = container.clientHeight || rowHeight * 3;
    const viewCenterY = scrollTop + viewH / 2;

    const startRow = Math.max(0, Math.floor(scrollTop / rowHeight) - GRID_OVERSCAN_ROWS);
    const endRow = Math.min(
      totalRows,
      Math.max(
        startRow + 1,
        Math.ceil((scrollTop + viewH) / rowHeight) + GRID_OVERSCAN_ROWS
      )
    );

    const startIndex = startRow * columns;
    const endIndex = Math.min(gridAllItems.length, endRow * columns);

    topSpacer.style.height = `${startRow * rowHeight}px`;
    bottomSpacer.style.height = `${Math.max(0, (totalRows - endRow) * rowHeight)}px`;

    cellsEl.style.gridTemplateColumns = `repeat(${columns}, 1fr)`;
    cellsEl.style.gap = `${GRID_GAP_PX}px`;

    for (const [idx, cell] of mounted) {
      if (idx < startIndex || idx >= endIndex) {
        thumbImageQueue.detach(cell);
        cell.remove();
        mounted.delete(idx);
      }
    }

    for (let i = startIndex; i < endIndex; i += 1) {
      if (mounted.has(i)) continue;
      const cell = createThumbCell(gridAllItems[i], { deferLoad: true });
      cell.dataset.virtualIndex = String(i);
      cellsEl.appendChild(cell);
      mounted.set(i, cell);
      const cellRow = Math.floor(i / columns);
      const cellCenterY = cellRow * rowHeight + rowHeight / 2;
      const priority = Math.abs(cellCenterY - viewCenterY);
      thumbImageQueue.enqueue(cell, priority);
    }
  }

  function runLayout() {
    if (raf) cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      raf = null;
      layout();
    });
  }

  function scheduleLayout(immediate = false) {
    if (immediate) {
      clearTimeout(scrollDebounceTimer);
      scrollDebounceTimer = null;
      runLayout();
      return;
    }
    clearTimeout(scrollDebounceTimer);
    scrollDebounceTimer = setTimeout(() => {
      scrollDebounceTimer = null;
      runLayout();
    }, GRID_SCROLL_DEBOUNCE_MS);
  }

  const onScroll = () => scheduleLayout(false);
  const ro = new ResizeObserver(() => scheduleLayout(true));

  container.addEventListener("scroll", onScroll, { passive: true });
  ro.observe(container);
  scheduleLayout(true);

  gridVirtual = {
    refresh: () => scheduleLayout(true),
    updateFaultStates() {
      for (const cell of mounted.values()) {
        if (cell._item) applyFaultUi(cell._item, cell, Boolean(cell._item.isFault));
      }
    },
    destroy() {
      clearTimeout(scrollDebounceTimer);
      if (raf) cancelAnimationFrame(raf);
      container.removeEventListener("scroll", onScroll);
      ro.disconnect();
      for (const cell of mounted.values()) thumbImageQueue.detach(cell);
      mounted.clear();
      thumbImageQueue.reset();
    },
  };
}

function createThumbCell(item, options = {}) {
  const cell = document.createElement("button");
  cell.type = "button";
  cell.className = "thumb-cell";
  cell.dataset.rowIndex = String(item.rowIndex);

  const img = document.createElement("img");
  img.alt = "Creative";
  img.draggable = false;
  img.referrerPolicy = "no-referrer";

  const sources = thumbSourcesForItem(item);
  cell._sources = sources;
  cell._img = img;
  cell._imgLoaded = false;
  cell._imgLoading = false;

  const overlay = document.createElement("div");
  overlay.className = "fault-overlay";
  overlay.innerHTML = '<span class="fault-x" aria-hidden="true">✕</span>';

  cell.appendChild(img);
  cell.appendChild(overlay);
  cell._item = item;
  applyFaultUi(item, cell, Boolean(item.isFault));

  if (!options.deferLoad) {
    if (applyThumbFromCache(cell)) {
      /* cached */
    } else if (sources.length) {
      loadThumbImage(cell, () => {});
    } else {
      showThumbPlaceholder(cell);
    }
  }

  return cell;
}

function bindGridInteractions() {
  if (!cardGrid || cardGrid.dataset.bound === "1") return;
  cardGrid.dataset.bound = "1";

  cardGrid.addEventListener(
    "pointerdown",
    (e) => {
      if (e.target.closest(".thumb-cell")) e.stopPropagation();
    },
    true
  );

  cardGrid.addEventListener("click", (e) => {
    if (!isGridReviewMode()) return;
    const cell = e.target.closest(".thumb-cell");
    if (!cell?._item) return;
    e.preventDefault();
    e.stopPropagation();
    toggleImageFault(cell._item, cell);
  });

  cardGrid.addEventListener("contextmenu", (e) => {
    if (!isGridReviewMode()) return;
    const cell = e.target.closest(".thumb-cell");
    if (!cell?._item) return;
    e.preventDefault();
    e.stopPropagation();
    openInspectModal(enrichInspectItem(cell._item));
  });
}

function renderBrandGrid(container, items) {
  createVirtualGrid(container, items);
}

function updateGroupCountText() {
  const queue = currentQueue();
  const g = queue[cursor];
  if (!g?.items) return;
  const faultCount = g.items.filter((i) => i.isFault).length;
  const active = g.items.length - faultCount;
  const label = mode === "uncertain" ? "Uncertain brand" : "Brand";
  cardCount.textContent =
    `${label} ${cursor + 1} of ${queue.length} · ${g.items.length} creatives · ${active} active · ${faultCount} marked fault`;
}

function applyFaultUi(item, cell, isFault) {
  item.isFault = isFault;
  cell.classList.toggle("is-fault", isFault);
  cell.setAttribute("aria-pressed", isFault ? "true" : "false");
  cell.setAttribute(
    "aria-label",
    isFault ? "Marked fault — click to clear ✕" : "Mark as fault (✕)"
  );
}

async function toggleImageFault(item, cell) {
  if (cell.dataset.toggling === "1") return;
  cell.dataset.toggling = "1";
  const nextFault = !Boolean(item.isFault);
  applyFaultUi(item, cell, nextFault);
  setRowReviewState(item.rowIndex, {
    isFault: nextFault,
    faultManual: nextFault,
  });
  updateGroupCountText();
  setReviewStatus("");
  delete cell.dataset.toggling;
}

function markAllGridFaults() {
  if (!isGridReviewMode() || reviewing) return;
  const group = currentQueue()[cursor];
  const items = group?.items || [];
  if (!items.length) return;

  for (const item of items) {
    setRowReviewState(item.rowIndex, { isFault: true, faultManual: true });
    item.isFault = true;
  }
  for (const item of gridAllItems) {
    item.isFault = true;
  }
  gridVirtual?.updateFaultStates();
  updateGroupCountText();
  setReviewStatus("");
}

function setUiForMode() {
  const isUnavailable = mode === "unavailable";
  const isUncertain = mode === "uncertain";
  cardGrid.classList.toggle("hidden", isUnavailable);
  cardSingle.classList.add("hidden");
  if (cardUnavailable) {
    cardUnavailable.classList.toggle("hidden", !isUnavailable);
  }

  if (isUnavailable) {
    hintLeft.textContent = "← Mark all fault";
    hintRight.textContent = "Acknowledge →";
    btnFault.textContent = "✕ Mark all fault";
    btnOk.textContent = "✓ Next";
    modeLabel.textContent = "Unavailable media — review table, then swipe";
  } else if (isUncertain) {
    hintLeft.textContent = "← Incorrect brand (group)";
    hintRight.textContent = "Correct brand →";
    btnFault.textContent = "✕ Mark all on this page";
    btnOk.textContent = "✓ Correct brand";
    modeLabel.textContent =
      "Uncertain ads · grouped by brand (grid: tap ✕ fault, right-click inspect)";
  } else {
    hintLeft.textContent = "";
    hintRight.textContent = "Next →";
    btnFault.textContent = "✕ Mark all Fault on this page";
    btnOk.textContent = "✓ Next";
    modeLabel.textContent = "Reviewing brand groups";
  }
}

function formatAdvertiserSubtitle(text) {
  if (!text || !String(text).trim()) return "";
  return String(text).trim();
}

function setCardHeader({ title, subtitle, hint }) {
  if (cardTitle) cardTitle.textContent = title || "Unknown brand";
  const sub = formatAdvertiserSubtitle(subtitle);
  if (cardSubtitle) {
    if (sub) {
      cardSubtitle.textContent = sub;
      cardSubtitle.classList.remove("hidden");
    } else {
      cardSubtitle.textContent = "";
      cardSubtitle.classList.add("hidden");
    }
  }
  if (cardHint) {
    if (hint) {
      cardHint.textContent = hint;
      cardHint.classList.remove("hidden");
    } else {
      cardHint.textContent = "";
      cardHint.classList.add("hidden");
    }
  }
}

function showCard() {
  reviewing = false;
  setReviewWait(false);
  setUiForMode();
  const queue = currentQueue();

  if (mode === "unavailable") {
    if (!unavailableMedia || cursor >= queue.length) {
      mode = uncertainItems.length > 0 ? "uncertain" : "brand";
      cursor = 0;
      setUiForMode();
      showCard();
      return;
    }
    const session = queue[cursor];
    setCardHeader({
      title: session.title || "Unavailable media",
      subtitle: `${session.count} creatives could not be previewed`,
      hint: "Open creative URLs in the table (new tab). Swipe right when done, or left to mark all as fault.",
    });
    cardCount.textContent = `${session.count} unavailable · 1 review step`;
    renderUnavailableTable(session);
  } else {
    if (mode === "uncertain" && cursor >= uncertainItems.length) {
      if (brandGroups.length > 0) {
        mode = "brand";
        cursor = 0;
        setUiForMode();
        showCard();
        return;
      }
      finishReview();
      return;
    }

    if (mode === "brand" && cursor >= brandGroups.length) {
      finishReview();
      return;
    }

    const item = queue[cursor];
    const items = (item.items?.length ? item.items : []).map((i) =>
      hydrateItemFromStore({ ...i })
    );
    if (item.items?.length) {
      item.items = items;
    }

    const gridHint =
      items.length > GRID_LARGE_GROUP
        ? `${items.length} creatives · scroll to browse · Tap ✕ fault · Mark all on page · Swipe OK`
        : "Tap image to toggle ✕ · Mark all on page · Right-click inspect · Swipe OK group";
    if (mode === "uncertain") {
      const reasonHint =
        item.reasonHint ||
        "Verify the brand label matches every creative in this group.";
      setCardHeader({
        title: item.title || "Unknown brand",
        subtitle: item.subtitle,
        hint: `${reasonHint} ${gridHint}`,
      });
    } else {
      setCardHeader({
        title: item.title,
        subtitle: item.subtitle,
        hint: gridHint,
      });
    }
    if (cardGrid) cardGrid.classList.remove("hidden");
    if (cardSingle) cardSingle.classList.add("hidden");
    renderBrandGrid(cardGrid, items);
    updateGroupCountText();
  }

  const totalSteps = totalReviewSteps();
  let doneSteps = unavailableStepCount();
  if (mode === "unavailable") {
    doneSteps = 0;
  } else if (mode === "uncertain") {
    doneSteps = unavailableStepCount() + cursor;
  } else {
    doneSteps = unavailableStepCount() + uncertainItems.length + cursor;
  }
  progressFill.style.width = totalSteps ? `${(doneSteps / totalSteps) * 100}%` : "0%";
  progressText.textContent = `${doneSteps} / ${totalSteps} items reviewed`;

  card.style.transform = "";
  card.style.opacity = "1";
  card.classList.remove("swipe-left", "swipe-right");
}

function finishReview() {
  reviewSection.classList.add("hidden");
  doneSection.classList.remove("hidden");
  $("export-btn")?.classList.add("hidden");
  setDoneExportStatus("");
}

function showLoading(show) {
  $("loading-overlay").classList.toggle("hidden", !show);
}

function postFormDataWithProgress(url, fd, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.responseType = "text";
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const ratio = e.total > 0 ? e.loaded / e.total : 0;
      onProgress?.(ratio, e.loaded, e.total);
    };
    xhr.onload = () => {
      resolve({
        status: xhr.status,
        ok: xhr.status >= 200 && xhr.status < 300,
        text: xhr.responseText || "",
      });
    };
    xhr.onerror = () => reject(new Error("Network error during upload"));
    xhr.send(fd);
  });
}

function phaseLabel(phase) {
  if (phase === "starting" || phase === "receive") return "Starting…";
  if (phase === "parse") return "Reading spreadsheet…";
  if (phase === "group") return "Grouping by brand…";
  if (phase === "done") return "Done!";
  return "Downloading media…";
}

function setLoadingProgress(percent, phase, hint, countText) {
  const pct = Math.max(0, Math.min(100, percent || 0));
  const ringFill = $("progress-ring-fill");
  if (ringFill) {
    ringFill.style.strokeDashoffset = String(RING_CIRCUMFERENCE * (1 - pct / 100));
  }
  const bar = $("loading-bar-fill");
  if (bar) bar.style.width = `${pct}%`;
  const pctEl = $("loading-percent");
  if (pctEl) pctEl.textContent = `${Math.round(pct)}%`;
  const ringWrap = $("progress-ring");
  if (ringWrap) ringWrap.setAttribute("aria-valuenow", String(Math.round(pct)));
  const phaseEl = $("loading-phase");
  if (phase && phaseEl) phaseEl.textContent = phaseLabel(phase) || phase;
  const hintEl = $("loading-hint");
  if (hint !== undefined && hint !== "" && hintEl) hintEl.textContent = hint;
  const countEl = $("loading-count");
  if (countText !== undefined && countEl) countEl.textContent = countText;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function hideContextMenu() {
  $("context-menu").classList.add("hidden");
  contextItem = null;
}

function openContextMenu(x, y, item) {
  contextItem = item;
  const menu = $("context-menu");
  menu.classList.remove("hidden");
  menu.style.left = `${Math.min(x, window.innerWidth - 180)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - 48)}px`;
}

function isVideoUrl(url) {
  return /\.(mp4|webm|mov|m4v)(\?|$)/i.test(url || "") || /_video\.mp4/i.test(url || "");
}

function isYoutubeUrl(url) {
  const u = String(url || "").toLowerCase();
  return u.includes("youtu.be/") || u.includes("youtube.com/");
}

function isTikTokUrl(url) {
  const u = String(url || "").toLowerCase();
  return u.includes("tiktok.com/");
}

function youtubeVideoId(url) {
  const u = String(url || "");
  const mShort = u.match(/youtu\.be\/([^?/#]+)/i);
  if (mShort) return mShort[1];
  const mEmbed = u.match(/youtube\.com\/(?:embed|shorts|v)\/([^?/#]+)/i);
  if (mEmbed) return mEmbed[1];
  const mWatch = u.match(/[?&]v=([^&]+)/i);
  if (mWatch) return mWatch[1];
  return "";
}

function youtubeEmbedUrl(url) {
  const id = youtubeVideoId(url);
  if (!id) return "";
  return `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}?rel=0&modestbranding=1`;
}

function youtubeThumbCandidates(url) {
  const id = youtubeVideoId(url);
  if (!id) return [];
  const vid = encodeURIComponent(id);
  return [
    `https://i.ytimg.com/vi/${vid}/mqdefault.jpg`, // medium (320x180)
    `https://i.ytimg.com/vi/${vid}/hqdefault.jpg`, // high
    `https://i.ytimg.com/vi/${vid}/sddefault.jpg`, // standard
    `https://i.ytimg.com/vi/${vid}/maxresdefault.jpg`, // maxres (may be placeholder)
    `https://i.ytimg.com/vi/${vid}/default.jpg`, // fallback
  ];
}

function isAdclarityUrl(url) {
  return /adclarity/i.test(url || "");
}

function adclarityPosterFromMp4(url) {
  const u = String(url || "");
  if (!/\.mp4(\?|$)/i.test(u)) return "";
  const [base, q] = u.split("?", 2);
  let path = base.replace(/_video(?=\.mp4$)/i, "");
  path = path.replace(/\.mp4$/i, ".jpeg");
  return q ? `${path}?${q}` : path;
}

function adclarityJpgFromJpeg(url) {
  const u = String(url || "");
  return u.replace(/\.jpeg(\?|$)/i, ".jpg$1");
}

function setImgSrcWithFallback(img, sources) {
  const list = (sources || []).filter(Boolean);
  let i = 0;
  const advance = () => {
    while (i < list.length && img.src === list[i]) i += 1;
    if (i >= list.length) return false;
    img.src = list[i];
    i += 1;
    return true;
  };
  img.onerror = () => {
    if (!advance()) img.onerror = null;
  };
  advance();
}

function openInspectModal(item) {
  hideContextMenu();
  const modal = $("inspect-modal");
  const mediaEl = $("inspect-media");
  const metaEl = $("inspect-meta");
  const url = item.mediaUrl || "";
  const thumb = item.thumbUrl || url;

  mediaEl.innerHTML = "";
  if (isTikTokUrl(url)) {
    const img = document.createElement("img");
    img.referrerPolicy = "no-referrer";
    img.src = thumb;
    img.style.cursor = "pointer";
    img.title = "Open TikTok in new tab";
    img.addEventListener("click", () => {
      if (url) window.open(url, "_blank", "noopener,noreferrer");
    });
    if (thumb !== url && url) {
      img.addEventListener(
        "error",
        () => {
          img.src = url;
        },
        { once: true }
      );
    }
    mediaEl.appendChild(img);
  } else if (isYoutubeUrl(url)) {
    const embed = youtubeEmbedUrl(url);
    if (embed) {
      const iframe = document.createElement("iframe");
      iframe.src = embed;
      iframe.title = "YouTube video";
      iframe.allow =
        "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
      iframe.allowFullscreen = true;
      iframe.loading = "lazy";
      iframe.referrerPolicy = "strict-origin-when-cross-origin";
      iframe.style.width = "100%";
      iframe.style.aspectRatio = "16 / 9";
      iframe.style.border = "0";
      mediaEl.appendChild(iframe);
    }
  } else if (isVideoUrl(url)) {
    const video = document.createElement("video");
    video.controls = true;
    video.referrerPolicy = "no-referrer";
    video.src = url;
    video.poster = thumb !== url ? thumb : "";
    mediaEl.appendChild(video);
  } else {
    const img = document.createElement("img");
    img.referrerPolicy = "no-referrer";
    img.src = thumb;
    if (thumb !== url && url) {
      img.addEventListener("error", () => { img.src = url; }, { once: true });
    }
    mediaEl.appendChild(img);
  }

  const meta = item.metadata || {};
  const brandTitle = meta.brand || item.title || "Unknown brand";
  $("inspect-title").textContent = brandTitle;

  const inspectSub = $("inspect-subtitle");
  const advLine = formatAdvertiserSubtitle(
    meta.advertiser_name || item.subtitle
  );
  if (inspectSub) {
    if (advLine) {
      inspectSub.textContent = advLine;
      inspectSub.classList.remove("hidden");
    } else {
      inspectSub.classList.add("hidden");
    }
  }

  metaEl.innerHTML = "";
  const order = [
    "brand",
    "advertiser_name",
    "platform",
    "social_headline_text",
    "social_description",
    "social_campaign_text",
    "creative_campaign_name",
    "creative_video_title",
    "social_page_name",
    "creative_url",
  ];
  for (const key of order) {
    if (key === "brand" || key === "advertiser_name") continue;
    const val = meta[key];
    if (!val) continue;
    const dt = document.createElement("dt");
    dt.textContent = META_LABELS[key] || key;
    const dd = document.createElement("dd");
    if (key === "creative_url") {
      const a = document.createElement("a");
      a.href = val;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = val;
      dd.appendChild(a);
    } else {
      dd.textContent = val;
    }
    metaEl.appendChild(dt);
    metaEl.appendChild(dd);
  }

  modal.classList.remove("hidden");
}

function closeInspectModal() {
  $("inspect-modal").classList.add("hidden");
  const mediaEl = $("inspect-media");
  const video = mediaEl.querySelector("video");
  if (video) video.pause();
  mediaEl.innerHTML = "";
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Stream upload — real progress on Vercel (NDJSON). Falls back to job polling locally. */
async function uploadWithStream(fd) {
  showLoading(true);
  setLoadingProgress(0, "starting", "Uploading file to server…", "");

  try {
    const res = await fetch("/api/upload-stream", { method: "POST", body: fd });
    if (!res.ok) {
      let detail = httpErrorMessage(res.status);
      try {
        const err = await parseJsonResponse(res);
        detail = err.detail || detail;
      } catch (e) {
        if (e.uploadStatus) throw e;
      }
      const err = new Error(typeof detail === "string" ? detail : String(detail));
      err.uploadStatus = res.status;
      throw err;
    }
    if (!res.body) {
      throw new Error("Streaming upload not supported by browser");
    }

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let result = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n");
      buf = parts.pop() || "";
      for (const line of parts) {
        if (!line.trim()) continue;
        let msg;
        try {
          msg = JSON.parse(line);
        } catch {
          continue;
        }
        if (msg.type === "error") {
          throw new Error(msg.detail || "Processing failed");
        }
        const count =
          msg.total > 0
            ? `Row ${msg.current ?? 0} / ${msg.total} · ${msg.downloaded ?? 0} thumbnails`
            : "";
        if (msg.percent != null || msg.phase) {
          let pct = Number(msg.percent ?? 0);
          pct = Math.max(pct, 15);
          setLoadingProgress(pct, msg.phase || "download", msg.hint, count);
        }
        if (msg.type === "complete" || msg.sessionId) {
          result = msg;
        }
      }
    }
    if (buf.trim()) {
      const msg = JSON.parse(buf);
      if (msg.type === "error") throw new Error(msg.detail || "Processing failed");
      if (msg.sessionId) result = msg;
    }
    if (!result?.sessionId) {
      throw new Error("Upload finished without session data");
    }
    setLoadingProgress(100, "done", "Starting review…", "");
    await sleep(300);
    return result;
  } finally {
    showLoading(false);
  }
}

async function uploadFile(fd) {
  try {
    return await uploadWithStream(fd);
  } catch (err) {
    if (err.uploadStatus && isNonRetryableUploadStatus(err.uploadStatus)) {
      throw err;
    }
    console.warn("Stream upload failed, falling back to job polling:", err);
    return uploadWithPolling(fd);
  }
}

/** Poll job status — fallback when streaming is unavailable. */
async function uploadWithPolling(fd) {
  showLoading(true);
  setLoadingProgress(0, "starting", "Uploading file to server…", "");

  const uploadRes = await postFormDataWithProgress("/api/jobs", fd, (ratio) => {
    const pct = Math.max(0, Math.min(15, Math.round(ratio * 15)));
    setLoadingProgress(pct, "receive", "Uploading file to server…", "");
  });
  let start;
  try {
    start = JSON.parse(uploadRes.text || "");
  } catch {
    showLoading(false);
    const err = new Error(httpErrorMessage(uploadRes.status));
    err.uploadStatus = uploadRes.status;
    throw err;
  }
  if (!uploadRes.ok) {
    showLoading(false);
    const err = new Error(
      uploadRes.status === 413
        ? uploadTooLargeMessage(MAX_UPLOAD_BYTES)
        : start.detail || httpErrorMessage(uploadRes.status)
    );
    err.uploadStatus = uploadRes.status;
    throw err;
  }

  const hints = start.hints?.length ? start.hints : [];
  const hintRotateMs = start.hintRotateMs || 5500;
  let hintIdx = 0;
  const hintEl = $("loading-hint");
  if (hints.length && hintEl) {
    hintEl.textContent = hints[0];
  }

  const hintTicker = setInterval(() => {
    if (!hints.length || !hintEl) return;
    hintIdx = (hintIdx + 1) % hints.length;
    hintEl.textContent = hints[hintIdx];
  }, hintRotateMs);

  if (start.status === "complete" && start.result) {
    setLoadingProgress(100, "done", "Starting review…", "");
    await sleep(300);
    return start.result;
  }
  if (start.status === "error" && start.error) {
    throw new Error(start.error);
  }

  const jobId = start.jobId;

  try {
    const pollStart = Date.now();
    while (true) {
      const elapsed = Date.now() - pollStart;
      // Faster early polling improves perceived progress on Vercel cold starts.
      const pollMs = elapsed < 2500 ? 200 : 600;
      await sleep(pollMs);
      const pollRes = await fetch(`/api/jobs/${jobId}`);
      const st = await parseJsonResponse(pollRes);
      if (!pollRes.ok) {
        throw new Error(st.detail || st.error || "Failed to get job status");
      }

      const count =
        st.total > 0
          ? `Row ${st.current ?? 0} / ${st.total} · ${st.downloaded ?? 0} thumbnails`
          : "";

      const phase = st.phase || "download";
      let pct = Number.isFinite(st.percent) ? Number(st.percent) : 0;
      // Phase-based floor so UI doesn't feel stuck even if server percent is coarse.
      if (phase === "parse") pct = Math.max(pct, 20);
      else if (phase === "group") pct = Math.max(pct, 25);
      else if (phase === "download") {
        const total = Number(st.total || 0);
        const cur = Number(st.current || 0);
        if ((!pct || pct < 30) && total > 0) {
          pct = 30 + Math.min(65, (cur / total) * 65); // 30 → 95
        }
        pct = Math.max(pct, 30);
      }
      // Never regress below upload progress segment.
      pct = Math.max(pct, 15);
      setLoadingProgress(pct, phase, undefined, count);

      if (st.status === "complete" && st.result) {
        setLoadingProgress(100, "done", "Starting review…", count);
        await sleep(300);
        return st.result;
      }
      if (st.status === "error") {
        throw new Error(st.error || "Processing failed");
      }
    }
  } finally {
    clearInterval(hintTicker);
    showLoading(false);
  }
}

function resetForNewUpload() {
  destroyVirtualGrid();
  sessionId = null;
  brandGroups = [];
  uncertainItems = [];
  unavailableMedia = null;
  cursor = 0;
  mode = "unavailable";
  reviewing = false;
  $("file-input").value = "";
  setFileSelectedName("");
  resetColumnConfig();
  uploadedFilename = "";
  setReviewFilename("");
  setReviewStatus("");
  setDoneExportStatus("");
  doneSection.classList.add("hidden");
  reviewSection.classList.add("hidden");
  uploadSection.classList.remove("hidden");
}

function goHome(e) {
  e?.preventDefault();
  showLoading(false);
  closeInspectModal();
  closeCoffeeModal();
  resetForNewUpload();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function parseJsonResponse(res) {
  const text = await res.text();
  if (!text) {
    throw new Error(
      res.ok
        ? "Empty response from server"
        : res.status === 413
          ? uploadTooLargeMessage(MAX_UPLOAD_BYTES)
          : `Server error (${res.status}). The server may have restarted during upload — try again.`
    );
  }
  try {
    return JSON.parse(text);
  } catch {
    const err = new Error(
      res.status === 413 ? uploadTooLargeMessage(MAX_UPLOAD_BYTES) : httpErrorMessage(res.status)
    );
    err.uploadStatus = res.status;
    throw err;
  }
}

async function submitReview(leftAction) {
  if (reviewing) return;
  const queue = currentQueue();
  if (cursor >= queue.length) return;

  if (mode === "brand" && leftAction) return;

  reviewing = true;
  setReviewWait(true);
  const item = queue[cursor];

  card.classList.add(leftAction ? "swipe-left" : "swipe-right");
  card.style.transform = leftAction
    ? "translateX(-120%) rotate(-12deg)"
    : "translateX(120%) rotate(12deg)";
  card.style.opacity = "0";

  try {
    if (mode === "unavailable") {
      applyUnavailableReview(leftAction);
    } else if (mode === "uncertain") {
      applyUncertainGroupReview(item, !leftAction);
    } else {
      applyBrandGroupReview(item);
    }
    setReviewStatus("");
    cursor += 1;
    setTimeout(showCard, 220);
  } catch (err) {
    reviewing = false;
    setReviewWait(false);
    card.style.transform = "";
    card.style.opacity = "1";
    card.classList.remove("swipe-left", "swipe-right");
    setReviewStatus(err.message || String(err));
  }
}

function exportResults() {
  if (!sessionId) return;
  if (!doneSection || doneSection.classList.contains("hidden")) {
    setReviewStatus("Export is available after review completes.");
    return;
  }

  const btnDone = $("export-btn-done");
  const btnReview = $("export-btn");
  if (btnDone) btnDone.disabled = true;
  if (btnReview) btnReview.disabled = true;
  setDoneExportStatus("Building export…");

  (async () => {
    try {
      await exportResultsClient();
      setDoneExportStatus("Download started.");
    } catch (err) {
      setDoneExportStatus(err.message || String(err));
    } finally {
      if (btnDone) btnDone.disabled = false;
      if (btnReview) btnReview.disabled = false;
    }
  })();
}

document.addEventListener("keydown", (e) => {
  if (reviewSection.classList.contains("hidden") || reviewing) return;
  if (e.key === "ArrowLeft") {
    e.preventDefault();
    if (isGridReviewMode()) {
      if (mode === "uncertain") submitReview(true);
      else markAllGridFaults();
    } else {
      submitReview(true);
    }
  } else if (e.key === "ArrowRight") {
    e.preventDefault();
    submitReview(false);
  }
});

let startX = 0;
let currentX = 0;
let dragging = false;

function onSwipePointerMove(e) {
  if (!dragging) return;
  currentX = e.clientX;
  const dx = currentX - startX;
  card.style.transform = `translateX(${dx}px) rotate(${dx * 0.05}deg)`;
  card.classList.toggle("swipe-left", dx < -40);
  card.classList.toggle("swipe-right", dx > 40);
}

function endSwipeGesture() {
  document.removeEventListener("pointermove", onSwipePointerMove);
  if (!dragging) return;
  dragging = false;
  const dx = currentX - startX;
  if (mode === "brand") {
    if (dx > 80) submitReview(false);
  } else if (dx < -80) {
    submitReview(true);
  } else if (dx > 80) {
    submitReview(false);
  } else {
    card.style.transform = "";
    card.classList.remove("swipe-left", "swipe-right");
  }
}

function bindSwipeZones() {
  if (!card || card.dataset.swipeBound === "1") return;
  card.dataset.swipeBound = "1";

  card.addEventListener("pointerdown", (e) => {
    if (reviewing || e.button !== 0) return;
    if (e.target.closest(".thumb-cell, .thumb-grid")) return;
    if (mode === "unavailable" && e.target.closest(".unavailable-table a")) return;
    if (mode !== "unavailable" && !e.target.closest(".card-swipe-zone")) return;

    dragging = true;
    startX = e.clientX;
    currentX = startX;
    document.addEventListener("pointermove", onSwipePointerMove);
    document.addEventListener("pointerup", endSwipeGesture, { once: true });
  });
}

bindGridInteractions();
bindSwipeZones();

btnFault.addEventListener("click", () => {
  if (isGridReviewMode()) markAllGridFaults();
  else submitReview(true);
});
btnOk.addEventListener("click", () => submitReview(false));
$("export-btn").addEventListener("click", exportResults);
$("export-btn-done").addEventListener("click", exportResults);
$("upload-another-btn").addEventListener("click", resetForNewUpload);

const fileInput = $("file-input");
if (fileInput) {
  fileInput.addEventListener("change", () => {
    const f = fileInput.files && fileInput.files[0];
    setFileSelectedName(f ? f.name : "");
    if (f) {
      previewFileColumns(f);
    } else {
      resetColumnConfig();
      setStatus("");
    }
  });
}

$("ctx-inspect").addEventListener("click", () => {
  if (contextItem) openInspectModal(enrichInspectItem(contextItem));
});
$("inspect-close").addEventListener("click", closeInspectModal);
$("inspect-modal").querySelector(".inspect-backdrop").addEventListener("click", closeInspectModal);

function setCoffeeTab(tabEl) {
  document.querySelectorAll(".coffee-tab").forEach((btn) => {
    const active = btn === tabEl;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  const qr = $("coffee-qr");
  const src = tabEl.dataset.qr;
  const label = tabEl.id === "coffee-tab-vn" ? "Vietnam" : "Global";
  qr.src = src;
  qr.alt = `${label} payment QR code`;
  const panel = $("coffee-qr-panel");
  panel.setAttribute("aria-labelledby", tabEl.id);
}

function openCoffeeModal() {
  setCoffeeTab($("coffee-tab-global"));
  $("coffee-modal").classList.remove("hidden");
}

function closeCoffeeModal() {
  $("coffee-modal").classList.add("hidden");
}

$("home-logo-link").addEventListener("click", goHome);

$("bmc-link").addEventListener("click", (e) => {
  e.preventDefault();
  openCoffeeModal();
});
$("coffee-close").addEventListener("click", closeCoffeeModal);
$("coffee-modal").querySelector(".coffee-backdrop").addEventListener("click", closeCoffeeModal);
document.querySelectorAll(".coffee-tab").forEach((tab) => {
  tab.addEventListener("click", () => setCoffeeTab(tab));
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("coffee-modal").classList.contains("hidden")) {
    closeCoffeeModal();
  }
});

$("context-menu").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => hideContextMenu());
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeInspectModal();
    hideContextMenu();
  }
});

$("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fileInput = $("file-input");
  if (!fileInput.files.length) return;

  const sizeErr = validateUploadFile(fileInput.files[0]);
  if (sizeErr) {
    setStatus(sizeErr);
    return;
  }

  const btn = $("upload-btn");
  btn.disabled = true;
  setStatus("Downloading media and grouping by brand…");

  uploadedFilename = fileInput.files[0]?.name || "";
  setReviewFilename(uploadedFilename);

  const urlCol = $("url-column");
  const brandCol = $("brand-column");
  if (!urlCol?.value || !brandCol?.value) {
    setStatus("Select URL and brand columns before uploading.");
    return;
  }

  const fd = new FormData();
  const file = fileInput.files[0];
  fd.append("file", file);
  fd.append("url_column", urlCol.value);
  fd.append("brand_column", brandCol.value);
  fd.append("k_groups", "0");

  try {
    let sourceSnapshot;
    try {
      sourceSnapshot = await parseSourceSnapshot(file, urlCol.value, brandCol.value);
    } catch (parseErr) {
      throw new Error(parseErr.message || "Could not read spreadsheet in browser.");
    }

    const data = await uploadFile(fd);

    sessionId = data.sessionId;
    let exportCacheWarn = "";
    try {
      await saveSourceSnapshot(sessionId, sourceSnapshot);
    } catch (saveErr) {
      console.warn("Source snapshot save failed:", saveErr);
      exportCacheWarn = " · Could not cache file for export in this browser.";
    }
    brandGroups = data.groups || [];
    uncertainItems = normalizeUncertainItems(data.uncertain || []);
    unavailableMedia = data.unavailable || null;
    initReviewStore(data);
    applyReviewStoreToQueues();
    cursor = 0;
    mode = unavailableMedia
      ? "unavailable"
      : uncertainItems.length > 0
        ? "uncertain"
        : "brand";

    const unavail = data.unavailableCount || 0;
    let statusMsg =
      `Loaded ${data.totalRows} rows · ${data.groupCount} brand group(s)` +
      (unavail ? ` · ${unavail} unavailable` : "") +
      (data.uncertainCount ? ` · ${data.uncertainCount} uncertain` : "");
    const warnings = data.warnings || [];
    if (warnings.length) {
      statusMsg += ` · Warning: ${warnings[0]}`;
    }
    setStatus(statusMsg);
    setReviewStatus(
      "Review saved in this browser. Export works offline — no server needed." + exportCacheWarn
    );
    uploadSection.classList.add("hidden");
    reviewSection.classList.remove("hidden");
    doneSection.classList.add("hidden");
    $("export-btn")?.classList.add("hidden");
    setUiForMode();
    showCard();
  } catch (err) {
    showLoading(false);
    setStatus(err.message || String(err));
  } finally {
    btn.disabled = false;
  }
});
