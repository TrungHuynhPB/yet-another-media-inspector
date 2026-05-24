let sessionId = null;
let brandGroups = [];
let uncertainItems = [];
let mode = "individual";
let cursor = 0;
let reviewing = false;

const $ = (id) => document.getElementById(id);

const uploadSection = $("upload-section");
const reviewSection = $("review-section");
const doneSection = $("done-section");
const card = $("card");
const cardTitle = $("card-title");
const cardSubtitle = $("card-subtitle");
const cardGrid = $("card-grid");
const cardSingle = $("card-single");
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
  return mode === "individual" ? uncertainItems : brandGroups;
}

function renderSingleImage(container, items) {
  container.innerHTML = "";
  items.forEach((item, i) => {
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
    container.appendChild(img);
  });
}

function renderBrandGrid(container, items) {
  container.innerHTML = "";
  items.forEach((item) => {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "thumb-cell" + (item.isFault ? " is-fault" : "");
    cell.dataset.rowIndex = String(item.rowIndex);
    cell.setAttribute("aria-pressed", item.isFault ? "true" : "false");
    cell.setAttribute("aria-label", item.isFault ? "Marked fault — click to clear" : "Mark as fault");

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
    cell.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleImageFault(item, cell);
    });
    container.appendChild(cell);
  });
}

function updateBrandCountText() {
  const g = brandGroups[cursor];
  if (!g?.items) return;
  const faultCount = g.items.filter((i) => i.isFault).length;
  const active = g.items.length - faultCount;
  cardCount.textContent =
    `Brand ${cursor + 1} of ${brandGroups.length} · ${active} active · ${faultCount} marked fault · tap image to toggle`;
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

    item.isFault = data.isFault;
    cell.classList.toggle("is-fault", item.isFault);
    cell.setAttribute("aria-pressed", item.isFault ? "true" : "false");
    cell.setAttribute(
      "aria-label",
      item.isFault ? "Marked fault — click to clear" : "Mark as fault"
    );
    updateBrandCountText();
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

function setUiForMode() {
  const isIndividual = mode === "individual";
  cardGrid.classList.toggle("hidden", isIndividual);
  cardSingle.classList.toggle("hidden", !isIndividual);

  if (isIndividual) {
    hintLeft.textContent = "← Wrong brand";
    hintRight.textContent = "Correct brand →";
    btnFault.textContent = "✕ Wrong brand";
    btnOk.textContent = "✓ Correct brand";
    modeLabel.textContent = "Step 1 · Verify uncertain ads (one at a time)";
  } else {
    hintLeft.textContent = "← Fault (whole group)";
    hintRight.textContent = "OK (whole group) →";
    btnFault.textContent = "✕ Fault group";
    btnOk.textContent = "✓ OK group";
    modeLabel.textContent = "Step 2 · Review brand groups";
  }
}

function showCard() {
  reviewing = false;
  const queue = currentQueue();

  if (mode === "individual" && cursor >= uncertainItems.length) {
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

  if (mode === "individual") {
    cardTitle.textContent = `Advertiser: ${item.title}`;
    const reason =
      item.uncertainReason === "visual_outlier"
        ? "This creative looks different from others under the same brand label."
        : item.uncertainReason === "missing_advertiser"
          ? "No advertiser name on this row."
          : "Please confirm the brand label matches this creative.";
    cardSubtitle.textContent = reason;
    cardSubtitle.classList.remove("hidden");
    cardCount.textContent = `Uncertain ${cursor + 1} of ${uncertainItems.length}`;
    renderSingleImage(cardSingle, items);
  } else {
    cardTitle.textContent = item.title;
    cardSubtitle.textContent = "Tap any image to mark it as fault (gray overlay + ✕).";
    cardSubtitle.classList.remove("hidden");
    renderBrandGrid(cardGrid, items);
    updateBrandCountText();
  }

  const totalSteps = uncertainItems.length + brandGroups.length;
  const doneSteps =
    (mode === "brand" ? uncertainItems.length : 0) +
    (mode === "individual" ? cursor : cursor + uncertainItems.length);
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

function resetForNewUpload() {
  sessionId = null;
  brandGroups = [];
  uncertainItems = [];
  cursor = 0;
  mode = "individual";
  reviewing = false;
  $("file-input").value = "";
  setStatus("");
  doneSection.classList.add("hidden");
  reviewSection.classList.add("hidden");
  uploadSection.classList.remove("hidden");
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
    if (mode === "individual") {
      await fetch(`/api/session/${sessionId}/review-item`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rowIndex: item.rowIndex,
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

card.addEventListener("pointerdown", (e) => {
  if (reviewing || e.target.closest(".thumb-cell")) return;
  dragging = true;
  startX = e.clientX;
  currentX = startX;
  card.setPointerCapture(e.pointerId);
});

card.addEventListener("pointermove", (e) => {
  if (!dragging) return;
  currentX = e.clientX;
  const dx = currentX - startX;
  card.style.transform = `translateX(${dx}px) rotate(${dx * 0.05}deg)`;
  card.classList.toggle("swipe-left", dx < -40);
  card.classList.toggle("swipe-right", dx > 40);
});

card.addEventListener("pointerup", () => {
  if (!dragging) return;
  dragging = false;
  const dx = currentX - startX;
  if (dx < -80) submitReview(true);
  else if (dx > 80) submitReview(false);
  else {
    card.style.transform = "";
    card.classList.remove("swipe-left", "swipe-right");
  }
});

btnFault.addEventListener("click", () => submitReview(true));
btnOk.addEventListener("click", () => submitReview(false));
$("export-btn").addEventListener("click", exportResults);
$("export-btn-done").addEventListener("click", exportResults);
$("upload-another-btn").addEventListener("click", resetForNewUpload);

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
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    const data = await parseJsonResponse(res);
    if (!res.ok) {
      const detail = data.detail;
      const msg = Array.isArray(detail)
        ? detail.map((d) => d.msg).join("; ")
        : detail || "Upload failed";
      throw new Error(msg);
    }

    sessionId = data.sessionId;
    brandGroups = data.groups || [];
    uncertainItems = data.uncertain || [];
    cursor = 0;
    mode = uncertainItems.length > 0 ? "individual" : "brand";

    setStatus(
      `Loaded ${data.totalRows} rows · ${data.groupCount} brand(s) · ${data.uncertainCount} to verify individually`
    );
    uploadSection.classList.add("hidden");
    reviewSection.classList.remove("hidden");
    doneSection.classList.add("hidden");
    setUiForMode();
    showCard();
  } catch (err) {
    setStatus(err.message || String(err));
  } finally {
    btn.disabled = false;
  }
});
