"use strict";

// Keep the same review conversation available on comparison/result/report pages.
(() => {
  const chatState = {review: null, summaries: [], view: "result"};
  async function chatApi(url, method = "GET", body) {
    const response = await fetch(url, {
      method,
      headers: {"X-Local-Agent": "1", "Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "서버 응답을 확인해 주세요.");
    return result;
  }
  const host = {state: chatState, api: chatApi, enabled: false};
  window.reviewChatHost = host;
  let selected = null, version = 0;
  async function updateContext() {
    const params = new URLSearchParams(location.search);
    const back = params.get("return");
    const comparison = params.get("comparison") || (back?.startsWith("/") ? new URL(back, location.origin).searchParams.get("comparison") : null);
    chatState.view = location.pathname.slice(1);
    if (selected === comparison) return;
    selected = comparison;
    const epoch = ++version;
    host.enabled = false;
    window.reviewChat?.sync();
    if (!comparison || !/^[a-z0-9-]+$/.test(comparison)) return;
    try {
      const context = await chatApi("/api/review-context/" + comparison);
      if (epoch !== version || !context.review_id) return;
      const review = await chatApi("/api/reviews/" + context.review_id);
      if (epoch !== version) return;
      chatState.review = review;
      chatState.summaries = [{id: review.id, title: review.title}];
      host.enabled = true;
      window.reviewChat?.sync();
    } catch { /* Legacy workspaces without a review continue to work independently. */ }
  }
  // Existing result navigation uses history.pushState without a full reload.
  window.addEventListener("popstate", updateContext);
  document.addEventListener("click", () => setTimeout(updateContext, 0));
  void updateContext();
})();
