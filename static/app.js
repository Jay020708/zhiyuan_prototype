"use strict";

const $ = (selector) => document.querySelector(selector);
const messages = $("#messages");
const scroll = $("#chat-scroll");
const input = $("#message");
const error = $("#error");
const suggestions = $("#suggestions");
const replyStatus = $("#reply-status");
const allowedDocs = new Set(["claw.sjtu.edu.cn", "apps.hpc.sjtu.edu.cn", "docs.docker.com",
  "pip.pypa.io", "support.huaweicloud.com"]);
let busy = true;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function updateControls() {
  for (const button of suggestions.children) button.disabled = busy;
  $("#reset").disabled = busy;
  input.disabled = busy;
  $("#send").disabled = busy || !input.value.trim();
}

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

async function request(path, data) {
  const options = data === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data),
  };
  const response = await fetch(path, options);
  if (!response.ok) {
    const message = response.status === 429 ? "请求过多，请稍后重试，或开启新会话。" :
      response.status === 409 ? "当前会话正在处理请求，请稍后重试。" :
      "问答服务暂时无法响应，请稍后重试。";
    throw new Error(message);
  }
  return response.json();
}

function appendText(parent, content) {
  const span = node("span", "text-fragment");
  const tokens = content.split(/(`[^`\n]+`|https:\/\/[^\s）。，、·]+)/g);
  for (const token of tokens) {
    if (token.startsWith("`") && token.endsWith("`")) {
      span.append(node("code", "", token.slice(1, -1)));
    } else if (token.startsWith("https://")) {
      let url;
      try { url = new URL(token); } catch { /* Keep malformed URLs as text. */ }
      if (url && allowedDocs.has(url.hostname)) {
        const link = node("a", "", "查看官方文档");
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
    if (index + 2 >= chunks.length) continue;
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
    bar.append(node("span", "", chunks[index + 1] || "代码"), copy);
    const pre = node("pre");
    pre.append(node("code", "", codeText));
    box.append(bar, pre);
    parent.append(box);
  }
}

function appendSources(parent, sources) {
  const section = node("details", "answer-sources");
  const list = node("ul", "source-list");
  for (const source of sources) {
    const item = node("li", "answer-source");
    item.append(node("strong", "", "[" + source.id + "] " + source.filename));
    if (source.location) item.append(node("small", "source-location", source.location));
    list.append(item);
  }
  section.append(node("summary", "", "参考资料"), list);
  parent.append(section);
}

function renderUser(text) {
  const user = node("div", "message user");
  user.setAttribute("aria-label", "用户消息");
  user.append(node("div", "bubble", text));
  $("#welcome").hidden = true;
  messages.append(user);
  return user;
}

function renderAnswer(turn) {
  const assistant = node("div", "message assistant");
  assistant.setAttribute("aria-label", "助手回复");
  const avatar = node("span", "assistant-avatar", "远");
  avatar.setAttribute("aria-hidden", "true");
  const body = node("div", "message-content");
  const bubble = node("div", "bubble");
  appendAnswer(bubble, turn.assistant);
  body.append(bubble);
  if (turn.sources?.length) appendSources(body, turn.sources);
  assistant.append(avatar, body);

  messages.append(assistant);
  scroll.scrollTop = scroll.scrollHeight;
}

function renderTurn(turn) {
  renderUser(turn.user);
  renderAnswer(turn);
}

function renderSuggestions(items) {
  for (const {question} of items) {
    const button = node("button", "suggestion", question);
    button.type = "button";
    button.addEventListener("click", () => sendQuestion(question));
    suggestions.append(button);
  }
}

async function sendQuestion(message, clearDraft = false) {
  if (!message || busy) return;
  busy = true;
  showError("");
  const pendingUser = renderUser(message);
  replyStatus.hidden = false;
  scroll.scrollTop = scroll.scrollHeight;
  updateControls();
  try {
    const {turn} = await request("./api/turn?view=public", {message});
    replyStatus.hidden = true;
    pendingUser.querySelector(".bubble").textContent = turn.user;
    renderAnswer(turn);
    if (clearDraft) input.value = "";
  } catch (problem) {
    pendingUser.remove();
    $("#welcome").hidden = messages.children.length > 0;
    showError(problem instanceof TypeError ? "连接失败，请检查网络后重试。" : problem.message);
  } finally {
    replyStatus.hidden = true;
    busy = false;
    updateControls();
    if (clearDraft) input.focus();
  }
}

async function resetConversation() {
  busy = true;
  showError("");
  updateControls();
  try {
    await request("./api/reset", {});
    messages.replaceChildren();
    $("#welcome").hidden = false;
    input.value = "";
    scroll.scrollTop = 0;
  } catch {
    showError("暂时无法开启新会话，请稍后重试。");
  } finally {
    busy = false;
    updateControls();
    input.focus();
  }
}

async function initialize() {
  try {
    const [items, session] = await Promise.all([
      request("./faq.json"),
      request("./api/session?view=public"),
    ]);
    renderSuggestions(items);
    for (const turn of session.turns) renderTurn(turn);
  } catch {
    showError("暂时无法加载会话，请刷新页面重试。");
  } finally {
    busy = false;
    updateControls();
  }
}

$("#reset").addEventListener("click", resetConversation);
$("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  sendQuestion(input.value.trim(), true);
});
input.addEventListener("input", updateControls);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});
initialize();
