"use strict";

const $ = (selector) => document.querySelector(selector);
const caseButtons = [...document.querySelectorAll("[data-case]")];
const conversationList = $("#conversation-list");
const newConversation = $("#new-conversation");
const messages = $("#messages");
const scroll = $("#chat-scroll");
const input = $("#message");
const error = $("#error");
const allowedDocs = new Set(["claw.sjtu.edu.cn", "apps.hpc.sjtu.edu.cn", "docs.docker.com",
  "pip.pypa.io", "support.huaweicloud.com"]);
const caseTitles = {api: "API 超时", image: "镜像缺依赖"};
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let activeConversationId = null;
let activeCase = null;
let busy = true;
let playback = null;
let knowledgeMode = true;

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
  input.disabled = busy || Boolean(playback);
  for (const button of messages.querySelectorAll(".follow-ups button")) button.disabled = input.disabled;
  $("#send").disabled = input.disabled || !input.value.trim();
  $("#pause-playback").hidden = !playback;
  $("#pause-playback").textContent = playback?.paused ? "继续" : "暂停";
  $("#finish-playback").hidden = !playback;
}

function configureMode(enabled) {
  knowledgeMode = enabled;
  $("#public-panels").classList.toggle("knowledge", enabled);
  $("#demo-cases").hidden = enabled;
  $("#demo-suggestions").hidden = enabled;
  $("#progress-panel").hidden = enabled;
  $("#page-title").textContent = enabled ? "知识库问答" : "使用排障";
  $("#page-description").textContent = enabled ? "就致远一号的使用问题提问。" :
    "选择一个案例查看完整排查过程，或输入你的问题开始对话。";
  $("#chat-title").textContent = enabled ? "问答对话" : "排障对话";
  $("#playback-status").textContent = enabled ? "知识库问答" : "手动对话";
  $("#welcome-title").textContent = enabled ? "你想了解什么？" : "今天遇到了什么问题？";
  $("#welcome-description").textContent = enabled ? "输入有关致远一号的问题，开始一段对话。" :
    "发来错误码或脱敏日志。我会根据你的环境和每次重试的结果继续排查。";
  scroll.setAttribute("aria-label", enabled ? "问答对话记录" : "排障对话记录");
  input.setAttribute("aria-label", enabled ? "问题" : "问题或脱敏报错");
  input.placeholder = enabled ? "输入你的问题…" : "输入问题或粘贴脱敏报错…";
}

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

function renderConversationList({id, conversations}) {
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
    const item = node("li");
    const button = node("button", "conversation-item");
    button.type = "button";
    button.disabled = busy;
    button.title = conversation.title;
    if (conversation.id === activeConversationId) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => openConversation(conversation.id, button));
    button.append(node("span", "conversation-title", conversation.title));
    item.append(button);
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

async function request(path, data, signal) {
  const options = data === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data),
  };
  const response = await fetch(`./api/${path}`, {...options, signal});
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

function answerWithoutCitations(text, sources) {
  const ids = sources && new Set(sources.map((source) => String(source.id)));
  return text.split(/(```[\s\S]*?```|`[^`\n]*`)/g).map((part, index) =>
    index % 2 ? part : part.replace(/[ \t]*\[(\d+)\]/g, (marker, id) => !ids || ids.has(id) ? "" : marker)
  ).join("");
}

function sendSuggestedQuestion(question) {
  if (busy || playback) return;
  input.value = question;
  updateControls();
  $("#composer").requestSubmit();
}

function addMessage(role, text, {note, followUps, insufficient} = {}) {
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
    appendAnswer(bubble, text);
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

function addTurn(turn, isDemo = false) {
  addMessage("user", turn.user);
  if (turn.interpreter === "knowledge") {
    addMessage("assistant", turn.sources?.length ? answerWithoutCitations(turn.assistant, turn.sources) : turn.assistant, {
      note: turn.notice,
      followUps: turn.follow_ups,
      insufficient: turn.knowledge_status === "insufficient",
    });
  } else {
    const note = turn.notice ? `${turn.notice}，本轮使用本地规则。` :
      !isDemo && turn.interpreter !== "本地规则" ? `由 ${turn.interpreter} 理解` : "";
    addMessage("assistant", turn.assistant, {note});
  }
  $("#welcome").hidden = true;
  scroll.scrollTop = scroll.scrollHeight;
}

function renderState(state) {
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
  scroll.scrollTop = 0;
}

function restoreConversation(data) {
  activeCase = data.case;
  renderConversationList(data);
  clearConversation();
  for (const turn of data.turns) addTurn(turn);
  renderState(data.state);
  $("#playback-status").textContent = knowledgeMode ? "知识库问答" :
    activeCase ? `${caseTitles[activeCase]} · 会话记录` : "手动对话";
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
    addTurn(frame.turn, true);
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
    renderConversationList(data);
    clearConversation();
    renderState(null);
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

async function openConversation(conversationId = null, trigger = newConversation) {
  if (busy || (conversationId && conversationId === activeConversationId)) return;
  busy = true;
  updateControls();
  showError("");
  let opened = false;
  try {
    const path = conversationId ? `conversations/${encodeURIComponent(conversationId)}/select` : "conversations";
    const data = await request(path, {});
    cancelPlayback();
    input.value = "";
    restoreConversation(data);
    opened = true;
  } catch (problem) {
    showError(problem.message);
  } finally {
    busy = false;
    updateControls();
    if (opened && conversationId) conversationList.querySelector('[aria-current="true"]')?.focus({preventScroll: true});
    else if (opened) input.focus();
    else trigger.focus({preventScroll: true});
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const value = input.value.trim();
  if (!value || busy || playback) return;
  busy = true;
  updateControls();
  showError("");
  const heading = $("#playback-status");
  const previousStatus = heading.textContent;
  heading.textContent = knowledgeMode ? "正在检索资料…" : "正在排查…";
  $("#welcome").hidden = true;
  const pendingUser = addMessage("user", value);
  const pendingAssistant = addMessage("assistant", "");
  const preview = pendingAssistant.querySelector(".bubble");
  preview.textContent = "正在生成回答…";
  input.value = "";
  scroll.scrollTop = scroll.scrollHeight;
  try {
    const result = await requestStream(value, activeConversationId, (event) => {
      if (typeof event.user === "string") pendingUser.querySelector(".bubble").textContent = event.user;
      if (typeof event.preview === "string") {
        preview.textContent = knowledgeMode ? answerWithoutCitations(event.preview).replace(/\[\d*$/, "") : event.preview;
        scroll.scrollTop = scroll.scrollHeight;
      }
    });
    pendingUser.remove();
    pendingAssistant.remove();
    addTurn(result.turn);
    renderState(result.state);
    renderConversationList(result);
    input.value = "";
    heading.textContent = knowledgeMode ? "知识库问答" :
      activeCase ? `${caseTitles[activeCase]} · 手动续问` : "手动对话";
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
    const session = await request("session", undefined, controller.signal);
    configureMode(Boolean(session.knowledge_base_enabled));
    status.classList.toggle("live", knowledgeMode || Boolean(session.model));
    status.querySelector("b").textContent = knowledgeMode ? "知识库问答" :
      session.model ? `手动对话 · ${session.model}` : "本地规则模式";
    restoreConversation(session);
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
newConversation.addEventListener("click", () => openConversation());
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
