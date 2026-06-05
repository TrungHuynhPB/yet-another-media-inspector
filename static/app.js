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

function setStatus(msg) {
  $("upload-status").textContent = msg;
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

const GRID_BATCH_SIZE = 48;
const TIKTOK_THUMB_CONCURRENCY = 2;
let gridAllItems = [];
let gridLoadObserver = null;
let tiktokThumbObserver = null;
let claptikConfig = null;
let claptikTurnstileToken = "";
const tiktokThumbCache = new Map();
const tiktokThumbQueue = [];
let tiktokThumbInflight = 0;

function disconnectGridInfiniteScroll() {
  if (gridLoadObserver) {
    gridLoadObserver.disconnect();
    gridLoadObserver = null;
  }
}

function needsLazyTikTokThumb(item) {
  if (!item || item.needsClientThumb) return Boolean(item?.needsClientThumb);
  const url = item.mediaUrl || "";
  if (!isTikTokUrl(url)) return false;
  const thumb = item.thumbUrl || "";
  if (!thumb) return true;
  if (thumb.includes("/api/thumb/")) return false;
  return thumb === url;
}

async function loadClaptikConfig() {
  if (claptikConfig) return claptikConfig;
  try {
    const res = await fetch("/api/claptik-config");
    if (!res.ok) return { enabled: false };
    claptikConfig = await res.json();
  } catch {
    claptikConfig = { enabled: false };
  }
  return claptikConfig;
}

function ensureTurnstileScript() {
  return new Promise((resolve, reject) => {
    if (window.turnstile) {
      resolve();
      return;
    }
    const existing = document.querySelector('script[src*="turnstile/v0/api.js"]');
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error("Turnstile failed")), {
        once: true,
      });
      return;
    }
    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Turnstile failed"));
    document.head.appendChild(script);
  });
}

async function ensureClaptikTurnstile() {
  if (claptikTurnstileToken) return claptikTurnstileToken;
  const cfg = await loadClaptikConfig();
  if (!cfg.enabled || !cfg.hasTurnstile || !cfg.turnstileSiteKey) return "";

  const modal = $("claptik-turnstile-modal");
  const mount = $("claptik-turnstile-mount");
  if (!modal || !mount) return "";

  try {
    await ensureTurnstileScript();
  } catch {
    return "";
  }

  return new Promise((resolve) => {
    modal.classList.remove("hidden");
    mount.innerHTML = "";
    try {
      window.turnstile.render(mount, {
        sitekey: cfg.turnstileSiteKey,
        callback: (token) => {
          claptikTurnstileToken = token || "";
          modal.classList.add("hidden");
          resolve(claptikTurnstileToken);
        },
        "error-callback": () => {
          modal.classList.add("hidden");
          resolve("");
        },
        "expired-callback": () => {
          claptikTurnstileToken = "";
        },
      });
    } catch {
      modal.classList.add("hidden");
      resolve("");
    }
  });
}

async function fetchClaptikThumb(mediaUrl) {
  if (!mediaUrl) return null;
  if (tiktokThumbCache.has(mediaUrl)) return tiktokThumbCache.get(mediaUrl);

  const cfg = await loadClaptikConfig();
  if (!cfg.enabled) return null;

  let turnstile = claptikTurnstileToken;
  if (cfg.hasTurnstile && !turnstile) {
    turnstile = await ensureClaptikTurnstile();
  }
  if (cfg.hasTurnstile && !turnstile) return null;

  try {
    const res = await fetch("/api/claptik-thumb", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: mediaUrl, turnstile }),
    });
    if (!res.ok) return null;
    const data = await res.json();
    const thumb = data.thumbUrl || null;
    if (thumb) tiktokThumbCache.set(mediaUrl, thumb);
    return thumb;
  } catch {
    return null;
  }
}

function drainTikTokThumbQueue() {
  while (tiktokThumbInflight < TIKTOK_THUMB_CONCURRENCY && tiktokThumbQueue.length) {
    const job = tiktokThumbQueue.shift();
    tiktokThumbInflight += 1;
    loadTikTokThumbForCell(job).finally(() => {
      tiktokThumbInflight -= 1;
      drainTikTokThumbQueue();
    });
  }
}

function queueTikTokThumb(cell, mediaUrl, img) {
  tiktokThumbQueue.push({ cell, mediaUrl, img });
  drainTikTokThumbQueue();
}

async function loadTikTokThumbForCell({ cell, mediaUrl, img }) {
  if (cell.dataset.tiktokThumbLoaded === "1") return;
  cell.dataset.tiktokThumbLoaded = "loading";
  const thumb = await fetchClaptikThumb(mediaUrl);
  if (!thumb) {
    cell.dataset.tiktokThumbLoaded = "failed";
    return;
  }
  img.referrerPolicy = "no-referrer";
  img.src = thumb;
  cell.classList.remove("thumb-cell--tiktok-pending");
  cell.dataset.tiktokThumbLoaded = "1";
}

function disconnectLazyTikTokThumbs() {
  if (tiktokThumbObserver) {
    tiktokThumbObserver.disconnect();
    tiktokThumbObserver = null;
  }
}

function observeLazyTikTokThumbs(container) {
  disconnectLazyTikTokThumbs();
  const cells = container.querySelectorAll("[data-needs-tiktok-thumb='1']");
  if (!cells.length) return;

  tiktokThumbObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const cell = entry.target;
        if (cell.dataset.tiktokThumbLoaded) {
          tiktokThumbObserver.unobserve(cell);
          continue;
        }
        tiktokThumbObserver.unobserve(cell);
        const img = cell.querySelector("img");
        const mediaUrl = cell.dataset.mediaUrl || "";
        if (img && mediaUrl) queueTikTokThumb(cell, mediaUrl, img);
      }
    },
    { root: container, rootMargin: "120px", threshold: 0.01 }
  );
  for (const cell of cells) tiktokThumbObserver.observe(cell);
}

function createThumbCell(item) {
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
  const lazyTikTok = needsLazyTikTokThumb(item);

  if (lazyTikTok) {
    cell.classList.add("thumb-cell--tiktok-pending");
    cell.dataset.needsTiktokThumb = "1";
    cell.dataset.mediaUrl = item.mediaUrl || "";
    img.alt = "TikTok preview";
  } else {
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
  }

  const overlay = document.createElement("div");
  overlay.className = "fault-overlay";
  overlay.innerHTML = '<span class="fault-x" aria-hidden="true">✕</span>';

  cell.appendChild(img);
  cell.appendChild(overlay);
  cell._item = item;
  applyFaultUi(item, cell, Boolean(item.isFault));
  return cell;
}

function appendGridBatch(container) {
  const sentinel = container.querySelector(".grid-scroll-sentinel");
  if (!sentinel) return;

  const rendered = parseInt(container.dataset.renderedCount || "0", 10);
  if (rendered >= gridAllItems.length) return;

  const batch = gridAllItems.slice(rendered, rendered + GRID_BATCH_SIZE);
  for (const item of batch) {
    container.insertBefore(createThumbCell(item), sentinel);
  }
  observeLazyTikTokThumbs(container);

  const nextCount = rendered + batch.length;
  container.dataset.renderedCount = String(nextCount);

  if (nextCount >= gridAllItems.length) {
    sentinel.textContent = "";
    sentinel.classList.add("grid-scroll-done");
  } else {
    sentinel.textContent = `Scroll for more… (${nextCount} of ${gridAllItems.length} shown)`;
    sentinel.classList.remove("grid-scroll-done");
  }
}

function bindGridInfiniteScroll(container) {
  disconnectGridInfiniteScroll();
  const sentinel = container.querySelector(".grid-scroll-sentinel");
  if (!sentinel || gridAllItems.length <= GRID_BATCH_SIZE) {
    if (sentinel) sentinel.classList.add("grid-scroll-done");
    return;
  }

  gridLoadObserver = new IntersectionObserver(
    (entries) => {
      if (!entries[0]?.isIntersecting) return;
      appendGridBatch(container);
    },
    { root: container, rootMargin: "240px", threshold: 0 }
  );
  gridLoadObserver.observe(sentinel);
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
  disconnectGridInfiniteScroll();
  disconnectLazyTikTokThumbs();
  gridAllItems = items || [];
  container.innerHTML = "";
  container.dataset.renderedCount = "0";
  container.scrollTop = 0;

  if (!gridAllItems.length) return;

  const sentinel = document.createElement("div");
  sentinel.className = "grid-scroll-sentinel";
  sentinel.setAttribute("aria-hidden", "true");
  container.appendChild(sentinel);

  appendGridBatch(container);
  bindGridInfiniteScroll(container);
  observeLazyTikTokThumbs(container);
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
  if (cell.dataset.toggling === "1") return;
  cell.dataset.toggling = "1";
  const prevFault = Boolean(item.isFault);
  const nextFault = !prevFault;
  applyFaultUi(item, cell, nextFault);
  updateGroupCountText();
  try {
    const res = await fetch(`/api/session/${sessionId}/toggle-fault`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rowIndex: item.rowIndex, isFault: nextFault }),
    });
    const data = await parseJsonResponse(res);
    if (!res.ok) throw new Error(data.detail || "Update failed");

    applyFaultUi(item, cell, Boolean(data.isFault));
    updateGroupCountText();
  } catch (err) {
    applyFaultUi(item, cell, prevFault);
    updateGroupCountText();
    setStatus(err.message || String(err));
  } finally {
    delete cell.dataset.toggling;
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
    hintLeft.textContent = "← Flag all as Incorrect";
    hintRight.textContent = "Correct brand (group) →";
    btnFault.textContent = "✕ Flag all as Incorrect";
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
    const items = item.items?.length ? item.items : [];

    const gridHint =
      items.length > GRID_BATCH_SIZE
        ? "Scroll grid for more creatives · Tap ✕ fault · Right-click inspect · Swipe whole group"
        : "Tap image to toggle ✕ fault · Right-click to inspect · Swipe for whole group";
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
  sessionId = null;
  brandGroups = [];
  uncertainItems = [];
  unavailableMedia = null;
  cursor = 0;
  mode = "unavailable";
  reviewing = false;
  claptikConfig = null;
  claptikTurnstileToken = "";
  tiktokThumbCache.clear();
  tiktokThumbQueue.length = 0;
  $("file-input").value = "";
  setFileSelectedName("");
  uploadedFilename = "";
  setReviewFilename("");
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
    setReviewWait(false);
    card.style.transform = "";
    card.style.opacity = "1";
    card.classList.remove("swipe-left", "swipe-right");
  }
}

function exportResults() {
  if (!sessionId) return;
  if (!doneSection || doneSection.classList.contains("hidden")) {
    setStatus("Export is available after review completes.");
    return;
  }
  (async () => {
    try {
      const res = await fetch(`/api/session/${sessionId}/export`);
      if (!res.ok) {
        const err = await parseJsonResponse(res).catch(() => ({}));
        throw new Error(err.detail || `Export failed (${res.status})`);
      }
      const blob = await res.blob();
      if (!blob.size) {
        throw new Error("Export returned an empty file");
      }
      let filename = "media_inspector_output.xlsx";
      const disp = res.headers.get("Content-Disposition") || "";
      const m = /filename="?([^";\n]+)"?/i.exec(disp);
      if (m) filename = m[1].trim();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus("Download started.");
    } catch (err) {
      setStatus(err.message || String(err));
    }
  })();
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

const fileInput = $("file-input");
if (fileInput) {
  fileInput.addEventListener("change", () => {
    const f = fileInput.files && fileInput.files[0];
    setFileSelectedName(f ? f.name : "");
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
function closeClaptikTurnstileModal() {
  $("claptik-turnstile-modal")?.classList.add("hidden");
}

$("claptik-turnstile-close")?.addEventListener("click", closeClaptikTurnstileModal);
$("claptik-turnstile-modal")
  ?.querySelector(".claptik-turnstile-backdrop")
  ?.addEventListener("click", closeClaptikTurnstileModal);

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
    closeClaptikTurnstileModal();
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

  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  fd.append("url_column", $("url-column").value.trim());
  fd.append("k_groups", "0");

  try {
    const data = await uploadFile(fd);

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
