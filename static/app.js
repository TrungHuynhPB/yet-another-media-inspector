let sessionId = null;
let brandGroups = [];
let uncertainItems = [];
let unavailableMedia = null;
let mode = "unavailable"; // unavailable | uncertain | brand
let cursor = 0;
let reviewing = false;
let contextItem = null;

const RING_CIRCUMFERENCE = 2 * Math.PI * 52;

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

function setStatus(msg) {
  $("upload-status").textContent = msg;
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
    img.src = thumbUrl || mediaUrl;
    if (thumbUrl && mediaUrl && thumbUrl !== mediaUrl) {
      img.addEventListener("error", () => { img.src = mediaUrl; }, { once: true });
    }

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
  container.innerHTML = "";
  items.forEach((item) => {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "thumb-cell";
    cell.dataset.rowIndex = String(item.rowIndex);

    const img = document.createElement("img");
    img.alt = "Creative";
    img.loading = "lazy";
    img.draggable = false;
    img.referrerPolicy = "no-referrer";
    const mediaUrl = item.mediaUrl || item.thumbUrl;
    const thumbUrl = item.thumbUrl;
    img.src = thumbUrl || mediaUrl;
    if (thumbUrl && mediaUrl && thumbUrl !== mediaUrl) {
      img.addEventListener("error", () => { img.src = mediaUrl; }, { once: true });
    }

    const overlay = document.createElement("div");
    overlay.className = "fault-overlay";
    overlay.innerHTML = '<span class="fault-x" aria-hidden="true">✕</span>';

    cell.appendChild(img);
    cell.appendChild(overlay);
    cell._item = item;
    applyFaultUi(item, cell, Boolean(item.isFault));
    container.appendChild(cell);
  });
}

function updateGroupCountText() {
  const queue = currentQueue();
  const g = queue[cursor];
  if (!g?.items) return;
  const faultCount = g.items.filter((i) => i.isFault).length;
  const active = g.items.length - faultCount;
  const label = mode === "uncertain" ? "Uncertain brand" : "Brand";
  cardCount.textContent =
    `${label} ${cursor + 1} of ${queue.length} · ${active} active · ${faultCount} marked fault · tap image to toggle`;
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
  const nextFault = !item.isFault;
  try {
    const res = await fetch(`/api/session/${sessionId}/toggle-fault`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rowIndex: item.rowIndex, isFault: nextFault }),
    });
    const data = await parseJsonResponse(res);
    if (!res.ok) throw new Error(data.detail || "Update failed");

    applyFaultUi(item, cell, data.isFault);
    updateGroupCountText();
  } catch (err) {
    setStatus(err.message || String(err));
  }
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
    btnOk.textContent = "✓ Continue";
    modeLabel.textContent = "Unavailable media — review table, then swipe";
  } else if (isUncertain) {
    hintLeft.textContent = "← Wrong brand (group)";
    hintRight.textContent = "Correct brand (group) →";
    btnFault.textContent = "✕ Wrong brand";
    btnOk.textContent = "✓ Correct brand";
    modeLabel.textContent =
      "Uncertain ads · grouped by brand (grid: tap ✕ fault, right-click inspect)";
  } else {
    hintLeft.textContent = "← Fault (whole group)";
    hintRight.textContent = "OK (whole group) →";
    btnFault.textContent = "✕ Fault group";
    btnOk.textContent = "✓ OK group";
    modeLabel.textContent = "Review brand groups";
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
    const items = item.items?.length ? item.items : [];

    const gridHint =
      "Tap image to toggle ✕ fault · Right-click to inspect · Swipe for whole group";
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
}

function showLoading(show) {
  $("loading-overlay").classList.toggle("hidden", !show);
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

function openInspectModal(item) {
  hideContextMenu();
  const modal = $("inspect-modal");
  const mediaEl = $("inspect-media");
  const metaEl = $("inspect-meta");
  const url = item.mediaUrl || "";
  const thumb = item.thumbUrl || url;

  mediaEl.innerHTML = "";
  if (isVideoUrl(url)) {
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

/** Poll job status — reliable progress (streaming often buffers in browsers). */
async function uploadWithPolling(fd) {
  showLoading(true);
  setLoadingProgress(0, "starting", "Uploading file to server…", "");

  const res = await fetch("/api/jobs", { method: "POST", body: fd });
  const start = await parseJsonResponse(res);
  if (!res.ok) {
    showLoading(false);
    throw new Error(start.detail || "Upload failed");
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

  const jobId = start.jobId;

  try {
    while (true) {
      await sleep(500);
      const pollRes = await fetch(`/api/jobs/${jobId}`);
      const st = await parseJsonResponse(pollRes);
      if (!pollRes.ok) {
        throw new Error(st.detail || st.error || "Failed to get job status");
      }

      const count =
        st.total > 0
          ? `Row ${st.current ?? 0} / ${st.total} · ${st.downloaded ?? 0} thumbnails`
          : "";

      setLoadingProgress(st.percent ?? 0, st.phase || "download", undefined, count);

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
  sessionId = null;
  brandGroups = [];
  uncertainItems = [];
  unavailableMedia = null;
  cursor = 0;
  mode = "unavailable";
  reviewing = false;
  $("file-input").value = "";
  setStatus("");
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
        : `Server error (${res.status}). The server may have restarted during upload — try again.`
    );
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`Invalid server response (${res.status})`);
  }
}

async function submitReview(leftAction) {
  if (reviewing) return;
  const queue = currentQueue();
  if (cursor >= queue.length) return;

  reviewing = true;
  const item = queue[cursor];

  card.classList.add(leftAction ? "swipe-left" : "swipe-right");
  card.style.transform = leftAction
    ? "translateX(-120%) rotate(-12deg)"
    : "translateX(120%) rotate(12deg)";
  card.style.opacity = "0";

  try {
    if (mode === "unavailable") {
      await fetch(`/api/session/${sessionId}/review-unavailable`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ isFault: leftAction }),
      });
    } else if (mode === "uncertain") {
      await fetch(`/api/session/${sessionId}/review-uncertain`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          groupId: Number(item.groupId),
          advertiserMatch: !leftAction,
        }),
      });
    } else {
      await fetch(`/api/session/${sessionId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ groupId: item.groupId, isFault: leftAction }),
      });
    }
    cursor += 1;
    setTimeout(showCard, 220);
  } catch {
    reviewing = false;
    card.style.transform = "";
    card.style.opacity = "1";
    card.classList.remove("swipe-left", "swipe-right");
  }
}

function exportResults() {
  if (!sessionId) return;
  window.location.href = `/api/session/${sessionId}/export`;
}

document.addEventListener("keydown", (e) => {
  if (reviewSection.classList.contains("hidden") || reviewing) return;
  if (e.key === "ArrowLeft") {
    e.preventDefault();
    submitReview(true);
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
  if (dx < -80) submitReview(true);
  else if (dx > 80) submitReview(false);
  else {
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

btnFault.addEventListener("click", () => submitReview(true));
btnOk.addEventListener("click", () => submitReview(false));
$("export-btn").addEventListener("click", exportResults);
$("export-btn-done").addEventListener("click", exportResults);
$("upload-another-btn").addEventListener("click", resetForNewUpload);

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

  const btn = $("upload-btn");
  btn.disabled = true;
  setStatus("Downloading media and grouping by brand…");

  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  fd.append("url_column", $("url-column").value.trim());
  fd.append("k_groups", "0");

  try {
    const data = await uploadWithPolling(fd);

    sessionId = data.sessionId;
    brandGroups = data.groups || [];
    uncertainItems = normalizeUncertainItems(data.uncertain || []);
    unavailableMedia = data.unavailable || null;
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
    uploadSection.classList.add("hidden");
    reviewSection.classList.remove("hidden");
    doneSection.classList.add("hidden");
    setUiForMode();
    showCard();
  } catch (err) {
    showLoading(false);
    setStatus(err.message || String(err));
  } finally {
    btn.disabled = false;
  }
});
