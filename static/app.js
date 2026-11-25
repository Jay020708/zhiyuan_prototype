"use strict";

const $ = (selector) => document.querySelector(selector);
const caseButtons = [...document.querySelectorAll("[data-case]")];
const conversationList = $("#conversation-list");
const newConversation = $("#new-conversation");
const conversationDialog = $("#conversation-dialog");
const renameForm = $("#rename-conversation");
const deleteForm = $("#delete-conversation");
const conversationName = $("#conversation-name");
const messages = $("#messages");
const scroll = $("#chat-scroll");
const input = $("#message");
const error = $("#error");
const allowedDocs = new Set(["claw.sjtu.edu.cn", "apps.hpc.sjtu.edu.cn", "docs.docker.com",
  "pip.pypa.io", "support.huaweicloud.com"]);
const caseTitles = {api: "API 超时", image: "镜像缺依赖"};
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let activeCase = null;
let activeConversationId = null;
let editingConversation = null;
let busy = true;
let playback = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function updateControls() {
  for (const button of caseButtons) {
    button.disabled = busy;
    button.setAttribute("aria-pressed", String(button.dataset.case === activeCase));
  }
  newConversation.disabled = busy;
  for (const button of conversationList.querySelectorAll("button")) button.disabled = busy;
  $("#exit-demo").hidden = !activeCase;
  $("#exit-demo").disabled = busy;
  input.disabled = busy || Boolean(playback);
  for (const button of messages.querySelectorAll(".follow-ups button")) button.disabled = input.disabled;
  $("#send").disabled = input.disabled || !input.value.trim();
  $("#pause-playback").hidden = !playback;
  $("#pause-playback").textContent = playback?.paused ? "继续" : "暂停";
  $("#finish-playback").hidden = !playback;
}

function displaySession(session) {
  activeCase = null;
  const modelStatus = $("#model-status");
  modelStatus.classList.toggle("live", Boolean(session.model));
  modelStatus.querySelector("b").textContent = session.model ?? "规则排障";
  clearConversation();
  for (const turn of session.turns) addTurn(turn);
  renderState(session.state);
  describeTurn(session.turns.at(-1), session.state);
  renderConversationList(session);
}

function describeTurn(turn, state) {
  $("#playback-status").textContent = turn?.kind === "knowledge" ? "知识库问答" :
    state ? `${caseTitles[state.scenario] ?? "排障"} · ${state.status === "resolved" ? "已完成" : "排障"}` :
    "可提问，也可输入报错";
}

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

function renderConversationList({id = null, conversations = []}) {
  activeConversationId = id;
  const empty = $("#conversation-empty");
  empty.textContent = "还没有历史对话。";
  empty.hidden = conversations.length > 0;
  const scrollTop = conversationList.scrollTop;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const lastWeek = new Date(today);
  lastWeek.setDate(today.getDate() - 6);
  const groups = new Map([["今天", []], ["昨天", []], ["最近七天", []], ["更早", []]]);
  for (const conversation of conversations) {
    const date = new Date(conversation.updated_at);
    const label = date >= today ? "今天" : date >= yesterday ? "昨天" : date >= lastWeek ? "最近七天" : "更早";
    const item = node("li", "conversation-row");
    const button = node("button", "conversation-item");
    button.type = "button";
    button.disabled = busy;
    button.title = conversation.title;
    if (conversation.id === activeConversationId) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => openConversation(conversation.id));
    button.append(node("span", "conversation-title", conversation.title));
    const menu = node("button", "conversation-menu", "⋯");
    menu.type = "button";
    menu.disabled = busy;
    menu.dataset.conversationId = conversation.id;
    menu.title = "重命名或删除";
    menu.setAttribute("aria-label", `管理对话 ${conversation.title}`);
    menu.setAttribute("aria-haspopup", "dialog");
    menu.addEventListener("click", () => manageConversation(conversation));
    item.append(button, menu);
    groups.get(label).push(item);
  }
  const sections = [];
  for (const [label, items] of groups) {
    if (!items.length) continue;
    const section = node("li", "conversation-group");
    const list = node("ul");
    list.setAttribute("aria-label", label);
    list.append(...items);
    section.append(node("h3", "", label), list);
    sections.push(section);
  }
  conversationList.replaceChildren(...sections);
  conversationList.scrollTop = scrollTop;
}

async function request(path, data, method = data === undefined ? "GET" : "POST", signal) {
  const options = {method, signal};
  if (data !== undefined) {
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(data);
  }
  const response = await fetch(`./api/${path}`, options);
  const result = await response.json().catch(() => null);
  if (!response.ok || !result) {
    throw new Error(typeof result?.detail === "string" ? result.detail : "服务暂时无法响应，请重试。");
  }
  return result;
}

function appendText(parent, content) {
  const span = node("span", "text-fragment");
  const tokens = content.split(/(`[^`\n]+`|https:\/\/[^\s）。，、·]+)/g);
  for (const token of tokens) {
    if (token.startsWith("`") && token.endsWith("`")) {
      span.append(node("code", "", token.slice(1, -1)));
    } else if (token.startsWith("https://")) {
      let url;
      try { url = new URL(token); } catch { /* Treat malformed URLs as text. */ }
      if (url && allowedDocs.has(url.hostname)) {
        const link = node("a", "", "查看文档");
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        span.append(link);
      } else {
        span.append(token);
      }
    } else {
      span.append(token);
    }
  }
  parent.append(span);
}

function appendAnswer(parent, content) {
  const chunks = content.split(/```([\w-]*)\n([\s\S]*?)```/g);
  for (let index = 0; index < chunks.length; index += 3) {
    appendText(parent, chunks[index]);
    if (index + 2 < chunks.length) {
      const codeText = chunks[index + 2].trimEnd();
      const box = node("div", "code");
      const bar = node("div", "code-bar");
      const copy = node("button", "copy-code", "复制");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(codeText);
          copy.textContent = "已复制";
        } catch {
          copy.textContent = "请手动复制";
        }
      });
      bar.append(node("span", "", chunks[index + 1] === "dockerfile" ? "Dockerfile" : "代码"), copy);
      const pre = node("pre");
      pre.append(node("code", "", codeText));
      box.append(bar, pre);
      parent.append(box);
    }
  }
}

function hideLegacyCitations(text, sources) {
  const ids = new Set(sources.map((source) => String(source.id)));
  if (!ids.size) return text;
  return text.replace(/```[\s\S]*?```|`[^`\n]*`|[ \t]*(?<![A-Za-z0-9_])\[(\d+)\](?![ \t]*\()/g,
    (token, id) => ids.has(id) ? "" : token);
}

function sendSuggestedQuestion(question) {
  if (busy || playback) return;
  input.value = question;
  updateControls();
  $("#composer").requestSubmit();
}

function addMessage(role, text, {note, sources, followUps, insufficient} = {}) {
  const row = node("div", `message ${role}`);
  const body = node("div", "message-content");
  const bubble = node("div", "bubble");
  row.setAttribute("aria-label", role === "user" ? "用户消息" : "助手回复");
  if (role === "user") {
    bubble.textContent = text;
  } else {
    const avatar = node("span", "assistant-avatar", "远");
    avatar.setAttribute("aria-hidden", "true");
    row.append(avatar);
    if (sources) bubble.textContent = hideLegacyCitations(text, sources);
    else appendAnswer(bubble, text);

  }
  if (insufficient) body.append(node("p", "answer-status", "资料不足"));
  body.append(bubble);
  if (note) body.append(node("p", "message-note", note));
  if (followUps?.length) {
    const section = node("section", "follow-ups");
    section.append(node("h4", "", "你可能还想问"));
    const choices = node("div", "suggestions");
    for (const question of followUps) {
      const button = node("button", "suggestion", question);
      button.type = "button";
      button.addEventListener("click", () => sendSuggestedQuestion(question));
      choices.append(button);
    }
    section.append(choices);
    body.append(section);
  }
  row.append(body);
  messages.append(row);
  return row;
}

function addTurn(turn) {
  addMessage("user", turn.user);
  if (turn.interpreter === "knowledge") {
    addMessage("assistant", turn.assistant, {
      note: turn.notice,
      sources: turn.knowledge_status === "excerpts" ? [] : turn.sources,
      followUps: turn.follow_ups,
      insufficient: turn.knowledge_status === "insufficient",
    });
  } else {
    const reply = turn.assistant.replace(/(?:\r?\n[ \t]*依据[：:][^\r\n]*)+\s*$/, "");
    addMessage("assistant", reply);
  }
  $("#welcome").hidden = true;
  scroll.scrollTop = scroll.scrollHeight;
}

function renderState(state) {
  $("#progress-panel").hidden = !state;
  $("#public-panels").classList.toggle("knowledge", !state);
  const status = state?.status;
  const pill = $("#progress-label");
  pill.className = `pill ${status || ""}`;
  pill.textContent = {active: "排查中", resolved: "已完成", support: "需要人工协助"}[status] || "待开始";
  $("#pending").textContent = state?.pending || (status === "resolved" ? "所有验证已完成。" :
    status === "support" ? "可将排查摘要交给技术支持。" : state ? "等待问题或重试结果。" : "开始对话后显示下一步。");

  const confirmed = Object.entries(state?.facts || {}).filter(([, value]) => value !== null);
  $("#facts-block").hidden = confirmed.length === 0;
  $("#facts").replaceChildren(...confirmed.map(([label, value]) => {
    const pair = node("div", "fact");
    pair.append(node("dt", "", label), node("dd", "", value));
    return pair;
  }));

  const history = state?.history || [];
  $("#history-total").textContent = history.length;
  $("#empty-history").hidden = history.length > 0;
  $("#history").replaceChildren(...history.map((entry) => {
    const item = node("li");
    const body = node("div");
    const detail = /^(.*?)（(.*)）$/.exec(entry);
    if (detail && /超时|401|失败|缺失|无法/.test(detail[2])) item.className = "failed";
    body.append(node("span", "", detail ? detail[1] : entry));
    if (detail) body.append(node("small", "", detail[2]));
    item.append(body);
    return item;
  }));
}

function clearConversation() {
  messages.replaceChildren();
  $("#welcome").hidden = false;
  renderState(null);
}

function cancelPlayback() {
  if (playback) clearTimeout(playback.timer);
  playback = null;
  updateControls();
}

function playNext(showAll = false) {
  const current = playback;
  if (!current) return;
  clearTimeout(current.timer);
  do {
    const frame = current.frames[current.index++];
    addTurn(frame.turn);
    renderState(frame.state);
  } while (showAll && current.index < current.frames.length);

  const heading = $("#playback-status");
  if (current.index === current.frames.length) {
    heading.textContent = `${caseTitles[activeCase]} · 示例反馈演示完毕`;
    cancelPlayback();
  } else {
    heading.textContent = `${caseTitles[activeCase]} · 示例反馈 ${current.index}/${current.frames.length}`;
    if (!current.paused) current.timer = setTimeout(playNext, 1100);
  }
}

function togglePlayback() {
  if (!playback) return;
  playback.paused = !playback.paused;
  if (playback.paused) {
    clearTimeout(playback.timer);
  } else {
    playNext();
  }
  updateControls();
}

async function loadDemo(caseId) {
  busy = true;
  cancelPlayback();
  showError("");
  const previousStatus = $("#playback-status").textContent;
  $("#playback-status").textContent = "正在运行示例工作流…";
  try {
    const data = await request(`demo/${caseId}`, {});
    activeCase = data.case;
    clearConversation();
    playback = {frames: data.frames, index: 0, timer: null, paused: false};
    playNext(reducedMotion.matches);
  } catch (problem) {
    $("#playback-status").textContent = previousStatus;
    showError(problem.message);
  } finally {
    busy = false;
    updateControls();
  }
}

async function exitDemo() {
  cancelPlayback();
  const path = activeConversationId ? `conversations/${encodeURIComponent(activeConversationId)}` : "session";
  displaySession(await request(path));
}

async function openConversation(conversationId = null) {
  if (busy) return;
  busy = true;
  cancelPlayback();
  showError("");
  try {
    const path = conversationId ? `conversations/${encodeURIComponent(conversationId)}/select` : "conversations";
    const session = await request(path, {});
    input.value = "";
    displaySession(session);
  } catch (problem) {
    showError(problem.message);
  } finally {
    busy = false;
    updateControls();
    input.focus();
  }
}

function showConversationError(message) {
  const dialogError = $("#conversation-dialog-error");
  dialogError.textContent = message;
  dialogError.hidden = !message;
}

function showDeleteConfirmation(confirming) {
  renameForm.hidden = confirming;
  deleteForm.hidden = !confirming;
  $("#conversation-dialog-title").textContent = confirming ? "删除对话" : "管理对话";
  showConversationError("");
  if (confirming) {
    $("#conversation-delete-name").textContent = editingConversation.title;
    $("#cancel-delete-conversation").focus();
  } else {
    conversationName.focus();
    conversationName.select();
  }
}

function manageConversation(conversation) {
  editingConversation = conversation;
  conversationName.value = conversation.title;
  conversationDialog.showModal();
  showDeleteConfirmation(false);
}

async function saveConversationChange(event) {
  event.preventDefault();
  if (busy) return;
  const deleting = event.currentTarget === deleteForm;
  const title = conversationName.value.trim();
  if (!deleting && !title) {
    showConversationError("请输入对话名称。");
    conversationName.focus();
    return;
  }
  const {id} = editingConversation;
  const controls = conversationDialog.querySelectorAll("input, button");
  busy = true;
  updateControls();
  for (const control of controls) control.disabled = true;
  showConversationError("");
  try {
    const path = `conversations/${encodeURIComponent(id)}`;
    const data = deleting ? await request(path, undefined, "DELETE") : await request(path, {title}, "PATCH");
    if (deleting && id === activeConversationId) {
      cancelPlayback();
      input.value = "";
      displaySession(data);
    } else {
      renderConversationList({id: activeConversationId, conversations: data.conversations});
    }
    conversationDialog.close();
  } catch (problem) {
    showConversationError(problem.message);
  } finally {
    busy = false;
    updateControls();
    for (const control of controls) control.disabled = false;
    if (conversationDialog.open) {
      (deleting ? deleteForm.querySelector("[type=submit]") : conversationName).focus();
    } else {
      const menu = [...conversationList.querySelectorAll(".conversation-menu")]
        .find((button) => button.dataset.conversationId === (deleting ? activeConversationId : id));
      (menu ?? newConversation).focus({preventScroll: true});
    }
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const value = input.value.trim();
  if (!value || busy || playback) return;
  busy = true;
  updateControls();
  showError("");
  if (activeCase) {
    try {
      await exitDemo();
    } catch (problem) {
      showError(problem.message);
      busy = false;
      updateControls();
      return;
    }
  }
  const heading = $("#playback-status");
  const previousStatus = heading.textContent;
  heading.textContent = "正在处理…";
  $("#welcome").hidden = true;
  const pendingUser = addMessage("user", value);
  const pendingAssistant = addMessage("assistant", "", {sources: []});
  const preview = pendingAssistant.querySelector(".bubble");
  preview.textContent = "正在生成回答…";
  input.value = "";
  scroll.scrollTop = scroll.scrollHeight;
  try {
    const result = await requestStream(value, activeConversationId, (event) => {
      if (typeof event.user === "string") pendingUser.querySelector(".bubble").textContent = event.user;
      if (typeof event.preview === "string") {
        preview.textContent = event.preview;
        scroll.scrollTop = scroll.scrollHeight;
      }
    });
    pendingUser.remove();
    pendingAssistant.remove();
    addTurn(result.turn);
    renderState(result.state);
    renderConversationList(result);
    input.value = "";
    describeTurn(result.turn, result.state);
  } catch (problem) {
    pendingUser.remove();
    pendingAssistant.remove();
    input.value = value;
    $("#welcome").hidden = messages.children.length > 0;
    heading.textContent = previousStatus;
    showError(problem.message);
  } finally {
    busy = false;
    updateControls();
    input.focus();
  }
}

async function requestStream(message, conversationId, onEvent) {
  const response = await fetch("./api/turn", {
    method: "POST",
    headers: {"Content-Type": "application/json", "Accept": "application/x-ndjson"},
    body: JSON.stringify({message, conversation_id: conversationId}),
  });
  if (!response.ok || !response.headers.get("content-type")?.includes("application/x-ndjson")) {
    const data = await response.json();
    if (response.ok && data.turn) return data;
    throw new Error(typeof data.detail === "string" ? data.detail : "发送失败，请重试。");
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let result;
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += value;
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line) continue;
        const event = JSON.parse(line);
        if (event.result) result = event.result;
        else onEvent(event);
      }
    }
  } finally {
    await reader.cancel();
  }
  if (!result) throw new Error("回答连接已中断，请重试。");
  return result;
}

async function initialize() {
  updateControls();
  const status = $("#model-status");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    displaySession(await request("session", undefined, "GET", controller.signal));
  } catch (problem) {
    status.querySelector("b").textContent = "服务未连接";
    $("#conversation-empty").textContent = "暂时无法加载历史对话。";
    showError(controller.signal.aborted ? "加载对话超时，请刷新页面重试。" : problem.message);
  } finally {
    clearTimeout(timeout);
    busy = false;
    updateControls();
  }
}

for (const button of caseButtons) button.addEventListener("click", () => loadDemo(button.dataset.case));
for (const button of document.querySelectorAll("[data-question]")) {
  button.addEventListener("click", () => sendSuggestedQuestion(button.dataset.question));
}
$("#finish-playback").addEventListener("click", () => playNext(true));
$("#pause-playback").addEventListener("click", togglePlayback);
$("#exit-demo").addEventListener("click", async () => {
  busy = true;
  updateControls();
  showError("");
  try {
    await exitDemo();
  } catch (problem) {
    showError(problem.message);
  } finally {
    busy = false;
    updateControls();
    input.focus();
  }
});
newConversation.addEventListener("click", () => openConversation());
renameForm.addEventListener("submit", saveConversationChange);
deleteForm.addEventListener("submit", saveConversationChange);
$("#start-delete-conversation").addEventListener("click", () => showDeleteConfirmation(true));
$("#cancel-delete-conversation").addEventListener("click", () => showDeleteConfirmation(false));
$("#close-conversation-dialog").addEventListener("click", () => conversationDialog.close());
conversationDialog.addEventListener("cancel", (event) => {
  if (busy) event.preventDefault();
});
$("#toggle-history").addEventListener("click", (event) => {
  const history = $("#conversation-history");
  history.hidden = !history.hidden;
  event.currentTarget.setAttribute("aria-expanded", String(!history.hidden));
  event.currentTarget.textContent = history.hidden ? "展开历史" : "收起历史";
});
$("#composer").addEventListener("submit", sendMessage);
input.addEventListener("input", updateControls);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});
initialize();
