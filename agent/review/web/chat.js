"use strict";

(() => {
  const host = window.reviewChatHost;
  if (!host) return;
  const state = host.state;
  const api = host.api;
  const ensureReview = host.ensureReview;
  function el(tag, text, cls) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (cls) item.className = cls;
    return item;
  }
  function button(text, action, cls) {
    const item = el("button", text, cls);
    item.type = "button";
    item.addEventListener("click", () => Promise.resolve().then(action).catch(e => showError(e.message)));
    return item;
  }
  function link(text, href, cls) {
    const item = el("a", text, cls);
    item.href = href;
    return item;
  }
  function svgElement(name, attributes) {
    const item = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attributes)) item.setAttribute(key, value);
    return item;
  }
  const root = el("div", undefined, "review-chat");
  root.id = "review-chat";
  const panel = el("section", undefined, "chat-panel");
  panel.id = "chat-panel";
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-labelledby", "chat-title");
  const header = el("div", undefined, "chat-header");
  const title = el("div");
  const heading = el("h2", "월급 도우미");
  heading.id = "chat-title";
  title.append(heading, el("p", "자료부터 결과까지, 다음 단계를 함께 확인해요"));
  const close = button("×", () => toggle(false), "chat-close");
  close.setAttribute("aria-label", "채팅창 닫기");
  header.append(title, close);
  const scope = el("div", undefined, "chat-scope");
  const scopeLabel = el("label", "현재 검토");
  scopeLabel.htmlFor = "chat-review-select";
  const select = el("select");
  select.id = "chat-review-select";
  scope.append(scopeLabel, select);
  const log = el("div", undefined, "chat-log");
  log.id = "chat-messages";
  log.setAttribute("role", "log");
  log.setAttribute("aria-live", "polite");
  log.setAttribute("aria-label", "월급 도우미 대화");
  const error = el("div", undefined, "chat-error");
  error.hidden = true;
  error.setAttribute("role", "status");
  const form = el("form", undefined, "chat-compose");
  const input = el("textarea");
  input.id = "chat-input";
  input.rows = 2;
  input.maxLength = 4000;
  input.placeholder = "어떤 단계에서 도움이 필요하세요?";
  input.setAttribute("aria-label", "월급 도우미에게 질문");
  const send = el("button", "보내기", "chat-send");
  send.type = "submit";
  form.append(input, send);
  const footnote = el("p", "현재 검토의 자료로 안내해요. 답변과 원문을 함께 확인해 주세요.", "chat-footnote");
  panel.append(header, scope, log, error, form, footnote);
  const launcher = button("", () => toggle(panel.hidden), "chat-launcher");
  launcher.id = "chat-launcher";
  launcher.setAttribute("aria-label", "월급 도우미 열기");
  launcher.setAttribute("aria-controls", "chat-panel");
  launcher.setAttribute("aria-expanded", "false");
  const icon = svgElement("svg", {viewBox: "0 0 24 24", width: 24, height: 24, "aria-hidden": "true"});
  icon.append(svgElement("path", {d: "M20 11.5a8 8 0 0 1-8 8H5l-3 2v-10a9 9 0 0 1 18 0Z", fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linejoin": "round"}));
  for (const x of [7, 11, 15]) icon.append(svgElement("circle", {cx: x, cy: 12, r: 1, fill: "currentColor"}));
  launcher.append(icon, el("span", "월급 도우미"));
  root.append(panel, launcher);
  document.body.append(root);

  let rid = null, chosen = null, data = {messages: []}, loading = false, posting = false;
  let timer = null, epoch = 0, signature = "", optionsKey = "", failedRequest = null, loadPromise = Promise.resolve();
  const drafts = new Map();
  const prompts = ["지금 어떤 단계부터 진행하면 돼?", "어떤 파일을 더 올려야 해?", "인식이 안 된 항목은 어떻게 확인해?", "대조 결과에서 차이가 나는 부분을 알려줘"];

  function busy() { return loading || posting || Boolean(data.active_job); }
  function controls() {
    send.disabled = busy() || !input.value.trim();
    send.textContent = posting || data.active_job ? "답변 중" : "보내기";
    log.setAttribute("aria-busy", String(loading || Boolean(data.active_job)));
    for (const item of log.querySelectorAll(".chat-prompt")) item.disabled = busy();
  }
  function showError(message) {
    error.textContent = message || "";
    error.hidden = !message;
  }
  function messageText(message) {
    const text = el("p", undefined, "chat-message-text");
    if (message.role !== "assistant") {
      text.textContent = message.content;
      return text;
    }
    // Render limited inline emphasis using text nodes; model HTML is never evaluated.
    for (const part of message.content.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g)) {
      if (part.startsWith("**") && part.endsWith("**")) text.append(el("strong", part.slice(2, -2)));
      else if (part.startsWith("`") && part.endsWith("`")) text.append(el("code", part.slice(1, -1)));
      else text.append(document.createTextNode(part));
    }
    return text;
  }
  function toggle(open) {
    panel.hidden = !open;
    launcher.setAttribute("aria-expanded", String(open));
    launcher.setAttribute("aria-label", open ? "월급 도우미 닫기" : "월급 도우미 열기");
    launcher.classList.toggle("is-open", open);
    if (open) {
      sync();
      if (rid && !busy()) loadPromise = refresh();
      input.focus({preventScroll: true});
    } else launcher.focus({preventScroll: true});
  }
  function renderMessages(force = false) {
    const key = JSON.stringify([rid, loading, data.messages, data.active_job, data.last_job]);
    controls();
    if (!force && signature === key) return;
    const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    signature = key;
    log.replaceChildren();
    if (!data.messages.length) {
      const welcome = el("div", undefined, "chat-welcome");
      welcome.append(el("span", "✦", "chat-welcome-mark"), el("h3", "막힌 부분을 함께 살펴볼까요?"), el("p", "업로드할 자료, 직접 확인할 항목, 대조 결과까지 현재 검토를 바탕으로 안내해 드려요."));
      for (const text of prompts) welcome.append(button(text + " ↗", () => submit(text), "chat-prompt"));
      if (!rid && state.view !== "new" && !state.summaries.length) {
        welcome.append(link("새 월급 검토 시작하기 →", "/review/new", "chat-source"));
      }
      log.append(welcome);
    }
    if (loading) log.append(el("p", "대화를 불러오는 중입니다…", "chat-status"));
    for (const message of data.messages) {
      const item = el("article", undefined, "chat-message " + message.role);
      item.dataset.messageId = message.id;
      item.append(el("small", message.role === "user" ? "나" : "월급 도우미"), messageText(message));
      if (message.role === "assistant") {
        if (message.steps?.length) {
          const steps = el("div", undefined, "chat-steps");
          for (const step of message.steps) {
            const card = el("section", undefined, "chat-step step-" + step.status);
            card.dataset.chatStage = step.stage;
            const head = el("div", undefined, "chat-step-head");
            const labels = {current: "현재 단계", waiting: "다음 단계", blocked: "확인 대기", completed: "완료"};
            head.append(el("span", String(step.number), "chat-step-number"), el("strong", step.stage_title), el("small", message.stale ? "이전 상태" : labels[step.status] || "확인 필요", "chat-step-status"));
            card.append(head, el("h4", step.title), el("p", step.description));
            if (step.reason) card.append(el("small", step.reason, "chat-step-reason"));
            for (const source of step.sources || []) {
              if (!/^\/(review\/|results\?comparison=)/.test(source.href)) continue;
              card.append(link(source.label + " ↗", source.href, "chat-source"));
            }
            steps.append(card);
          }
          item.append(steps);
        }
        if (message.mode === "guidance") item.append(el("small", "저장 상태로 기본 안내", "chat-mode"));
        if (message.stale || (state.review?.id === rid && message.revision !== state.review.revision)) item.append(el("p", "답변 이후 자료가 바뀌었습니다. 다시 질문하면 최신 상태로 확인합니다.", "chat-stale"));
        if (message.sources?.length) {
          const refs = el("div", undefined, "chat-sources");
          refs.append(el("small", "관련 자료·다음 단계"));
          for (const source of message.sources) {
            const a = link(source.label + (source.page ? " · " + source.page + "쪽" : "") + " ↗", source.href, "chat-source");
            // Stored links are server-generated; only local application paths are accepted.
            if (!/^\/(review\/|results\?comparison=)/.test(source.href)) continue;
            refs.append(a);
          }
          item.append(refs);
        }
      }
      log.append(item);
    }
    if (data.active_job) log.append(el("p", "현재 검토의 자료와 상태를 살펴보고 있어요…", "chat-status chat-thinking"));
    if (!data.active_job && ["failed", "interrupted"].includes(data.last_job?.status)) {
      const retry = el("div", undefined, "chat-status");
      retry.append(el("p", data.last_job.error || "답변이 중단되었습니다."), button("마지막 질문 다시 보내기", () => {
        const last = data.messages.findLast(m => m.role === "user");
        if (last) return submit(last.content);
      }, "chat-prompt"));
      log.append(retry);
    }
    controls();
    if (force || follow) {
      const lastMessage = data.messages.at(-1);
      const answer = log.querySelector(".chat-message.assistant:last-of-type");
      if (lastMessage?.role === "assistant" && lastMessage.steps?.length && answer) {
        log.scrollTop += answer.getBoundingClientRect().top - log.getBoundingClientRect().top;
      } else log.scrollTop = log.scrollHeight;
    }
  }
  async function refresh() {
    if (!rid) return;
    const captured = rid, version = epoch;
    try {
      const result = await api("/api/reviews/" + captured + "/chat");
      if (captured !== rid || version !== epoch) return;
      data = result;
      loading = false;
      showError("");
      renderMessages();
      clearTimeout(timer);
      if (data.active_job) timer = setTimeout(refresh, 900);
    } catch (e) {
      if (captured !== rid || version !== epoch) return;
      loading = false;
      showError("대화를 불러오지 못했습니다. " + e.message);
      controls();
      if (data.active_job) timer = setTimeout(refresh, 2500);
    }
  }
  function sync() {
    root.hidden = host.enabled === false;
    if (root.hidden) return;
    const fixed = state.review?.id;
    const next = fixed || (state.view === "new" ? null : (state.summaries.some(r => r.id === chosen) ? chosen : state.summaries[0]?.id || null));
    const choices = state.summaries.slice();
    if (fixed && !choices.some(r => r.id === fixed)) choices.unshift({id: fixed, title: state.review.title});
    const optionSignature = JSON.stringify([choices.map(r => [r.id, r.title]), next, state.view]);
    if (optionsKey !== optionSignature) {
      optionsKey = optionSignature;
      select.replaceChildren();
      if (!next) {
        const option = el("option", state.view === "new" ? "작성 중인 새 월급 검토" : "검토를 생성하면 자료를 함께 살펴볼 수 있어요");
        option.value = "";
        select.append(option);
      }
      for (const r of choices) {
        const option = el("option", r.id === fixed ? state.review.title : r.title);
        option.value = r.id;
        select.append(option);
      }
      select.value = next || "";
      select.disabled = Boolean(fixed) || state.view === "new" || !choices.length;
    }
    if (rid === next) return;
    drafts.set(rid || "new", input.value);
    rid = next;
    epoch++;
    clearTimeout(timer);
    data = {messages: []};
    posting = false;
    failedRequest = null;
    input.value = drafts.get(rid || "new") || "";
    loading = Boolean(rid);
    showError("");
    renderMessages(true);
    if (rid) loadPromise = refresh();
  }
  async function submit(text) {
    text = text.trim();
    if (!text || busy()) return;
    if (!rid) {
      if (state.view !== "new") {
        showError("새 월급 검토를 만들거나 위에서 안내받을 검토를 선택해 주세요.");
        return;
      }
      try {
        await ensureReview();
        sync();
        await loadPromise;
      } catch (e) { showError(e.message); return; }
    }
    const captured = rid, version = epoch;
    const request = failedRequest?.message === text ? failedRequest : {
      message: text, request_id: "message-" + crypto.randomUUID(), page: state.view,
      document_id: state.did || null, field_id: state.selectedField || null,
    };
    posting = true;
    showError("");
    controls();
    try {
      const result = await api("/api/reviews/" + captured + "/chat", "POST", request);
      if (captured !== rid || version !== epoch) return;
      data = result;
      input.value = "";
      drafts.delete(rid);
      failedRequest = null;
      posting = false;
      renderMessages(true);
      timer = setTimeout(refresh, 500);
    } catch (e) {
      if (captured !== rid || version !== epoch) return;
      failedRequest = request;
      posting = false;
      input.value = text;
      showError("질문을 전송하지 못했습니다. 다시 보내기를 눌러 주세요. " + e.message);
      controls();
    }
  }
  select.addEventListener("change", () => { chosen = select.value; sync(); });
  input.addEventListener("input", controls);
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void submit(input.value);
    }
  });
  panel.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); toggle(false); }
  });
  form.addEventListener("submit", event => { event.preventDefault(); void submit(input.value); });
  window.reviewChat = {sync};
  sync();
  renderMessages(true);
})();
