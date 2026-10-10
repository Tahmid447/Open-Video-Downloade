const $ = (id) => document.getElementById(id);
let currentUrl = "";
let activeJobId = null;
let completedJobId = null;
let completedDownloadUrl = null;
let lastCompletedFilename = "";
let completedExpiresAt = null;

function setStatus(msg, bad = false) {
  const el = $("status");
  el.textContent = msg;
  el.className = "status" + (bad ? " bad" : "");
}

function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return "";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let n = bytes;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined) return "";
  const total = Math.max(0, Math.round(sec));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function cleanClientFilename(value) {
  let text = (value || "download").trim();
  text = text.replace(/[<>:"/\\|?*\u0000-\u001f\u007f]+/g, " ");
  text = text.replace(/\s+/g, " ").replace(/^[ .]+|[ .]+$/g, "");
  return text.slice(0, 120) || "download";
}

function splitFilename(filename) {
  const match = (filename || "").match(/^(.*?)(\.[^.]+)$/);
  if (!match) return {stem: filename || "download", ext: ""};
  return {stem: match[1] || "download", ext: match[2] || ""};
}

function setDownloadButtonsDisabled(disabled) {
  document.querySelectorAll("#best, #audio, .format button").forEach((button) => {
    button.disabled = disabled;
  });
}

function showProgress() {
  $("progressPanel").classList.remove("hidden");
}

function hideSavePanel() {
  $("savePanel").classList.add("hidden");
}

function showRunningActions(show) {
  $("runningActions").classList.toggle("hidden", !show);
}

function renderProgress(data) {
  showProgress();
  $("progressPanel").dataset.state = data.status || "working";

  const labels = {
    preparing: "Preparing download…",
    downloading: "Downloading…",
    processing: "Processing / merging…",
    cancelling: "Cancelling…",
    cancelled: "Download cancelled",
    completed: "Ready to save ✓",
    error: "Download failed",
  };

  $("progressStage").textContent = labels[data.status] || "Working…";

  const hasPercent = typeof data.progress === "number" && Number.isFinite(data.progress);
  const percent = hasPercent ? Math.max(0, Math.min(100, data.progress)) : 0;
  const progressBar = $("progressBar");
  progressBar.style.width = `${percent}%`;
  progressBar.classList.toggle("indeterminate", !hasPercent && data.status === "downloading");

  $("progressPercent").textContent = hasPercent
    ? `${percent < 10 && percent !== 0 ? percent.toFixed(1) : Math.round(percent)}%`
    : "—";

  const track = document.querySelector(".progress-track");
  if (hasPercent) track.setAttribute("aria-valuenow", String(Math.round(percent)));
  else track.removeAttribute("aria-valuenow");

  const details = [];
  if (data.downloaded_bytes !== null && data.downloaded_bytes !== undefined) {
    if (data.total_bytes) {
      details.push(`${fmtSize(data.downloaded_bytes)} / ${fmtSize(data.total_bytes)}`);
    } else {
      details.push(`${fmtSize(data.downloaded_bytes)} downloaded`);
    }
  }
  if (data.speed) details.push(`${fmtSize(data.speed)}/s`);
  if (data.eta !== null && data.eta !== undefined && data.status === "downloading") {
    details.push(`ETA ${fmtDuration(data.eta)}`);
  }
  if (data.status === "processing") details.push("Finishing the media file on the server");
  if (data.status === "cancelling") details.push("Stopping safely and removing the partial file");
  if (data.status === "cancelled") details.push("Partial files were removed");
  if (data.status === "completed") details.push("Rename it if you want, then tap Save file");

  $("progressDetails").textContent = details.join(" · ");
}

function resetJobUI({hideProgress = false} = {}) {
  activeJobId = null;
  completedJobId = null;
  completedDownloadUrl = null;
  lastCompletedFilename = "";
  showRunningActions(false);
  hideSavePanel();
  $("cancelDownload").disabled = false;
  $("cancelDownload").textContent = "Cancel download";
  setDownloadButtonsDisabled(false);
  if (hideProgress) $("progressPanel").classList.add("hidden");
}

function prepareSavePanel(data) {
  completedExpiresAt = data.expires_at;
  completedJobId = data.job_id;
  completedDownloadUrl = data.download_url;
  lastCompletedFilename = data.filename || "download";

  const parts = splitFilename(lastCompletedFilename);
  const preferredStem = cleanClientFilename(data.requested_name || parts.stem);
  $("saveName").value = preferredStem;
  $("saveExt").textContent = parts.ext;
  $("savePanel").classList.remove("hidden");
  $("saveFile").disabled = false;
  updateExpiry();
  showRunningActions(false);
}

function buildFormatRow(format, index) {
  const row = document.createElement("div");
  row.className = "format";

  const info = document.createElement("div");
  info.className = "format-info";

  const title = document.createElement("strong");
  const titleBits = [];
  titleBits.push(format.resolution || `Format ${index + 1}`);
  if (format.ext) titleBits.push(format.ext.toUpperCase());
  if (format.format_note) titleBits.push(format.format_note);
  title.textContent = titleBits.join(" · ");

  const details = document.createElement("span");
  const detailBits = [format.type || "media"];
  if (format.fps) detailBits.push(`${format.fps} fps`);
  if (format.tbr) detailBits.push(`${Math.round(format.tbr)} kbps`);
  if (format.filesize) detailBits.push(fmtSize(format.filesize));
  details.textContent = detailBits.join(" · ");

  const codecs = document.createElement("span");
  const codecBits = [];
  if (format.vcodec && format.vcodec !== "none") codecBits.push(`Video: ${format.vcodec}`);
  if (format.acodec && format.acodec !== "none") codecBits.push(`Audio: ${format.acodec}`);
  codecs.textContent = codecBits.join(" · ");

  const tech = document.createElement("span");
  tech.className = "format-tech";
  const techBits = [`ID: ${format.format_id}`];
  if (format.protocol) techBits.push(`Source: ${format.protocol}`);
  tech.textContent = techBits.join(" · ");

  info.append(title, details);
  if (codecBits.length) info.append(codecs);
  info.append(tech);

  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary format-download";
  button.textContent = "Download";
  button.addEventListener("click", () => startDownload(format.format_id, false));

  row.append(info, button);
  return row;
}

async function analyze() {
  const url = $("url").value.trim();
  if (!url) return setStatus("Paste a URL first.", true);
  if (activeJobId) return setStatus("Cancel the current download before analyzing another link.", true);

  currentUrl = url;
  $("result").classList.add("hidden");
  resetJobUI({hideProgress: true});
  setStatus("Analyzing media…");
  $("analyze").disabled = true;

  try {
    const res = await fetch("/api/info", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Analysis failed.");

    $("title").textContent = data.title || "Untitled video";
    $("thumb").src = data.thumbnail || "";
    $("thumb").style.display = data.thumbnail ? "block" : "none";
    $("filename").value = cleanClientFilename(data.suggested_filename || data.title || "download");

    const pieces = [];
    if (data.uploader) pieces.push(data.uploader);
    if (data.duration) pieces.push(fmtDuration(data.duration));
    $("meta").textContent = pieces.join(" · ");

    const formats = $("formats");
    formats.replaceChildren();
    data.formats.forEach((format, index) => formats.appendChild(buildFormatRow(format, index)));

    $("result").classList.remove("hidden");
    setStatus(`Ready. ${data.formats.length} format${data.formats.length === 1 ? "" : "s"} found.`);
  } catch (error) {
    setStatus(error.message || "Analysis failed.", true);
  } finally {
    $("analyze").disabled = false;
  }
}

async function pollDownload(jobId) {
  while (activeJobId === jobId) {
    let res, data;
    try {
      res = await fetch(`/api/download/progress/${jobId}`, {cache: "no-store"});
      data = await res.json();
    } catch {
      setStatus("Connection interrupted. Reconnecting; the server job continues…");
      await sleep(2000);
      continue;
    }
    if (activeJobId !== jobId) return;
    if (!res.ok) {
      activeJobId = null;
      setDownloadButtonsDisabled(false);
      showRunningActions(false);
      setStatus(data.detail || "Could not read progress.", true);
      return;
    }
    renderProgress(data);
    if (["error", "cancelled", "completed"].includes(data.status)) {
      activeJobId = null;
      setDownloadButtonsDisabled(false);
      showRunningActions(false);
      if (data.status === "completed") {
        prepareSavePanel(data);
        setStatus("Ready on the server. Choose the final filename and save it.");
      } else if (data.status === "error") {
        setStatus(data.error || "Download failed. Retry from the quality controls.", true);
      } else {
        setStatus("Download cancelled. Partial files were removed.");
      }
      refreshHistory();
      return;
    }
    await sleep(800);
  }
}

async function startDownload(formatId = "", audioOnly = false) {
  if (!currentUrl) return setStatus("Analyze a link first.", true);
  if (activeJobId) return setStatus("A download is already running.", true);

  const requestedName = cleanClientFilename($("filename").value || "download");
  $("filename").value = requestedName;
  setStatus("Preparing download…");
  setDownloadButtonsDisabled(true);
  hideSavePanel();
  showRunningActions(true);
  $("cancelDownload").disabled = false;
  $("cancelDownload").textContent = "Cancel download";

  renderProgress({
    status: "preparing",
    progress: 0,
    downloaded_bytes: null,
    total_bytes: null,
    speed: null,
    eta: null,
  });

  try {
    const res = await fetch("/api/download/start", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        url: currentUrl,
        format_id: formatId,
        audio_only: audioOnly,
        filename: requestedName,
      }),
    });

    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Download failed.");

    activeJobId = data.job_id;
    rememberJob(data.job_id);
    refreshHistory();
    setStatus("Download started.");
    await pollDownload(data.job_id);
  } catch (error) {
    activeJobId = null;
    setDownloadButtonsDisabled(false);
    showRunningActions(false);
    setStatus(error.message || "Download failed.", true);
    renderProgress({
      status: "error",
      progress: 0,
      downloaded_bytes: null,
      total_bytes: null,
      speed: null,
      eta: null,
    });
  }
}

async function cancelCurrentDownload() {
  if (!activeJobId) return;
  const jobId = activeJobId;
  $("cancelDownload").disabled = true;
  $("cancelDownload").textContent = "Cancelling…";
  renderProgress({
    status: "cancelling",
    progress: Number.parseFloat($("progressBar").style.width) || 0,
    downloaded_bytes: null,
    total_bytes: null,
    speed: null,
    eta: null,
  });

  try {
    const res = await fetch(`/api/download/cancel/${jobId}`, {method: "POST"});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Could not cancel the download.");
    setStatus("Cancelling download…");
  } catch (error) {
    $("cancelDownload").disabled = false;
    $("cancelDownload").textContent = "Cancel download";
    setStatus(error.message || "Could not cancel the download.", true);
  }
}

function saveCompletedFile() {
  if (!completedJobId || !completedDownloadUrl) return;
  const saveName = cleanClientFilename($("saveName").value || splitFilename(lastCompletedFilename).stem);
  $("saveName").value = saveName;

  const separator = completedDownloadUrl.includes("?") ? "&" : "?";
  const href = `${completedDownloadUrl}${separator}name=${encodeURIComponent(saveName)}`;
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setStatus("Save request sent to your browser. The expiry time below applies to the server copy.");
}

function newDownload() {
  currentUrl = "";
  $("url").value = "";
  $("filename").value = "";
  $("result").classList.add("hidden");
  resetJobUI({hideProgress: true});
  setStatus("Ready for another link.");
  $("url").focus();
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (!text.trim()) return setStatus("Your clipboard is empty.", true);
    $("url").value = text.trim();
    setStatus("Link pasted. Tap Analyze.");
  } catch {
    setStatus("Safari did not allow clipboard access. Paste the link manually.", true);
  }
}

$("analyze").addEventListener("click", analyze);
$("pasteUrl").addEventListener("click", pasteFromClipboard);
$("url").addEventListener("keydown", (event) => {
  if (event.key === "Enter") analyze();
});
$("best").addEventListener("click", () => startDownload("", false));
$("audio").addEventListener("click", () => startDownload("", true));
$("cancelDownload").addEventListener("click", cancelCurrentDownload);
$("saveFile").addEventListener("click", saveCompletedFile);
$("newDownload").addEventListener("click", newDownload);


function rememberJob(id) {
  try { localStorage.setItem("ovd-current-job", id); } catch { /* Storage may be disabled. */ }
}

async function selectJob(job) {
  if (activeJobId === job.job_id) return;
  resetJobUI();
  rememberJob(job.job_id);
  renderProgress(job);
  if (job.status === "completed") prepareSavePanel(job);
  else if (!["error", "cancelled"].includes(job.status)) {
    activeJobId = job.job_id;
    setDownloadButtonsDisabled(true);
    showRunningActions(true);
    await pollDownload(job.job_id);
  } else setStatus(job.error || "Download cancelled.", job.status === "error");
}

async function removeServerFile(id) {
  try {
    const res = await fetch(`/api/jobs/${id}`, {method: "DELETE"});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not remove file.");
    if (completedJobId === id) resetJobUI({hideProgress: true});
    setStatus("Server copy removed. Files already saved to your device are unaffected.");
    refreshHistory();
  } catch (error) { setStatus(error.message, true); }
}

async function refreshHistory(restore = false) {
  try {
    const res = await fetch("/api/jobs", {cache: "no-store"});
    if (!res.ok) return;
    const data = await res.json();
    $("storageWarning").textContent = data.storage_warning || "";
    $("storageWarning").classList.toggle("hidden", !data.storage_warning);
    $("serverStorage").textContent = `${fmtSize(data.free_bytes)} free · Max file ${fmtSize(data.max_file_bytes)}`;
    const list = $("recentJobs");
    list.replaceChildren();
    const labels = {completed: "Ready", error: "Failed", cancelled: "Cancelled", preparing: "Preparing", downloading: "Downloading", processing: "Processing", cancelling: "Cancelling"};
    for (const job of data.jobs.slice(0, 30)) {
      const row = document.createElement("div");
      row.className = "history-row";
      const open = document.createElement("button");
      open.className = "ghost history-open";
      open.textContent = job.filename || job.requested_name || "Download";
      open.addEventListener("click", () => selectJob(job));
      const chip = document.createElement("span");
      chip.className = "job-chip";
      chip.dataset.state = job.status;
      chip.textContent = labels[job.status] || job.status;
      row.append(open, chip);
      if (["completed", "error", "cancelled"].includes(job.status)) {
        const remove = document.createElement("button");
        remove.className = "ghost compact";
        remove.textContent = job.status === "completed" ? "Remove server file" : "Dismiss";
        remove.addEventListener("click", () => removeServerFile(job.job_id));
        row.append(remove);
      }
      list.append(row);
    }
    if (!data.jobs.length) list.textContent = "Jobs appear here while this server is running.";
    if (restore && data.jobs.length) {
      let preferred;
      try { preferred = localStorage.getItem("ovd-current-job"); } catch { /* optional */ }
      const job = data.jobs.find(j => j.job_id === preferred)
        || data.jobs.find(j => !["completed", "error", "cancelled"].includes(j.status))
        || data.jobs[0];
      selectJob(job);
    }
  } catch { /* Progress handles reconnection; history retries on its timer. */ }
}

function updateExpiry() {
  if (!completedJobId || !completedExpiresAt) return;
  const seconds = Math.max(0, Math.floor(completedExpiresAt - Date.now() / 1000));
  $("fileExpiry").textContent = seconds ? `Server copy expires in ${fmtDuration(seconds)}. Save it to keep it on your device.` : "Server copy expired. Download the source again if needed.";
  $("saveFile").disabled = seconds === 0;
}

$("removeFile").addEventListener("click", () => { if (completedJobId) removeServerFile(completedJobId); });
setInterval(updateExpiry, 1000);
setInterval(refreshHistory, 8000);
refreshHistory(true);
