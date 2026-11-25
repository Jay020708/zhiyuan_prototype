"use strict";

const $ = (selector) => document.querySelector(selector);
const password = $("#password");
let knowledgeConfigured = false;
const documentFile = $("#document-file");
const replaceFile = $("#replace-file");
const error = $("#admin-error");
const statusLabels = {processing: "处理中", indexed: "已入库", failed: "入库失败", retired: "已停用"};
const dateFormat = new Intl.DateTimeFormat("zh-CN", {dateStyle: "medium", timeStyle: "short"});
const sizeFormat = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2});
let initialized = false;
let enabled = false;
let authenticated = false;
let uploadLimit = 0;
let busy = false;
let documents = [];
let pollTimer;
let replaceTarget = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return `${sizeFormat.format(bytes / (1024 * 1024))} MB`;
  if (bytes >= 1024) return `${sizeFormat.format(bytes / 1024)} KB`;
  return `${bytes} 字节`;
}

async function request(path, options) {
  const response = await fetch(`./api/admin/${path}`, options).catch(() => {
    throw new Error("管理服务暂时无法连接，请重试。");
  });
  if (response.status === 204) return;
  const result = await response.json().catch(() => null);
  if (!response.ok || !result) {
    const problem = new Error(typeof result?.detail === "string" ? result.detail : "管理服务暂时无法响应，请重试。");
    problem.status = response.status;
    throw problem;
  }
  return result;
}

function renderControls() {
  $("#login-panel").hidden = !enabled || authenticated;
  $("#services-panel").hidden = !authenticated;
  $("#upload-panel").hidden = !authenticated || !knowledgeConfigured;
  $("#documents-panel").hidden = !authenticated || !knowledgeConfigured;
  $("#admin-logout").hidden = !authenticated;
  $("#admin-retry").hidden = initialized || busy;
  const sessionStatus = $("#admin-session-status");
  sessionStatus.hidden = (initialized && enabled) || (!initialized && !busy);
  sessionStatus.textContent = initialized ? "管理服务尚未配置，请先设置管理员密码。" :
    "正在连接管理服务…";
  for (const control of [password, documentFile, replaceFile, $("#login-submit"), $("#upload-submit"),
    $("#refresh-documents"), $("#admin-logout")]) {
    control.disabled = busy;
  }
  for (const button of document.querySelectorAll(".document-action")) {
    button.disabled = busy;
  }
}

function versionNote(entry) {
  if (entry.superseded_by) {
    return entry.superseded_by_version ? `已被 v${entry.superseded_by_version} 替代` : "已被新版本替代";
  }
  return entry.supersedes_version ? `替代 v${entry.supersedes_version}` : "";
}

function actionButton(label, handler) {
  const button = node("button", "quiet document-action", label);
  button.type = "button";
  button.disabled = busy;
  button.addEventListener("click", handler);
  return button;
}

function renderDocuments() {
  const rows = documents.map((entry) => {
    const row = node("tr");
    const name = node("th", "document-name");
    name.scope = "row";
    name.append(node("span", "", entry.filename));
    const note = versionNote(entry);
    if (note) name.append(node("span", "document-version-note", note));
    const status = node("td");
    const label = statusLabels[entry.status] || "未知";
    status.append(node("span", `pill document-${entry.status}`, label));
    const count = entry.status === "indexed" ? String(entry.chunk_count) : "—";
    const timestamp = new Date(entry.created_at);
    const date = Number.isNaN(timestamp.getTime()) ? "—" : dateFormat.format(timestamp);
    const failure = entry.error || (entry.status === "failed" ? "未返回失败原因" : "—");
    const actions = node("td", "document-actions");
    if (!entry.superseded_by) {
      if (entry.status === "failed") {
        actions.append(actionButton("重新索引", () => run(() => retryDocument(entry))));
      }
      if (entry.status === "indexed" || entry.status === "failed") {
        actions.append(actionButton("替换为新版本", () => requestReplacement(entry)));
      }
    }
    if (!actions.childElementCount) actions.textContent = "—";
    row.append(name, node("td", "document-version", `v${entry.version || 1}`), status, node("td", "", count),
      node("td", "document-date", date), node("td", "document-error", failure), actions);
    return row;
  });
  $("#document-rows").replaceChildren(...rows);
  $("#empty-documents").hidden = rows.length > 0;
  $("#documents-table").hidden = rows.length === 0;
}

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

function endSession() {
  authenticated = false;
  replaceTarget = null;
  replaceFile.value = "";
  $("#service-status-list").replaceChildren();
  $("#service-check-status").textContent = "";
  documents = [];
  $("#document-upload").reset();
  renderDocuments();
}

async function run(task) {
  if (busy) return;
  clearTimeout(pollTimer);
  busy = true;
  renderControls();
  showError("");
  try {
    await task();
  } catch (problem) {
    if (authenticated && (problem.status === 401 || problem.status === 403)) endSession();
    showError(problem.message);
  } finally {
    busy = false;
    renderControls();
    if (authenticated && documents.some((entry) => entry.status === "processing")) {
      pollTimer = setTimeout(() => run(refreshDocuments), 2000);
    }
  }
}

async function refreshDocuments() {
  const result = await request("documents");
  documents = result.documents;
  renderDocuments();
}

async function checkServices() {
  const button = $("#check-services");
  if (button.disabled) return;
  const status = $("#service-check-status");
  button.disabled = true;
  status.textContent = "正在检查向量服务、生成模型和索引…";
  try {
    const results = await request("services");
    if (!authenticated) return;
    const cards = Object.entries({embedding: "向量服务", model: "生成模型", index: "知识库索引"})
      .map(([key, title]) => {
        const result = results[key];
        const card = node("div", "service-status-card");
        const name = node("dt", "", title);
        const labels = {ready: "正常", empty: "暂无文档", unconfigured: "未配置", error: "异常"};
        const detail = node("dd");
        detail.append(node("span", `pill service-${result.status}`, labels[result.status]),
          node("p", "", result.detail));
        if (result.hint) detail.append(node("p", "empty", result.hint));
        card.append(name, detail);
        return card;
      });
    $("#service-status-list").replaceChildren(...cards);
    status.textContent = `最近检查 ${dateFormat.format(new Date(results.checked_at))}`;
  } catch (problem) {
    if (authenticated) status.textContent = problem.message;
  } finally {
    button.disabled = false;
  }
}

async function loadAdminPanels() {
  if (!authenticated) return;
  void checkServices();
  if (knowledgeConfigured) await refreshDocuments();
}

async function initialize() {
  const session = await request("session");
  initialized = true;
  enabled = session.enabled;
  knowledgeConfigured = session.knowledge_configured;
  authenticated = enabled && session.authenticated;
  uploadLimit = session.upload_limit_bytes;
  if (uploadLimit > 0) {
    $("#upload-formats").textContent = `支持 TXT、Markdown、PDF，单个文件不超过 ${formatSize(uploadLimit)}。`;
  }
  await loadAdminPanels();
}

async function login() {
  const value = password.value;
  password.value = "";
  const session = await request("login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({password: value}),
  });
  authenticated = session.authenticated;
  await loadAdminPanels();
}

async function logout() {
  await request("logout", {method: "POST"});
  endSession();
}

async function upload() {
  const file = documentFile.files[0];
  if (!file) return;
  const submit = $("#upload-submit");
  submit.textContent = "正在上传…";
  try {
    const result = await sendDocument(file);
    documents = [result.document, ...documents.filter((entry) => entry.id !== result.document.id)];
    $("#document-upload").reset();
    renderDocuments();
  } finally {
    submit.textContent = "上传并入库";
  }
}

function checkFile(file) {
  if (!/\.(txt|md|pdf)$/i.test(file.name)) throw new Error("请选择 TXT、Markdown 或 PDF 文档。");
  if (uploadLimit > 0 && file.size > uploadLimit) {
    throw new Error(`文件超过 ${formatSize(uploadLimit)} 上传限制。`);
  }
}

async function sendDocument(file, supersedes) {
  checkFile(file);
  const headers = {
    "Content-Type": file.type || "application/octet-stream",
    "X-Document-Filename": encodeURIComponent(file.name),
  };
  if (supersedes) headers["X-Document-Supersedes"] = supersedes;
  return request("documents", {method: "POST", headers, body: file});
}

function requestReplacement(entry) {
  if (busy) return;
  const confirmed = window.confirm(
    `确认用新文件替换「${entry.filename}」？\n\n` +
    `新版本入库成功后会停用 v${entry.version || 1}；如果入库失败，旧版继续用于检索。`,
  );
  if (!confirmed) return;
  replaceTarget = entry;
  replaceFile.value = "";
  replaceFile.click();
}

async function replaceVersion(entry, file) {
  const result = await sendDocument(file, entry.id);
  documents = [result.document, ...documents.filter((document) => document.id !== result.document.id)];
  renderDocuments();
}

async function retryDocument(entry) {
  const result = await request(`documents/${entry.id}/retry`, {method: "POST"});
  documents = documents.map((document) => (
    document.id === result.document.id ? result.document : document
  ));
  renderDocuments();
}

$("#admin-login").addEventListener("submit", (event) => {
  event.preventDefault();
  return run(login);
});
$("#document-upload").addEventListener("submit", (event) => {
  event.preventDefault();
  return run(upload);
});
replaceFile.addEventListener("change", () => {
  const file = replaceFile.files[0];
  const entry = replaceTarget;
  replaceFile.value = "";
  replaceTarget = null;
  if (file && entry) return run(() => replaceVersion(entry, file));
});
$("#admin-logout").addEventListener("click", () => run(logout));
$("#admin-retry").addEventListener("click", () => run(initialize));
$("#refresh-documents").addEventListener("click", () => run(refreshDocuments));
$("#check-services").addEventListener("click", checkServices);
run(initialize);
