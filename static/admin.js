"use strict";

const $ = (selector) => document.querySelector(selector);
const password = $("#password");
const documentFile = $("#document-file");
const error = $("#admin-error");
const statusLabels = {processing: "处理中", indexed: "已入库", failed: "入库失败"};
const dateFormat = new Intl.DateTimeFormat("zh-CN", {dateStyle: "medium", timeStyle: "short"});
const sizeFormat = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2});
let initialized = false;
let enabled = false;
let authenticated = false;
let uploadLimit = 0;
let busy = false;
let documents = [];
let pollTimer;

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
  $("#upload-panel").hidden = !authenticated;
  $("#documents-panel").hidden = !authenticated;
  $("#admin-logout").hidden = !authenticated;
  $("#admin-retry").hidden = initialized || busy;
  const sessionStatus = $("#admin-session-status");
  sessionStatus.hidden = (initialized && enabled) || (!initialized && !busy);
  sessionStatus.textContent = initialized ? "管理服务尚未配置，请先设置管理员密码和向量服务地址。" :
    "正在连接管理服务…";
  for (const control of [password, documentFile, $("#login-submit"), $("#upload-submit"),
    $("#refresh-documents"), $("#admin-logout")]) {
    control.disabled = busy;
  }
}

function renderDocuments() {
  const rows = documents.map((entry) => {
    const row = node("tr");
    const name = node("th", "document-name", entry.filename);
    name.scope = "row";
    const status = node("td");
    const label = statusLabels[entry.status] || "未知";
    status.append(node("span", `pill document-${entry.status}`, label));
    const count = entry.status === "indexed" ? String(entry.chunk_count) : "—";
    const timestamp = new Date(entry.created_at);
    const date = Number.isNaN(timestamp.getTime()) ? "—" : dateFormat.format(timestamp);
    const failure = entry.error || (entry.status === "failed" ? "未返回失败原因" : "—");
    row.append(name, status, node("td", "", count), node("td", "document-date", date),
      node("td", "document-error", failure));
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

async function initialize() {
  const session = await request("session");
  initialized = true;
  enabled = session.enabled;
  authenticated = enabled && session.authenticated;
  uploadLimit = session.upload_limit_bytes;
  if (uploadLimit > 0) {
    $("#upload-formats").textContent = `支持 TXT、Markdown、PDF，单个文件不超过 ${formatSize(uploadLimit)}。`;
  }
  if (authenticated) await refreshDocuments();
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
  if (authenticated) await refreshDocuments();
}

async function logout() {
  await request("logout", {method: "POST"});
  endSession();
}

async function upload() {
  const file = documentFile.files[0];
  if (!file) return;
  if (!/\.(txt|md|pdf)$/i.test(file.name)) throw new Error("请选择 TXT、Markdown 或 PDF 文档。");
  if (uploadLimit > 0 && file.size > uploadLimit) {
    throw new Error(`文件超过 ${formatSize(uploadLimit)} 上传限制。`);
  }
  const submit = $("#upload-submit");
  submit.textContent = "正在上传…";
  try {
    const result = await request("documents", {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Document-Filename": encodeURIComponent(file.name),
      },
      body: file,
    });
    documents = [result.document, ...documents.filter((entry) => entry.id !== result.document.id)];
    $("#document-upload").reset();
    renderDocuments();
  } finally {
    submit.textContent = "上传并入库";
  }
}

$("#admin-login").addEventListener("submit", (event) => {
  event.preventDefault();
  return run(login);
});
$("#document-upload").addEventListener("submit", (event) => {
  event.preventDefault();
  return run(upload);
});
$("#admin-logout").addEventListener("click", () => run(logout));
$("#admin-retry").addEventListener("click", () => run(initialize));
$("#refresh-documents").addEventListener("click", () => run(refreshDocuments));
run(initialize);
