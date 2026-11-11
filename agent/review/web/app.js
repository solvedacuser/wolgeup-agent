"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  review: null,
  summaries: [],
  workplaces: [],
  profiles: {},
  view: "home",
  rid: null,
  did: null,
  page: 1,
  selectedField: null,
  selected: new Set(),
  pending: new Map(),
  uploading: [],
  uploadErrors: [],
  library: [],
  timer: null,
  chain: Promise.resolve(),
  creating: null,
  disclosures: new Map(),
  executionSelections: new Map(),
  flowScrolls: new Map(),
  reviewFilter: "all",
};
const statusLabels = {
  queued: "대기",
  reading: "원문 판독 중",
  classifying: "문서 분류 중",
  extracting: "항목 추출 중",
  verifying: "재검증 중",
  completed: "처리 완료 · 사용자 확인 전",
  needs_review: "확인 필요",
  failed: "실패",
  interrupted: "중단",
  cancelled: "취소",
  superseded: "이전 처리 결과",
};
const busyStates = new Set([
  "queued",
  "reading",
  "classifying",
  "extracting",
  "verifying",
]);
const basicLabels = {
  employer_name: "근무지 이름",
  pay_type: "급여 형태",
  period_start: "급여 산정 시작일",
  period_end: "급여 산정 종료일",
  payment_date: "약정 지급일 (선택)",
};
const unitLabels = {
  text: "문자",
  date: "날짜",
  time: "시각",
  minute: "분",
  count: "건·일",
  category: "급여 형태",
  day_of_month: "매월 지급일",
  rate: "임금 단위 선택",
  KRW: "원",
  "KRW/hour": "원 / 시간",
  "KRW/month": "원 / 월",
  "KRW/year": "원 / 연",
  "KRW/day": "원 / 일",
};

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}
function button(text, fn, cls = "btn") {
  const e = el("button", text, cls);
  e.type = "button";
  e.addEventListener("click", () => safe(fn));
  return e;
}
function link(text, href, cls = "btn") {
  const e = el("a", text, cls);
  e.href = href;
  return e;
}
function badge(text, type = "") {
  return el("span", text, "badge " + type);
}
function saveDisclosures() {
  try {
    sessionStorage.setItem(
      "review-disclosures-v1",
      JSON.stringify([...state.disclosures]),
    );
  } catch {
    /* UI preferences remain in memory if browser storage is unavailable. */
  }
}
function rememberDisclosures() {
  for (const canvas of document.querySelectorAll("[data-flow-scroll]"))
    state.flowScrolls.set(canvas.dataset.flowScroll, canvas.scrollLeft);
  for (const item of document.querySelectorAll(
    "details[data-disclosure-key]",
  )) {
    state.disclosures.set(item.dataset.disclosureKey, item.open);
  }
  saveDisclosures();
}
function disclosure(key, cls, initiallyOpen = false) {
  const item = el("details", undefined, cls);
  const scoped = (state.review?.id || "new") + ":" + key;
  item.dataset.disclosureKey = scoped;
  item.open = state.disclosures.has(scoped)
    ? state.disclosures.get(scoped)
    : initiallyOpen;
  item.addEventListener("toggle", () => {
    if (!item.isConnected) return;
    state.disclosures.set(scoped, item.open);
    saveDisclosures();
  });
  return item;
}
function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => ($("toast").hidden = true), 3500);
}
function notice(text) {
  $("notice").textContent = text || "";
  $("notice").hidden = !text;
}
async function safe(fn) {
  try {
    await fn();
  } catch (e) {
    notice(e.message || "요청을 처리하지 못했습니다.");
  }
}
async function api(url, method = "GET", body, extra = {}) {
  const headers = { "X-Local-Agent": "1", ...extra };
  if (body !== undefined && !(body instanceof File))
    headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    method,
    headers,
    body:
      body === undefined
        ? undefined
        : body instanceof File
          ? body
          : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "서버 응답을 확인해 주세요.");
    error.status = response.status;
    throw error;
  }
  return result;
}
async function ensureReview() {
  if (state.review) return state.review;
  if (!state.creating)
    state.creating = api("/api/reviews", "POST", {})
      .then((review) => {
        state.review = review;
        state.rid = review.id;
        history.replaceState(null, "", "/review/new?draft=" + review.id);
        return review;
      })
      .finally(() => (state.creating = null));
  return state.creating;
}
function enqueue(fn) {
  const action = state.chain.catch(() => {}).then(fn);
  state.chain = action;
  return action;
}
async function mutate(action, body = {}, method = "POST") {
  return enqueue(async () => {
    await ensureReview();
    try {
      state.review = await api(
        "/api/reviews/" + state.review.id + (action ? "/" + action : ""),
        method,
        { ...body, revision: state.review.revision },
      );
      render();
      return state.review;
    } catch (e) {
      if (e.status === 409) {
        state.review = await api("/api/reviews/" + state.review.id);
        render();
      }
      throw e;
    }
  });
}
async function refreshList() {
  const result = await api("/api/reviews");
  state.summaries = result.reviews;
  state.workplaces = result.workplaces;
  state.profiles = result.profiles;
  renderSidebar();
}
function hasFocusedInput() {
  const e = document.activeElement;
  return (
    e &&
    $("content").contains(e) &&
    (e.tagName === "TEXTAREA" ||
      (e.tagName === "INPUT" &&
        !["checkbox", "radio", "button", "file"].includes(e.type)))
  );
}
async function navigate(url) {
  await state.chain.catch(() => {});
  history.pushState(null, "", url);
  await loadRoute();
}
async function loadRoute() {
  clearTimeout(state.timer);
  notice("");
  state.selected.clear();
  state.selectedField = null;
  state.pending.clear();
  state.did = null;
  state.page = 1;
  const path = location.pathname;
  const routeQuery = new URLSearchParams(location.search);
  const requestedPage = Number(routeQuery.get("page"));
  if (Number.isInteger(requestedPage) && requestedPage > 0)
    state.page = requestedPage;
  state.selectedField = routeQuery.get("field") || null;
  if (state.selectedField) state.reviewFilter = "all";
  const match = path.match(
    /^\/review\/(review-[a-f0-9]+)\/(materials|verify)$/,
  );
  state.view = match
    ? match[2]
    : path === "/review/new"
      ? "new"
      : path === "/review/library"
        ? "library"
        : "home";
  state.rid = match
    ? match[1]
    : state.view === "new"
      ? new URLSearchParams(location.search).get("draft")
      : null;
  state.review = state.rid ? await api("/api/reviews/" + state.rid) : null;
  await refreshList();
  if (state.view === "library")
    state.library = (await api("/api/documents")).documents.filter(
      (d) => d.kind !== "review_part",
    );
  render(true);
  poll();
  window.scrollTo(0, 0);
}
async function poll() {
  clearTimeout(state.timer);
  if (!state.review) return;
  try {
    const rid = state.review.id;
    const review = await api("/api/reviews/" + rid);
    if (state.review?.id !== rid) return;
    if (review.revision >= state.review.revision) state.review = review;
    render();
    const active =
      review.documents.some(
        (d) => !d.excluded && busyStates.has(d.job?.status),
      ) || review.requests.some((r) => r.state === "running");
    const recommendationActive = Boolean(review.recommendation_review?.active_job);
    state.timer = setTimeout(() => safe(poll), active || recommendationActive ? 900 : 3500);
  } catch (e) {
    notice(e.message);
    state.timer = setTimeout(() => safe(poll), 4000);
  }
}

function renderSidebar() {
  const target = $("workplaces");
  target.replaceChildren();
  const groups = new Map();
  for (const r of state.summaries.filter((r) => r.state === "active")) {
    const key = r.workplace_id || r.employer_name || "unknown";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  $("workplace-count").textContent = groups.size + "곳";
  for (const records of groups.values()) {
    const group = el("div", undefined, "workplace");
    const name = records[0].employer_name || "근무지 확인 필요";
    const title = el("div", undefined, "workplace-name");
    title.append(el("span", name[0], "workplace-letter"), el("span", name));
    group.append(title);
    for (const r of records) {
      const period =
        r.period_start && r.period_end
          ? r.period_start + " ~ " + r.period_end
          : "기간 확인 필요";
      group.append(
        link(
          "◷  " + period,
          "/review/" + r.id + "/materials",
          "workplace-period" + (r.id === state.review?.id ? " active" : ""),
        ),
      );
    }
    target.append(group);
  }
  if (!groups.size)
    target.append(el("p", "새 월급 검토를 만들어 주세요.", "empty"));
}
function heading(title, description) {
  const box = el("header");
  box.append(
    el("div", state.review?.title || "새 급여 검토 · Case 생성", "eyebrow"),
    el("h1", title),
    el("p", description, "description"),
  );
  return box;
}
function steps(current) {
  const box = el("div", undefined, "steps");
  for (const [key, label] of [
    ["new", "1  기본 정보"],
    ["materials", "2  자료 등록"],
    ["verify", "3  내용 확인"],
  ])
    box.append(el("div", label, "step" + (key === current ? " current" : "")));
  return box;
}
function render(force = false) {
  renderSidebar();
  for (const id of state.selected)
    if (window.reviewRecommendationUI?.critical(findField(id))) state.selected.delete(id);
  window.reviewChat?.sync();
  if (!force && hasFocusedInput()) {
    const monitor = $("execution-panel");
    if (monitor && state.review) {
      rememberDisclosures();
      monitor.replaceWith(executionPanel());
    }
    return;
  }
  const content = $("content");
  rememberDisclosures();
  content.replaceChildren();
  if (state.view === "home") renderHome(content);
  else if (state.view === "library") renderLibrary(content);
  else if (state.view === "new") renderNew(content);
  else if (state.review) {
    if (state.view === "materials") renderMaterials(content);
    else renderVerify(content);
  } else content.append(el("p", "검토를 불러올 수 없습니다.", "empty"));
  if (state.review && ["new", "materials", "verify"].includes(state.view)) {
    content.append(executionPanel());
  }
}
function renderHome(content) {
  content.append(
    heading(
      "내 월급 검토",
      "자료를 올리고 원문과 추출값을 확인해 급여를 검토합니다.",
    ),
    link("＋ 새 월급 검토", "/review/new", "btn primary"),
  );
  const list = el("div", undefined, "review-list");
  for (const r of state.summaries) {
    const card = link("", "/review/" + r.id + "/materials", "card review-tile");
    card.append(
      el("h3", r.title),
      el(
        "small",
        r.document_count +
          "개 자료 · " +
          (r.state === "draft" ? "작성 중" : "검토 사례"),
      ),
    );
    if (r.state === "draft") card.href = "/review/new?draft=" + r.id;
    list.append(card);
  }
  if (!state.summaries.length)
    list.append(
      el(
        "div",
        "아직 검토 사례가 없습니다. 새 월급 검토에서 파일을 추가해 보세요.",
        "card empty",
      ),
    );
  content.append(list);
}
function renderLibrary(content) {
  content.append(
    heading(
      "전체 자료함",
      "이 컴퓨터에 저장한 원본 자료입니다. 기존 자료는 자동으로 다시 분석하지 않습니다.",
    ),
  );
  const card = el("div", undefined, "card");
  for (const doc of state.library) {
    const row = el("div", undefined, "library-row"),
      info = el("div");
    info.append(el("strong", doc.title), el("small", doc.pages + "페이지"));
    row.append(
      info,
      link("원본 보기", "/api/documents/" + doc.id + "/original", "btn small"),
    );
    row.lastChild.target = "_blank";
    card.append(row);
  }
  if (!state.library.length)
    card.append(el("p", "저장된 자료가 없습니다.", "empty"));
  content.append(card);
}

function uploadBox() {
  const container = el("section", undefined, "card");
  container.append(el("h2", "파일 추가"));
  const drop = el("div", undefined, "dropzone"),
    input = el("input", undefined, "file-input");
  input.type = "file";
  input.accept = "application/pdf,image/jpeg,image/png,.pdf,.jpg,.jpeg,.png";
  input.multiple = true;
  input.id = "review-file-input";
  input.setAttribute("aria-label", "검토 파일 여러 개 선택");
  const pick = button("파일 선택", () => input.click(), "btn accent");
  drop.append(
    el("div", "↥", "drop-icon"),
    el("p", "파일을 여기에 끌어 놓으세요"),
    pick,
    input,
    el(
      "small",
      "PDF · JPG · PNG / 파일당 20MB, PDF 최대 20페이지\n추가하면 자동으로 문서를 읽고 AI로 항목을 추출합니다.",
    ),
  );
  input.addEventListener("change", () =>
    safe(() => uploadFiles([...input.files])),
  );
  for (const name of ["dragenter", "dragover"])
    drop.addEventListener(name, (e) => {
      e.preventDefault();
      drop.classList.add("dragover");
    });
  for (const name of ["dragleave", "drop"])
    drop.addEventListener(name, (e) => {
      e.preventDefault();
      drop.classList.remove("dragover");
    });
  drop.addEventListener("drop", (e) =>
    safe(() => uploadFiles([...e.dataTransfer.files])),
  );
  container.append(drop);
  for (const name of state.uploading)
    container.append(
      el("div", name + " · 업로드 중", "upload-line processing"),
    );
  for (const message of state.uploadErrors)
    container.append(el("div", message, "error-text"));
  if (state.review)
    container.append(button("기존 자료 연결", openLibraryPicker, "link"));
  return container;
}
async function uploadFiles(files, replaceId = null) {
  if (!files.length) return;
  await ensureReview();
  const uploadReviewId = state.review.id;
  state.uploadErrors = [];
  state.uploading.push(...files.map((f) => f.name));
  render();
  let cursor = 0;
  const worker = async () => {
    while (cursor < files.length) {
      const file = files[cursor++];
      try {
        if (!/\.(pdf|jpe?g|png)$/i.test(file.name))
          throw new Error("PDF·JPG·PNG만 지원합니다.");
        if (file.size > 20 * 1024 * 1024)
          throw new Error("파일당 20MB 이하로 올려 주세요.");
        const result = await api(
          "/api/reviews/" + uploadReviewId + "/documents",
          "POST",
          file,
          {
            "Content-Type": file.type || "application/octet-stream",
            "X-Document-Name": encodeURIComponent(file.name),
          },
        );
        await enqueue(async () => {
          const current = await api("/api/reviews/" + uploadReviewId);
          if (
            state.review?.id === uploadReviewId &&
            current.revision >= state.review.revision
          )
            state.review = current;
        });
        if (result.duplicate) toast(file.name + " · 이미 연결한 자료입니다.");
        else if (replaceId && state.review?.id === uploadReviewId)
          await mutate("documents/" + replaceId, { action: "exclude" });
      } catch (e) {
        state.uploadErrors.push(file.name + " · " + e.message);
      } finally {
        state.uploading.splice(state.uploading.indexOf(file.name), 1);
        render();
      }
    }
  };
  await Promise.all([worker(), worker()]);
  await refreshList();
  render();
  poll();
}
async function openLibraryPicker() {
  const docs = (await api("/api/documents")).documents.filter(
    (d) =>
      d.kind !== "review_part" &&
      !state.review.documents.some(
        (existing) => existing.id === d.id && !existing.excluded,
      ),
  );
  const card = el("div", undefined, "card");
  card.append(el("h2", "기존 자료 연결"));
  for (const doc of docs) {
    const row = el("div", undefined, "library-row");
    row.append(
      el("span", doc.title),
      button(
        "연결",
        async () => {
          await mutate("link", { document_id: doc.id });
          toast("기존 자료를 연결했습니다. 자동 재분석하지 않습니다.");
          card.remove();
        },
        "btn small",
      ),
    );
    card.append(row);
  }
  if (!docs.length)
    card.append(el("p", "추가로 연결할 기존 자료가 없습니다.", "empty"));
  card.append(button("닫기", () => card.remove(), "btn small"));
  $("content").prepend(card);
}

function renderNew(content) {
  content.append(
    heading(
      "근무지와 급여 기간",
      "먼저 자료를 추가하거나 정보를 직접 입력하세요. 추출된 정보는 확인 전 후보이며 언제든 수정할 수 있습니다.",
    ),
    uploadBox(),
  );
  const form = el("section", undefined, "card form-card"),
    grid = el("div", undefined, "grid");
  const workplaces = el("label", undefined, "field full"),
    select = el("select");
  workplaces.append(el("span", "근무지"));
  select.append(new Option("새 근무지 등록 또는 직접 입력", ""));
  for (const workplace of state.workplaces)
    select.append(new Option(workplace.name, workplace.id));
  select.value = state.review?.workplace_id || "";
  select.addEventListener("change", () =>
    safe(async () => {
      const choice = state.workplaces.find((w) => w.id === select.value);
      await mutate(
        "",
        {
          workplace_id: choice?.id || null,
          ...(choice ? { basic: { employer_name: choice.name } } : {}),
        },
        "PUT",
      );
    }),
  );
  workplaces.append(select);
  grid.append(workplaces);
  for (const key of Object.keys(basicLabels)) grid.append(basicInput(key));
  form.append(
    grid,
    el(
      "div",
      "급여 산정기간은 계약기간과 구분합니다. 사례를 생성해도 값이 자동 확정되거나 계산되지는 않습니다.",
      "info",
    ),
    button(
      "사례 생성 후 자료 등록 →",
      async () => {
        await ensureReview();
        await mutate("", { activate: true }, "PUT");
        await refreshList();
        await navigate("/review/" + state.review.id + "/materials");
      },
      "btn primary wide",
    ),
  );
  form.lastChild.style.marginTop = "20px";
  if (state.review) form.prepend(window.reviewRecommendationUI?.controls("basic") || el("div"));
  content.append(form);
  const criticalQueue = window.reviewRecommendationUI?.queue();
  if (criticalQueue) content.append(criticalQueue);
  if (state.review?.documents.length) {
    content.append(el("h2", "자료에서 읽은 정보"));
    for (const doc of state.review.documents.filter((d) => !d.excluded))
      content.append(documentCard(doc, true));
  }
}
function basicInput(key) {
  const field = state.review?.basic[key] || { value: "", candidates: [] },
    wrap = el(
      "label",
      undefined,
      "field" +
        (key === "employer_name" ? " full" : "") +
        (field.conflict ? " warning" : ""),
    );
  wrap.append(el("span", basicLabels[key]));
  let input;
  if (key === "pay_type") {
    input = el("select");
    for (const [value, label] of [
      ["", "선택해 주세요"],
      ["hourly", "시급제"],
      ["monthly", "월급제"],
      ["annual", "연봉제"],
      ["daily", "일급제"],
    ])
      input.append(new Option(label, value));
  } else {
    input = el("input");
    input.type = ["period_start", "period_end", "payment_date"].includes(key)
      ? "date"
      : "text";
    input.placeholder = "자료에서 추출하거나 직접 입력";
  }
  input.id = "basic-" + key;
  input.value = state.pending.get("basic:" + key)?.value ?? field.value;
  input.addEventListener("input", () =>
    state.pending.set("basic:" + key, { value: input.value }),
  );
  input.addEventListener("change", () =>
    safe(() => saveBasic(key, input.value)),
  );
  input.addEventListener("blur", () =>
    safe(async () => {
      if (state.pending.has("basic:" + key)) await saveBasic(key, input.value);
      else render();
    }),
  );
  wrap.append(input);
  wrap.append(
    el(
      "small",
      field.confirmed
        ? "사용자 확인 완료"
        : field.conflict
          ? "확인 필요 · 자료별 값이 다릅니다"
          : field.value
            ? "자동 채움 또는 직접 입력 · 사용자 확인 전"
            : "추출되지 않았거나 아직 처리 중입니다.",
    ),
  );
  if (field.conflict)
    for (const candidate of field.candidates)
      wrap.append(
        button(
          candidate.title + " · " + candidate.value,
          () => saveBasic(key, candidate.value),
          "candidate-choice",
        ),
      );
  const recommendation = window.reviewRecommendationUI?.proposalCard("basic:" + key);
  if (recommendation) wrap.append(recommendation);
  return wrap;
}
async function saveBasic(key, value) {
  const pendingKey = "basic:" + key;
  state.pending.set(pendingKey, { value });
  await mutate("", { basic: { [key]: value } }, "PUT");
  if (state.pending.get(pendingKey)?.value === value)
    state.pending.delete(pendingKey);
  render();
}

function documentCard(doc, details = false) {
  const card = el(
    "section",
    undefined,
    "card file-card" +
      (doc.job?.status === "failed" ? " failed" : "") +
      (doc.excluded ? " excluded" : ""),
  );
  card.dataset.documentId = doc.id;
  const top = el("div", undefined, "spread"),
    title = el("div");
  title.append(
    el("h3", doc.title),
    el(
      "div",
      doc.pages +
        "페이지 · " +
        doc.confirmed_count +
        " / " +
        doc.field_count +
        "개 항목 확인",
      "file-meta",
    ),
  );
  const status = doc.excluded
    ? "검토에서 제외"
    : statusLabels[doc.job?.status] ||
      (doc.imported ? "기존 자료 연결됨" : "직접 검토");
  top.append(
    title,
    badge(
      status,
      doc.job?.status === "failed"
        ? "danger"
        : busyStates.has(doc.job?.status)
          ? "active"
          : doc.confirmed_count && doc.confirmed_count === doc.field_count
            ? "success"
            : "",
    ),
  );
  card.append(top);
  if (doc.job) {
    const stages = el("div", undefined, "file-stage");
    for (const [key, label] of [
      ["reading", "원문 판독"],
      ["classifying", "분류"],
      ["extracting", "추출"],
      ["verifying", "재검증"],
    ]) {
      const done = doc.job.events.some((e) => e.stage === key);
      stages.append(
        el(
          "span",
          label,
          doc.job.status === key ? "current processing" : done ? "done" : "",
        ),
      );
    }
    card.append(stages);
    if (doc.job.error) card.append(el("p", doc.job.error, "error-text"));
  }
  const actions = el("div", undefined, "field-actions");
  if (!doc.excluded) {
    if (busyStates.has(doc.job?.status))
      actions.append(
        button(
          "처리 중단",
          async () => {
            await api("/api/jobs/" + doc.job.id + "/cancel", "POST", {});
            await poll();
          },
          "btn small",
        ),
      );
    else
      actions.append(
        button(
          "다시 처리",
          () => mutate("documents/" + doc.id, { action: "retry" }),
          "btn small",
        ),
      );
    actions.append(
      button("다른 파일로 교체", () => replaceFile(doc.id), "btn small"),
      button(
        "검토에서 제외",
        () => mutate("documents/" + doc.id, { action: "exclude" }),
        "btn small danger",
      ),
      link(
        "내용 확인 →",
        "/review/" + state.review.id + "/verify?document=" + doc.id,
        "btn small accent",
      ),
    );
  }
  card.append(actions);
  if (doc.excluded) return card;
  for (const part of doc.parts) {
    const summary = disclosure(
      "part:" + doc.id + ":" + part.id,
      "part-summary",
      details,
    );
    summary.append(
      el(
        "summary",
        part.label +
          " · " +
          part.start_page +
          "–" +
          part.end_page +
          "쪽 · " +
          part.fields.filter((f) => f.review_state === "confirmed").length +
          "개 확인",
      ),
    );
    const body = el("div", undefined, "part-body");
    if (part.reason)
      body.append(
        el("p", part.reason, part.needs_review ? "warning-text" : "muted"),
      );
    if (details && part.fields.length) {
      const grid = el("div", undefined, "grid");
      for (const f of part.fields)
        grid.append(fieldEditor(doc, part, f, false));
      body.append(grid, addButtons(part));
    } else
      body.append(
        el(
          "small",
          part.fields.length +
            "개 추출 항목 · " +
            part.fields.filter((f) => f.validation_status !== "valid").length +
            "개 확인 필요",
          "muted",
        ),
      );
    summary.append(body);
    card.append(summary);
  }
  if (
    !doc.parts.length ||
    doc.parts.some((p) => p.needs_review) ||
    doc.job?.status === "failed"
  )
    card.append(classifyForm(doc));
  return card;
}
function replaceFile(did) {
  const input = el("input");
  input.type = "file";
  input.accept = ".pdf,.jpg,.jpeg,.png";
  input.addEventListener("change", () =>
    safe(() => uploadFiles([...input.files], did)),
  );
  input.click();
}
function classifyForm(doc) {
  const details = disclosure("classification:" + doc.id, "part-summary");
  details.append(el("summary", "문서 유형·페이지 범위 지정 / 직접 입력"));
  const rows = el("div"),
    addRow = (part = {}) => {
      const row = el("div", undefined, "inline classification-row"),
        select = el("select", undefined, "control");
      for (const [key, profile] of Object.entries(state.profiles))
        select.append(new Option(profile.label, key));
      select.value =
        part.document_type in state.profiles ? part.document_type : "contract";
      const start = el("input"),
        end = el("input");
      for (const input of [start, end]) {
        input.type = "number";
        input.min = 1;
        input.max = doc.pages;
        input.style.width = "65px";
      }
      start.value = part.start_page || 1;
      end.value = part.end_page || doc.pages;
      row.append(
        select,
        start,
        el("span", "~"),
        end,
        button("삭제", () => row.remove(), "btn small"),
      );
      rows.append(row);
    };
  if (doc.parts.length) doc.parts.forEach(addRow);
  else addRow();
  const parts = () =>
    [...rows.children].map((row) => ({
      document_type: row.querySelector("select").value,
      start_page: Number(row.querySelectorAll("input")[0].value),
      end_page: Number(row.querySelectorAll("input")[1].value),
    }));
  details.append(
    rows,
    button("구간 추가", () => addRow(), "btn small"),
    button(
      "지정 후 다시 추출",
      () =>
        mutate("documents/" + doc.id, { action: "classify", parts: parts() }),
      "btn small accent",
    ),
    button(
      "직접 입력으로 계속",
      () => mutate("documents/" + doc.id, { action: "manual", parts: parts() }),
      "btn small",
    ),
  );
  return details;
}
function renderMaterials(content) {
  content.append(
    heading(
      "자료 등록",
      "자료별 처리 상태를 확인하세요. 일부 자료가 없어도 가능한 검토는 계속 진행할 수 있습니다.",
    ),
    steps("materials"),
    uploadBox(),
  );
  const docs = state.review.documents.filter((d) => !d.excluded),
    done = docs.filter((d) => !busyStates.has(d.job?.status)).length;
  content.append(
    el(
      "p",
      done +
        " / " +
        docs.length +
        "개 자료 처리 종료 · 사용자 확인은 별도입니다.",
      "muted",
    ),
  );
  for (const doc of state.review.documents) content.append(documentCard(doc));
  if (!docs.length)
    content.append(el("div", "검토할 자료를 추가해 주세요.", "card empty"));
  const actions = el("div", undefined, "actions");
  actions.append(
    link("기본 정보 수정", "/review/new?draft=" + state.review.id),
    link(
      "내용 확인 →",
      "/review/" + state.review.id + "/verify",
      "btn primary",
    ),
  );
  content.append(actions);
}

const rowNames = {
  work: "근무",
  earning: "지급",
  deduction: "공제",
  transaction: "거래",
};
const groupedLinks = new Set(["linked", "partial", "manual"]);
const quietIssues = new Set([
  "MANUAL",
  "VISION_CANDIDATE",
  "NOT_FOUND",
  "MISSING_OUTPUT",
]);
// Informational validator notes that do not need user attention.
function shownIssue(code) {
  return !(
    code === "MANUAL" ||
    code === "VISION_CANDIDATE" ||
    code === "SOURCE_UNIT_FROM_HEADER" ||
    code === "SOURCE_SCALAR_SPELLING_RESTORED" ||
    code.startsWith("SOURCE_TEXT_")
  );
}
const reviewFilters = [
  ["all", "전체"],
  ["critical", "필수 확인"],
  ["attention", "확인 필요"],
  ["missing", "값 없음"],
  ["unconfirmed", "미확정"],
];
function findField(id) {
  for (const doc of state.review?.documents || [])
    for (const part of doc.parts)
      for (const field of part.fields) if (field.id === id) return field;
  return null;
}
function rowPrefix(field) {
  const row = field.field_id.match(
    /^(work|earning|deduction|transaction)_(\d+)_/,
  );
  return row ? rowNames[row[1]] + " " + row[2] + " · " : "";
}
// "attention": extracted but doubtful, conflicting, or put on hold.
// "missing": nothing extracted and nothing typed yet.
function fieldConcern(field) {
  if (window.reviewRecommendationUI?.critical(field)) return "critical";
  if (field.review_state === "confirmed") return null;
  if (field.source_kind === "ai_provisional" || field.assessment?.kind === "equivalent" || field.assessment?.kind === "ambiguous") return "attention";
  if (field.review_state === "deferred") return "attention";
  if (field.validation_status === "missing" && !field.value) return "missing";
  if (
    field.validation_status === "needs_review" ||
    (field.issues || []).some((c) => c === "CONFLICT" || c === "RECHECK_FAILED")
  )
    return "attention";
  return null;
}
function rowConcern(row, cells) {
  if (cells.some(({field}) => window.reviewRecommendationUI?.critical(field))) return "critical";
  const primary = new Set(row.primary);
  let result = null;
  for (const { key, field } of cells) {
    const concern = fieldConcern(field);
    if (concern === "critical") return "critical";
    if (concern === "attention") return "attention";
    if (concern === "missing" && primary.has(key)) result = "missing";
  }
  return result;
}
function rowRelevant(row, cells) {
  const primary = new Set(row.primary);
  return cells.filter(
    ({ key, field }) =>
      primary.has(key) || field.value || field.raw_value != null,
  );
}
function reviewItems(part) {
  const byId = new Map(part.fields.map((f) => [f.id, f]));
  const grouped = new Map(),
    loose = new Map();
  for (const row of part.rows || [])
    for (const id of Object.values(row.cells))
      (groupedLinks.has(row.link) ? grouped : loose).set(id, row);
  const items = [],
    seen = new Set();
  for (const field of part.fields) {
    const row = grouped.get(field.id);
    if (!row) {
      items.push({
        field,
        hint: loose.get(field.id)?.reason,
        concern: fieldConcern(field),
        unconfirmed: field.review_state !== "confirmed",
      });
      continue;
    }
    if (seen.has(row.id)) continue;
    seen.add(row.id);
    const cells = Object.entries(row.cells)
      .map(([key, id]) => ({ key, field: byId.get(id) }))
      .filter((cell) => cell.field);
    items.push({
      row,
      cells,
      concern: rowConcern(row, cells),
      unconfirmed: rowRelevant(row, cells).some(
        ({ field }) => field.review_state !== "confirmed",
      ),
    });
  }
  const priority = {critical: 0, attention: 1, missing: 2};
  return items.sort((a, b) => (priority[a.concern] ?? 3) - (priority[b.concern] ?? 3));
}
function itemVisible(item) {
  const filter = state.reviewFilter;
  if (filter === "unconfirmed") return item.unconfirmed;
  if (filter === "attention" || filter === "missing" || filter === "critical")
    return item.concern === filter;
  return true;
}
function itemFieldIds(item) {
  return item.row
    ? rowRelevant(item.row, item.cells).map(({ field }) => field.id)
    : [item.field.id];
}
function setReviewFilter(value) {
  state.reviewFilter = value;
  try {
    sessionStorage.setItem("review-filter-v1", value);
  } catch {
    /* The filter still applies for this page view. */
  }
  state.selected.clear();
  render(true);
}

function valueControl(doc, field, ariaLabel) {
  const pending = state.pending.get(field.id),
    value = pending?.value ?? field.value;
  const wrap = el("div", undefined, "field"),
    input = el(
      field.unit === "text" && value.length > 120 ? "textarea" : "input",
    );
  if (input.tagName === "INPUT")
    input.type =
      field.unit === "date" ? "date" : field.unit === "time" ? "time" : "text";
  input.value = value;
  input.id = "field-" + field.id;
  input.setAttribute("aria-label", ariaLabel);
  input.placeholder =
    field.validation_status === "missing"
      ? "확인 필요 · 직접 입력 가능"
      : "원문을 확인해 입력";
  input.addEventListener("focus", () => highlight(field, doc, false));
  input.addEventListener("input", () =>
    state.pending.set(field.id, {
      value: input.value,
      unit: state.pending.get(field.id)?.unit || field.unit,
    }),
  );
  input.addEventListener("blur", () =>
    safe(async () => {
      const item = state.pending.get(field.id);
      if (item) await saveField(field.id, item);
      else render();
    }),
  );
  wrap.append(input);
  const unit = field.unit;
  if (unit === "rate" || unit.startsWith("KRW")) {
    const select = el("select");
    select.setAttribute("aria-label", ariaLabel + " 단위");
    if (unit === "rate") select.append(new Option("임금 단위 선택", "rate"));
    for (const key of ["KRW", "KRW/hour", "KRW/month", "KRW/year", "KRW/day"])
      select.append(new Option(unitLabels[key], key));
    select.value = pending?.unit || unit;
    select.addEventListener("change", () =>
      safe(() =>
        saveField(field.id, { value: input.value, unit: select.value }),
      ),
    );
    wrap.append(select);
  } else wrap.append(el("small", unitLabels[unit] || unit));
  return wrap;
}
function candidateButton(field, candidate, prefix = "") {
  return button(
    prefix +
      (candidate.value ?? candidate.raw_value ?? "읽지 못함") +
      " · " +
      (candidate.unit || "") +
      " · " +
      (candidate.evidence?.page || "?") +
      "쪽",
    async () => {
      await mutate(
        "fields",
        { field_id: field.id, candidate_id: candidate.id },
        "PUT",
      );
      state.pending.delete(field.id);
      render();
    },
    "candidate-choice",
  );
}
function historyLine(item, prefix = "") {
  return el(
    "div",
    (item.at || "").slice(0, 19).replace("T", " ") +
      " · " +
      prefix +
      (item.event === "confirmed"
        ? "확정: " + item.value
        : "수정 전: " + (item.before?.value || "빈값")),
  );
}
function cardClass(confirmed, concern, ids) {
  return (
    "card field-card" +
    (concern === "critical" ? " critical-field" : "") +
    (confirmed ? " confirmed" : concern ? " needs-review" : "") +
    (ids.includes(state.selectedField) ? " selected" : "")
  );
}
function stateBadge(confirmed, partly, deferred, concern) {
  return confirmed
    ? badge("확인 완료", "success")
    : partly
      ? badge("일부 확정", "warning")
      : deferred
        ? badge("보류", "warning")
        : concern
          ? badge("확인 필요", "warning")
          : badge("미확정");
}

function fieldEditor(doc, part, field, full = true, hint = "") {
  const prefix = rowPrefix(field);
  const card = el(
    full ? "article" : "div",
    undefined,
    full
      ? cardClass(
          field.review_state === "confirmed",
          fieldConcern(field),
          [field.id],
        )
      : "field",
  );
  card.dataset.fieldId = field.id;
  card.dataset.fieldIds = field.id;
  const header = el("div", undefined, "field-header"),
    label = el("label");
  if (full) {
    const check = el("input");
    check.type = "checkbox";
    check.checked = state.selected.has(field.id);
    check.disabled = Boolean(window.reviewRecommendationUI?.critical(field));
    check.setAttribute("aria-label", prefix + field.label + " 선택");
    check.addEventListener("change", () => {
      if (check.checked) state.selected.add(field.id);
      else state.selected.delete(field.id);
      updateSelectionCount();
    });
    label.append(check);
  }
  label.append(el("span", prefix + field.label));
  header.append(
    label,
    stateBadge(
      field.review_state === "confirmed",
      false,
      field.review_state === "deferred",
      field.validation_status !== "valid",
    ),
  );
  card.append(header);
  if (window.reviewRecommendationUI?.critical(field)) card.append(el("strong", "필수 확인 · 최상단에서 기준값을 선택해 개별 확정해 주세요.", "critical-text"));
  card.append(valueControl(doc, field, prefix + field.label));
  const recommendation = window.reviewRecommendationUI?.proposalCard(field.id);
  if (recommendation) card.append(recommendation);
  if (field.raw_value != null)
    card.append(el("div", "원문 표기: " + field.raw_value, "source-value"));
  if (field.evidence?.quote)
    card.append(el("div", field.evidence.quote, "field-quote"));
  for (const code of field.issues || [])
    if (shownIssue(code))
      card.append(
        el("div", state.review.issue_labels[code] || code, "warning-text"),
      );
  if (hint) card.append(el("div", "행으로 묶지 않음 · " + hint, "row-link"));
  if (field.source_kind === "user_statement")
    card.append(
      el("small", "직접 입력한 값 · 문서 추출값과 별도 보관", "muted"),
    );
  if (field.candidates.length) {
    const candidates = disclosure("candidates:" + field.id, "field-candidates");
    candidates.append(
      el("summary", "추출 후보 " + field.candidates.length + "개 보기"),
    );
    for (const candidate of field.candidates)
      candidates.append(candidateButton(field, candidate));
    card.append(candidates);
  }
  const actions = el("div", undefined, "field-actions");
  actions.append(
    button(
      "원문 위치",
      () => {
        if (state.view !== "verify")
          return navigate(
            "/review/" +
              state.review.id +
              "/verify?document=" +
              doc.id +
              "&field=" +
              field.id,
          );
        highlight(field, doc, true);
      },
      "btn small accent",
    ),
  );
  if (full) {
    actions.append(
      button(
        "이 값 확정",
        async () => {
          if (window.reviewRecommendationUI?.critical(field)) {
            $("critical-review")?.scrollIntoView({behavior: "smooth", block: "start"});
            return;
          }
          await savePendingField(field);
          await mutate("confirm", { field_ids: [field.id] });
          toast("선택한 값을 확정했습니다.");
        },
        "btn small",
      ),
      button(
        "보류",
        async () => {
          state.pending.delete(field.id);
          await mutate(
            "fields",
            { field_id: field.id, value: "", unit: field.unit, defer: true },
            "PUT",
          );
        },
        "btn small",
      ),
      button(
        "재검증",
        () =>
          mutate("documents/" + doc.id, {
            action: "verify",
            field_ids: [field.id],
          }),
        "btn small",
      ),
    );
  }
  card.append(actions);
  if (full && field.history.length) {
    const history = disclosure("history:" + field.id, "history");
    history.append(
      el("summary", "수정·확정 이력 " + field.history.length + "건"),
    );
    for (const item of field.history.slice().reverse())
      history.append(historyLine(item));
    card.append(history);
  }
  return card;
}

// One card for one printed table row (e.g. 항목명 + 금액). Each cell is still
// stored, edited and confirmed as its own field; the card only groups them.
function rowEditor(doc, part, item) {
  const { row, cells } = item;
  const ids = cells.map(({ field }) => field.id);
  const relevant = rowRelevant(row, cells);
  const confirmedCount = relevant.filter(
    ({ field }) => field.review_state === "confirmed",
  ).length;
  const confirmed = relevant.length > 0 && confirmedCount === relevant.length;
  const byKey = Object.fromEntries(cells.map((c) => [c.key, c.field]));
  const title =
    byKey.label?.value ||
    byKey.label?.raw_value ||
    byKey.date?.value ||
    byKey.date?.raw_value ||
    "";
  const prefix =
    rowNames[row.prefix] + " " + row.index + (title ? " · " + title : "");
  const card = el(
    "article",
    undefined,
    cardClass(confirmed, item.concern, ids) + " row-card",
  );
  card.dataset.fieldId = ids[0];
  card.dataset.fieldIds = ids.join(" ");
  card.dataset.rowId = row.id;
  const header = el("div", undefined, "field-header"),
    label = el("label"),
    check = el("input");
  check.type = "checkbox";
  check.checked = relevant.some(({ field }) => state.selected.has(field.id));
  check.disabled = item.concern === "critical";
  check.setAttribute("aria-label", prefix + " 행 선택");
  check.addEventListener("change", () => {
    for (const { field } of relevant)
      if (check.checked && !window.reviewRecommendationUI?.critical(field) && (field.value || state.pending.get(field.id)?.value))
        state.selected.add(field.id);
      else state.selected.delete(field.id);
    updateSelectionCount();
  });
  label.append(check, el("span", prefix));
  header.append(
    label,
    stateBadge(
      confirmed,
      confirmedCount > 0 && !confirmed,
      relevant.some(({ field }) => field.review_state === "deferred"),
      item.concern,
    ),
  );
  card.append(header);
  card.append(
    el(
      "div",
      (row.link === "linked" ? "↔ 같은 행 · " : "") + row.reason,
      "row-link " + row.link,
    ),
  );
  const shown = new Set(relevant.map(({ key }) => key));
  for (const { key, field } of cells)
    if (
      field.review_state === "deferred" ||
      field.candidates.some((c) => c.value || c.raw_value)
    )
      shown.add(key);
  const cellBox = ({ key, field }) => {
    const box = el("div", undefined, "row-cell");
    box.dataset.cell = key;
    const head = el("div", undefined, "row-cell-head");
    head.append(el("span", field.label));
    const concern = fieldConcern(field);
    if (field.review_state === "confirmed")
      head.append(el("small", "✓ 확정", "cell-state success"));
    else if (field.review_state === "deferred")
      head.append(el("small", "보류", "cell-state warning"));
    else if (concern === "attention")
      head.append(el("small", "확인 필요", "cell-state warning"));
    else if (concern === "missing" && row.primary.includes(key))
      head.append(el("small", "값 없음", "cell-state warning"));
    box.append(head, valueControl(doc, field, prefix + " " + field.label));
    if (field.raw_value != null && field.raw_value !== field.value)
      box.append(el("div", "원문 표기: " + field.raw_value, "source-value"));
    if (field.source_kind === "user_statement" && field.value)
      box.append(el("small", "직접 입력한 값", "muted"));
    for (const code of field.issues || [])
      if (
        shownIssue(code) &&
        !quietIssues.has(code) &&
        code !== "LOCATION_UNAVAILABLE"
      )
        box.append(
          el("div", state.review.issue_labels[code] || code, "warning-text"),
        );
    return box;
  };
  const grid = el("div", undefined, "row-cells");
  for (const cell of cells) if (shown.has(cell.key)) grid.append(cellBox(cell));
  card.append(grid);
  const empty = cells.filter((cell) => !shown.has(cell.key));
  if (empty.length) {
    const more = disclosure("row-empty:" + doc.id + ":" + row.id, "row-empty");
    more.append(
      el(
        "summary",
        "빈 칸 " +
          empty.length +
          "개 · " +
          empty.map(({ field }) => field.label).join(", "),
      ),
    );
    const extra = el("div", undefined, "row-cells");
    for (const cell of empty) extra.append(cellBox(cell));
    more.append(extra);
    card.append(more);
  }
  const sourced = cells.find(({ field }) => field.evidence?.quote)?.field;
  if (sourced)
    card.append(
      el(
        "div",
        sourced.evidence.row_quote || sourced.evidence.quote,
        "field-quote",
      ),
    );
  if (
    cells.some(({ field }) =>
      (field.issues || []).includes("LOCATION_UNAVAILABLE"),
    )
  )
    card.append(
      el(
        "div",
        state.review.issue_labels.LOCATION_UNAVAILABLE ||
          "정확한 위치 확인 필요",
        "warning-text",
      ),
    );
  const withCandidates = cells.filter(({ field }) => field.candidates.length);
  if (withCandidates.length) {
    const total = withCandidates.reduce(
      (sum, { field }) => sum + field.candidates.length,
      0,
    );
    const candidates = disclosure(
      "candidates:row:" + ids[0],
      "field-candidates",
    );
    candidates.append(el("summary", "추출 후보 " + total + "개 보기"));
    for (const { field } of withCandidates)
      for (const candidate of field.candidates)
        candidates.append(
          candidateButton(field, candidate, field.label + ": "),
        );
    card.append(candidates);
  }
  const focusField =
    cells.find(({ field }) => field.evidence?.page)?.field || cells[0].field;
  for (const {field} of cells) {
    const recommendation = window.reviewRecommendationUI?.proposalCard(field.id);
    if (recommendation) card.append(recommendation);
  }
  if (item.concern === "critical") card.append(el("strong", "필수 확인 · 최상단에서 해당 항목을 개별 확정해 주세요.", "critical-text"));
  const actions = el("div", undefined, "field-actions");
  actions.append(
    button(
      "원문 위치",
      () => highlight(focusField, doc, true),
      "btn small accent",
    ),
    button(
      "이 행 확정",
      async () => {
        if (item.concern === "critical") {
          $("critical-review")?.scrollIntoView({behavior: "smooth", block: "start"});
          return;
        }
        for (const { field } of cells) await savePendingField(field);
        const fresh = relevant
          .map(({ key, field }) => ({ key, field: findField(field.id) }))
          .filter(({ field }) => field);
        const blank = fresh.filter(
          ({ key, field }) => row.primary.includes(key) && !field.value,
        );
        if (blank.length)
          throw new Error(
            blank.map(({ field }) => field.label).join(", ") +
              " 값을 입력하거나 보류한 뒤 확정해 주세요.",
          );
        const targets = fresh
          .filter(
            ({ field }) => field.value && field.review_state !== "confirmed",
          )
          .map(({ field }) => field.id);
        if (!targets.length) return toast("이미 확정된 행입니다.");
        await mutate("confirm", { field_ids: targets });
        toast(prefix + " 행을 확정했습니다.");
      },
      "btn small",
    ),
    button(
      "보류",
      async () => {
        for (const { key, field } of cells) {
          if (!row.primary.includes(key) || field.review_state === "deferred")
            continue;
          state.pending.delete(field.id);
          await mutate(
            "fields",
            { field_id: field.id, value: "", unit: field.unit, defer: true },
            "PUT",
          );
        }
      },
      "btn small",
    ),
    button(
      "재검증",
      () =>
        mutate("documents/" + doc.id, {
          action: "verify",
          field_ids: relevant.map(({ field }) => field.id).slice(0, 50),
        }),
      "btn small",
    ),
  );
  card.append(actions);
  const history = cells.flatMap(({ field }) =>
    field.history.map((entry) => ({ entry, label: field.label })),
  );
  if (history.length) {
    const box = disclosure("history:row:" + ids[0], "history");
    box.append(el("summary", "수정·확정 이력 " + history.length + "건"));
    history.sort((a, b) => (b.entry.at || "").localeCompare(a.entry.at || ""));
    for (const { entry, label } of history)
      box.append(historyLine(entry, label + " "));
    card.append(box);
  }
  return card;
}
async function saveField(fid, item) {
  state.pending.set(fid, item);
  await mutate(
    "fields",
    { field_id: fid, value: item.value, unit: item.unit },
    "PUT",
  );
  if (state.pending.get(fid)?.value === item.value) state.pending.delete(fid);
  render();
}
async function savePendingField(field) {
  const pending = state.pending.get(field.id);
  if (pending) await saveField(field.id, pending);
}
function addButtons(part) {
  const actions = el("div", undefined, "field-actions");
  actions.append(
    button(
      "＋ 항목 추가",
      async () => {
        const label = prompt("추가할 항목의 이름을 입력하세요.");
        if (label?.trim())
          await mutate("fields", { part_id: part.id, label: label.trim() });
      },
      "btn small",
    ),
  );
  for (const [key, row] of Object.entries(
    state.profiles[part.document_type]?.rows || {},
  ))
    actions.append(
      button(
        "＋ " + row.label + " 행 추가",
        () => mutate("fields", { part_id: part.id, row_type: key }),
        "btn small",
      ),
    );
  return actions;
}

function renderVerify(content) {
  content.append(
    heading(
      "내용 확인",
      "추출된 값과 실제 원문을 비교해 계산에 사용할 값을 확정합니다.",
    ),
    steps("verify"),
  );
  const info = el("div", undefined, "info");
  info.append(
    el("strong", "원문 판독과 사용자 확인을 별도로 관리합니다."),
    el(
      "div",
      "모든 추출값은 확인 전 후보입니다. 확인하지 못한 값은 0으로 간주하지 않습니다.",
    ),
  );
  content.append(info);
  const docs = state.review.documents.filter((d) => !d.excluded),
    query = new URLSearchParams(location.search);
  state.did = state.did || query.get("document") || docs[0]?.id;
  const current = docs.find((d) => d.id === state.did) || docs[0];
  if (!current) {
    content.append(uploadBox());
    return;
  }
  state.did = current.id;
  state.page = Math.min(state.page, current.pages);
  const tabs = el("div", undefined, "document-tabs");
  for (const doc of docs)
    tabs.append(
      button(
        doc.title,
        () => {
          state.did = doc.id;
          state.page = 1;
          state.selectedField = null;
          state.selected.clear();
          render(true);
        },
        "btn small" + (doc.id === current.id ? " selected" : ""),
      ),
    );
  content.append(tabs);
  const layout = el("div", undefined, "evidence-layout"),
    viewer = el("section", undefined, "card viewer");
  viewer.id = "viewer";
  const tools = el("div", undefined, "viewer-controls");
  tools.append(
    el("strong", "원문"),
    link(
      "원본 열기",
      "/api/documents/" + current.id + "/original",
      "btn small",
    ),
  );
  tools.lastChild.target = "_blank";
  viewer.append(tools);
  const stage = el("div", undefined, "page-stage"),
    image = el("img", undefined, "page-image"),
    svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  image.id = "source-page";
  image.alt = current.title + " 원문 " + state.page + "페이지";
  image.src =
    "/api/reviews/" +
    state.review.id +
    "/documents/" +
    current.id +
    "/pages/" +
    state.page +
    ".png";
  image.addEventListener("error", () => {
    const note = $("source-note");
    if (note)
      note.textContent =
        "원문 미리보기를 만들지 못했습니다. 원본 열기로 확인해 주세요.";
  });
  svg.id = "source-overlay";
  svg.classList.add("overlay");
  svg.setAttribute("viewBox", "0 0 1 1");
  svg.setAttribute("preserveAspectRatio", "none");
  stage.append(image, svg);
  viewer.append(stage);
  const pages = el("div", undefined, "page-controls");
  const prev = button("←", () => changePage(current, -1), "btn small"),
    next = button("→", () => changePage(current, 1), "btn small");
  prev.disabled = state.page <= 1;
  next.disabled = state.page >= current.pages;
  pages.append(
    prev,
    el("span", state.page + " / " + current.pages + "페이지"),
    next,
  );
  viewer.append(pages);
  const note = el(
    "div",
    "항목의 ‘원문 위치’를 누르면 해당 영역이 강조됩니다.",
    "source-note",
  );
  note.id = "source-note";
  viewer.append(
    note,
    button(
      "원문 위치 다시 읽기",
      () => mutate("documents/" + current.id, { action: "locate" }),
      "btn small",
    ),
  );
  layout.append(viewer);
  const fields = el("section");
  const sections = current.parts.map((part) => ({
    part,
    items: reviewItems(part),
  }));
  const allItems = sections.flatMap(({ items }) => items);
  const filters = el("div", undefined, "review-filter");
  filters.setAttribute("role", "group");
  filters.setAttribute("aria-label", "표시할 항목");
  for (const [key, text] of reviewFilters) {
    const total =
      key === "all"
        ? allItems.length
        : allItems.filter((item) =>
            key === "unconfirmed" ? item.unconfirmed : item.concern === key,
          ).length;
    const chip = button(
      text + " " + total,
      () => setReviewFilter(key),
      "filter-chip" + (state.reviewFilter === key ? " selected" : ""),
    );
    chip.dataset.filter = key;
    chip.setAttribute("aria-pressed", String(state.reviewFilter === key));
    filters.append(chip);
  }
  const toolbar = el("div", undefined, "selection-toolbar"),
    count = el("small", "0개 선택");
  count.id = "selection-count";
  toolbar.append(
    filters,
    count,
    button(
      "값 있는 항목 선택",
      () => {
        // Only cards currently shown; a filter must never hide what gets confirmed.
        for (const item of allItems.filter(itemVisible))
          for (const id of itemFieldIds(item))
            if (findField(id)?.value && !window.reviewRecommendationUI?.critical(findField(id))) state.selected.add(id);
        render(true);
      },
      "btn small",
    ),
    button(
      "선택 항목 확정",
      async () => {
        for (const part of current.parts)
          for (const f of part.fields)
            if (state.selected.has(f.id)) await savePendingField(f);
        await mutate("confirm", { field_ids: [...state.selected] });
        state.selected.clear();
        render();
        toast("선택한 항목을 확정했습니다.");
      },
      "btn small primary",
    ),
  );
  fields.append(toolbar);
  const criticalQueue = window.reviewRecommendationUI?.queue();
  if (criticalQueue) fields.prepend(criticalQueue);
  let hidden = 0;
  for (const { part, items } of sections) {
    const visible = items.filter(itemVisible);
    hidden += items.length - visible.length;
    if (!visible.length && state.reviewFilter !== "all" && items.length)
      continue;
    fields.append(el("h2", part.label, "section-caption"));
    fields.append(window.reviewRecommendationUI?.controls(part.id) || el("div"));
    if (part.needs_review) fields.append(el("p", part.reason, "warning-text"));
    for (const item of visible)
      fields.append(
        item.row
          ? rowEditor(current, part, item)
          : fieldEditor(current, part, item.field, true, item.hint),
      );
    if (state.reviewFilter === "all") fields.append(addButtons(part));
  }
  if (state.reviewFilter !== "all") {
    const label = reviewFilters.find(([k]) => k === state.reviewFilter)[1];
    const note = el(
      "div",
      allItems.some(itemVisible)
        ? "‘" + label + "’ 항목만 표시 중 · 숨긴 카드 " + hidden + "개"
        : "‘" + label + "’에 해당하는 항목이 없습니다.",
      "card empty filter-note",
    );
    note.append(button("전체 보기", () => setReviewFilter("all"), "btn small"));
    fields.append(note);
  }
  if (!current.parts.length)
    fields.append(
      el(
        "div",
        "추출이 끝나면 항목이 표시됩니다. 실패한 경우 유형을 지정하고 직접 입력할 수 있습니다.",
        "card empty",
      ),
      classifyForm(current),
    );
  layout.append(fields);
  content.append(layout);
  drawOverlay(current);
  updateSelectionCount();
  if (state.selectedField) {
    const field = current.parts
      .flatMap((p) => p.fields)
      .find((f) => f.id === state.selectedField);
    if (field) highlight(field, current, false);
  }
  renderReadiness(content);
}
function changePage(doc, delta) {
  state.page = Math.min(doc.pages, Math.max(1, state.page + delta));
  render(true);
}
function drawOverlay(doc) {
  const svg = $("source-overlay");
  if (!svg) return;
  svg.replaceChildren();
  for (const field of doc.parts.flatMap((p) => p.fields)) {
    const ev = field.evidence;
    if (ev?.page !== state.page || ev.location_status !== "located") continue;
    for (const bounds of ev.boxes) {
      const rect = document.createElementNS(svg.namespaceURI, "rect");
      rect.setAttribute("x", bounds[0]);
      rect.setAttribute("y", bounds[1]);
      rect.setAttribute("width", Math.max(0.001, bounds[2] - bounds[0]));
      rect.setAttribute("height", Math.max(0.001, bounds[3] - bounds[1]));
      rect.setAttribute(
        "class",
        "source-rect" + (state.selectedField === field.id ? " selected" : ""),
      );
      rect.setAttribute("aria-label", field.label);
      rect.setAttribute("role", "button");
      rect.setAttribute("tabindex", "0");
      const focus = () => highlight(field, doc, true);
      rect.addEventListener("click", focus);
      rect.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") focus();
      });
      svg.append(rect);
    }
  }
}
function highlight(field, doc, scroll) {
  state.selectedField = field.id;
  const ev = field.evidence;
  if (state.view !== "verify") return;
  if (ev?.page && state.page !== ev.page) {
    state.page = ev.page;
    render(true);
  }
  for (const card of document.querySelectorAll(".field-card"))
    card.classList.toggle(
      "selected",
      (card.dataset.fieldIds || "").split(" ").includes(field.id),
    );
  drawOverlay(doc);
  const note = $("source-note");
  if (note)
    note.textContent =
      ev?.location_status === "located"
        ? field.label +
          " · 원문 " +
          ev.page +
          "페이지 · 강조 영역을 확인해 주세요."
        : field.source_kind === "user_statement"
          ? "직접 입력한 값입니다. 연결된 원문 표기와 다를 수 있습니다."
          : "정확한 위치 확인 필요 · " +
            (ev?.quote || "연결할 원문을 찾지 못했습니다.");
  if (scroll)
    document
      .querySelector('.field-card[data-field-ids~="' + field.id + '"]')
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function updateSelectionCount() {
  if ($("selection-count"))
    $("selection-count").textContent = state.selected.size + "개 선택";
}

const requestInfo = {
  compare: {
    title: "확정한 값으로 문서 대조",
    detail:
      "확정한 값끼리 계약 단가·근무시간·지급/공제 합계·실수령액·입금액을 대조한 결과입니다.",
    link: "문서 대조 결과 보기 →",
    target: "results",
  },
  prepare: {
    title: "표준 임금 계산 조건 입력",
    detail:
      "확정값을 불러와 표준 임금 계산에 쓸 조건(근로시간·수당 범위 등)을 입력하는 단계입니다.",
    link: "계산 조건 입력 →",
    target: "compare",
  },
};
function checkControl(label, key) {
  const wrap = el("label", undefined, "check"),
    input = el("input");
  input.type = "checkbox";
  input.checked = state.review.options[key];
  input.addEventListener("change", () =>
    safe(() => mutate("", { options: { [key]: input.checked } }, "PUT")),
  );
  wrap.append(input, el("span", label));
  return wrap;
}
function renderReadiness(content) {
  const card = el("section", undefined, "card");
  card.append(el("h2", "검토 범위와 실행 준비"));
  const basics = el("div", undefined, "grid");
  card.append(window.reviewRecommendationUI?.controls("basic") || el("div"));
  for (const key of Object.keys(basicLabels)) basics.append(basicInput(key));
  card.append(
    basics,
    button(
      "입력한 기본 정보 확인·확정",
      () => mutate("", { confirm_basic: true }, "PUT"),
      "btn accent",
    ),
  );
  for (const [kind, label] of [
    ["contract", "기준 근로계약서"],
    ["payslip", "기준 급여명세서"],
  ]) {
    const parts = state.review.documents
      .filter((d) => !d.excluded)
      .flatMap((d) =>
        d.parts
          .filter((p) => p.document_type === kind && !p.needs_review)
          .map((p) => ({ d, p })),
      );
    if (parts.length > 1) {
      const wrap = el("label", undefined, "field"),
        select = el("select");
      wrap.append(el("span", label));
      select.append(new Option("기준 자료를 선택해 주세요", ""));
      for (const { d, p } of parts)
        select.append(
          new Option(
            d.title + " · " + p.label + " · " + p.start_page + "쪽",
            p.id,
          ),
        );
      select.value = state.review.options[kind + "_part_id"] || "";
      select.addEventListener("change", () =>
        safe(() =>
          mutate(
            "",
            { options: { [kind + "_part_id"]: select.value || null } },
            "PUT",
          ),
        ),
      );
      wrap.append(select);
      card.append(wrap);
    }
  }
  for (const [key, label] of [
    [
      "subjects_confirmed",
      "선택한 자료가 같은 근로자·사업장의 자료임을 확인했습니다.",
    ],
    ["earnings_complete", "이번 기간의 지급 항목을 빠짐없이 확인했습니다."],
    ["deductions_complete", "이번 기간의 공제 항목을 빠짐없이 확인했습니다."],
    [
      "payment_scope_confirmed",
      "선택한 입금이 이번 급여기간에 해당하는 범위임을 확인했습니다.",
    ],
    [
      "distinct_transactions_confirmed",
      "중복 가능 거래를 확인했고 서로 다른 실제 거래임을 확인했습니다.",
    ],
  ])
    card.append(checkControl(label, key));
  for (const [key, label] of [
    ["worked_time_kind", "근태 시간의 의미"],
    ["slip_time_kind", "명세서 시간의 의미"],
  ]) {
    const wrap = el("label", undefined, "field"),
      select = el("select");
    wrap.append(el("span", label));
    for (const [value, title] of [
      ["", "미확인"],
      ["actual", "실제 근로시간"],
      ["paid", "유급·지급기초시간"],
    ])
      select.append(new Option(title, value));
    select.value = state.review.options[key];
    select.addEventListener("change", () =>
      safe(() => mutate("", { options: { [key]: select.value } }, "PUT")),
    );
    wrap.append(select);
    card.append(wrap);
  }
  for (const doc of state.review.documents.filter((d) => !d.excluded))
    for (const part of doc.parts.filter((p) => p.document_type === "deposit")) {
      const rows = new Map();
      for (const f of part.fields) {
        const m = f.field_id.match(/^transaction_(\d+)_(.+)$/);
        if (m) {
          if (!rows.has(m[1])) rows.set(m[1], {});
          rows.get(m[1])[m[2]] = f;
        }
      }
      for (const [index, row] of rows) {
        const wrap = el("label", undefined, "check"),
          input = el("input"),
          key = part.id + ":transaction_" + index;
        input.type = "checkbox";
        input.checked =
          state.review.options.selected_transactions.includes(key);
        input.disabled = ![row.date, row.deposit_amount].every(
          (f) => f?.review_state === "confirmed",
        );
        input.addEventListener("change", () =>
          safe(async () => {
            const values = new Set(state.review.options.selected_transactions);
            if (input.checked) values.add(key);
            else values.delete(key);
            await mutate(
              "",
              { options: { selected_transactions: [...values] } },
              "PUT",
            );
          }),
        );
        wrap.append(
          input,
          el(
            "span",
            "급여 입금으로 선택 · " +
              doc.title +
              " · " +
              (row.date?.value || "날짜 확인 필요") +
              " · " +
              (row.deposit_amount?.value || "금액 확인 필요") +
              "원",
          ),
        );
        card.append(wrap);
      }
    }
  for (const capability of state.review.capabilities) {
    const box = el("div", undefined, "request-row");
    box.append(el("h3", capability.label));
    if (requestInfo[capability.id])
      box.append(
        el("p", requestInfo[capability.id].detail, "request-detail"),
      );
    for (const missing of capability.missing)
      box.append(el("p", missing, "warning-text"));
    box.append(
      button(
        capability.status === "ready" ? "실행하기" : "준비되면 실행하도록 요청",
        () => mutate("requests", { capability: capability.id }),
        "btn primary",
      ),
    );
    card.append(box);
  }
  const requests = state.review.requests.slice().reverse();
  if (requests.length) {
    card.append(el("h3", "실행 기록 · 결과 보기", "request-history-title"));
    card.append(
      el(
        "p",
        "아래 결과는 각 실행을 요청한 시점의 확정값으로 만든 것입니다. 이후 값을 바꾸면 다시 실행해야 반영됩니다.",
        "execution-caption",
      ),
    );
  }
  const latestByCapability = new Map();
  for (const request of requests)
    if (
      request.comparison_id &&
      request.state === "completed" &&
      !latestByCapability.has(request.capability)
    )
      latestByCapability.set(request.capability, request.id);
  for (const request of requests) {
    const info = requestInfo[request.capability] || {
      title: request.capability,
      detail: "",
    };
    const latest = latestByCapability.get(request.capability) === request.id;
    const row = el(
      "div",
      undefined,
      "request-row request-history" + (latest ? "" : " older"),
    );
    row.dataset.requestId = request.id;
    const head = el("div", undefined, "request-head");
    head.append(
      el("strong", info.title),
      badge(
        {
          waiting: "자료·확정 대기",
          running: "실행 중",
          completed: "실행 완료",
          failed: "실행 실패",
          cancelled: "취소됨",
          interrupted: "중단됨",
        }[request.state] || request.state,
        request.state === "completed" ? "success" : "",
      ),
    );
    if (request.comparison_id && request.state === "completed")
      head.append(
        latest ? badge("최신 결과", "active") : badge("이전 실행 결과"),
      );
    if (request.stale)
      head.append(badge("입력 변경 · 결과 갱신 필요", "warning"));
    row.append(head);
    const meta = [
      "요청 " + new Date(request.created_at).toLocaleString("ko-KR"),
    ];
    if (Number.isInteger(request.confirmed_field_count))
      meta.push("확정값 " + request.confirmed_field_count + "개 기준");
    if (request.scope?.period_start || request.scope?.period_end)
      meta.push(
        "기간 " +
          (request.scope.period_start || "?") +
          " ~ " +
          (request.scope.period_end || "?"),
      );
    row.append(el("small", meta.join(" · "), "request-meta"));
    if (info.detail) row.append(el("p", info.detail, "request-detail"));
    if (request.error) row.append(el("p", request.error, "error-text"));
    const actions = el("div", undefined, "field-actions");
    if (request.comparison_id)
      actions.append(
        link(
          info.link,
          "/" + info.target + "?comparison=" + request.comparison_id,
          "btn small accent",
        ),
        link(
          "이 결과의 보고서",
          "/reports?comparison=" + request.comparison_id,
          "btn small",
        ),
      );
    if (["waiting", "running"].includes(request.state))
      actions.append(
        button(
          "요청 취소",
          () => mutate("requests/" + request.id),
          "btn small danger",
        ),
      );
    if (actions.children.length) row.append(actions);
    card.append(row);
  }
  content.append(card);
}

function tokens(value) {
  return Number.isInteger(value)
    ? value.toLocaleString("ko-KR")
    : "저장값 없음";
}
function usageText(usage, local = false) {
  return local ? "0 토큰 · 로컬 처리" : tokens(usage?.total_tokens) + " 토큰";
}
function executionStatus(status) {
  return (
    {
      running: "실행 중",
      completed: "완료",
      failed: "실패",
      cancelled: "취소",
      interrupted: "중단",
      queued: "대기",
      waiting: "확인·요건 대기",
    }[status] ||
    statusLabels[status] ||
    status
  );
}
function executionPanel() {
  const panel = el("section", undefined, "card execution-panel");
  panel.id = "execution-panel";
  panel.setAttribute("aria-label", "에이전트 실행·토큰 사용량");
  panel.append(el("h2", "에이전트 실행·토큰 사용량"));
  const execution = state.review.execution;
  if (!execution) {
    panel.append(
      el("p", "실행 기록을 불러오려면 서버를 다시 시작해 주세요.", "muted"),
    );
    return panel;
  }
  panel.append(el("p", execution.scope, "execution-caption"));
  const usage = execution.usage || {};
  const summary = el("div", undefined, "execution-totals");
  for (const [key, label] of [
    ["total_tokens", "전체 작업 누적 토큰"],
    ["input_tokens", "누적 입력 토큰"],
    ["output_tokens", "누적 출력 토큰"],
  ]) {
    const box = el("div");
    const value = el(
      "strong",
      tokens(usage[key] ?? (execution.model_calls === 0 ? 0 : undefined)),
    );
    value.dataset.tokenMetric = key;
    box.append(el("span", label), value);
    summary.append(box);
  }
  const calls = el("div");
  calls.append(
    el("span", "모델 호출·시도"),
    el("strong", execution.model_calls + "회"),
  );
  summary.append(calls);
  panel.append(summary);
  if (execution.pending_calls)
    panel.append(
      el(
        "p",
        execution.pending_calls +
          "회 실행 중 · 응답이 도착하면 사용량이 갱신됩니다.",
        "source-note",
      ),
    );
  if (execution.unavailable_calls)
    panel.append(
      el(
        "p",
        "사용량 미확인 " +
          execution.unavailable_calls +
          "회 · 실패·취소 등으로 사용량 응답을 받지 못한 호출은 위 합계에 포함되지 않을 수 있습니다.",
        "warning-text",
      ),
    );
  if (execution.historical_jobs)
    panel.append(
      el(
        "p",
        "이전 실행 " +
          execution.historical_jobs +
          "건은 계측 기능 추가 전에 실행되어 총 토큰만 저장됐습니다. 분류·추출·재검증이 실패했다는 뜻이 아닙니다. 당시 단계별 사용량은 수집하지 않아 복원할 수 없으며, 새 실행부터 박스별 수치가 표시됩니다.",
        "source-note",
      ),
    );
  if (usage.cached_input_tokens || usage.reasoning_tokens)
    panel.append(
      el(
        "p",
        "입력 중 캐시: " +
          tokens(usage.cached_input_tokens) +
          " · 출력 중 추론: " +
          tokens(usage.reasoning_tokens) +
          " (입력·출력에 포함된 값)",
        "source-note",
      ),
    );
  const jobs = execution.jobs.slice().reverse();
  const selectedId = state.executionSelections.get(state.review.id) || "latest";
  const selected = jobs.find((job) => job.id === selectedId) || jobs[0];
  const chooser = el("div", undefined, "execution-chooser");
  const label = el("label", "강조해서 볼 작업");
  label.htmlFor = "execution-job-select";
  const select = el("select");
  select.id = "execution-job-select";
  const latest = el("option", "가장 최근 작업 자동 따라가기");
  latest.value = "latest";
  select.append(latest);
  for (const job of jobs) {
    const option = el(
      "option",
      job.title +
        " · " +
        triggerLabel(job) +
        " · " +
        new Date(job.created_at).toLocaleString("ko-KR"),
    );
    option.value = job.id;
    select.append(option);
  }
  select.value = jobs.some((job) => job.id === selectedId)
    ? selectedId
    : "latest";
  select.addEventListener("change", () => {
    state.executionSelections.set(state.review.id, select.value);
    rememberDisclosures();
    panel.replaceWith(executionPanel());
  });
  chooser.append(label, select);
  panel.append(chooser);
  if (selected) {
    const chosen = el("div", undefined, "execution-selected");
    chosen.append(el("h3", selected.title + " · " + triggerLabel(selected)));
    const metrics = el("div", undefined, "execution-totals selected-totals");
    for (const [key, label] of [
      ["total_tokens", "선택한 작업 총 토큰"],
      ["input_tokens", "이 작업 입력"],
      ["output_tokens", "이 작업 출력"],
    ]) {
      const metric = el("div");
      const value = el("strong", tokens(selected.usage?.[key]));
      value.dataset.selectedMetric = key;
      metric.append(el("span", label), value);
      metrics.append(metric);
    }
    const status = el("div");
    status.append(
      el("span", "작업 상태"),
      el("strong", executionStatus(selected.status)),
    );
    metrics.append(status);
    chosen.append(metrics);
    if (selected.model)
      chosen.append(
        el("p", "이 작업의 모델·엔진: " + selected.model, "execution-caption"),
      );
    if (selected.trace.some((event) => event.usage_status === "not_recorded"))
      chosen.append(
        el(
          "p",
          "이 작업은 총량만 저장된 과거 실행입니다. 파란 박스는 호출이 확인된 단계이며, 당시 분기·연결 순서와 모델별 토큰은 수집하지 않았습니다. 0토큰이나 실행 실패를 의미하지 않습니다.",
          "source-note",
        ),
      );
    if (selected.trace.some((event) => event.usage_status === "unavailable"))
      chosen.append(
        el(
          "p",
          "응답을 받지 못한 모델 호출이 있습니다. 선택 작업 합계는 확인된 사용량만 포함합니다.",
          "warning-text",
        ),
      );
    panel.append(chosen);
  }
  panel.append(el("h3", "전체 구현 구조 · 선택한 작업의 실행 경로"));
  const legend = el("div", undefined, "flow-legend");
  for (const [cls, text] of [
    ["completed", "파랑: 실행 확인"],
    ["running", "노랑: 실행·확인 대기"],
    ["failed", "빨강: 실패·중단"],
    ["idle", "회색: 구현돼 있으나 선택 작업의 실행 기록 없음"],
  ])
    legend.append(el("span", text, "legend-" + cls));
  panel.append(legend);
  panel.append(
    el(
      "p",
      "마름모는 조건 분기입니다. 문서 처리·업무 실행·챗봇 그래프는 각각 최근 또는 선택한 실행을 표시합니다. 모델 호출이 없는 PDF·OCR·규칙 계산은 0토큰입니다.",
      "execution-caption",
    ),
  );
  if (!execution.topology)
    panel.append(
      el(
        "p",
        "새 구조도를 불러오려면 Python 서버를 재시작해 주세요.",
        "source-note",
      ),
    );
  // Each graph shows the job of its own kind. Following "latest" must not blank the
  // document graph just because a business request ran after the uploads.
  const documentJobs = jobs.filter((job) => job.kind === "document"),
    businessJobs = jobs.filter((job) => job.kind === "business"),
    chatJobs = jobs.filter((job) => job.kind === "chat"),
    recommendationJobs = jobs.filter((job) => job.kind === "recommendation");
  const latestDocument =
    (state.view === "verify" &&
      documentJobs.find((job) => job.document_id === state.did)) ||
    documentJobs[0];
  const pinned = selectedId !== "latest" ? selected : null;
  const graphJobs = {
    document:
      pinned?.kind === "document" ? pinned : latestDocument || null,
    work: pinned?.kind === "business" ? pinned : businessJobs[0] || null,
    chat: pinned?.kind === "chat" ? pinned : chatJobs[0] || null,
    recommendation: pinned?.kind === "recommendation" ? pinned : recommendationJobs[0] || null,
  };
  for (const group of execution.topology || [])
    panel.append(
      executionDiagram(
        group,
        group.id in graphJobs ? graphJobs[group.id] : selected,
        group.id in graphJobs,
      ),
    );
  panel.append(
    el(
      "p",
      "우측 하단의 월급 도우미는 현재 검토의 자료·확인 상태·대조 결과를 바탕으로 안내합니다. 법령 검색과 MCP 서버 연결은 아직 제공하지 않습니다.",
      "execution-caption",
    ),
  );
  if (!execution.jobs.length)
    panel.append(
      el(
        "p",
        "파일을 처리하면 실제 호출 순서와 토큰 사용량이 여기에 기록됩니다.",
        "empty",
      ),
    );
  if (selected) panel.append(executionJob(selected, true));
  return panel;
}

function triggerLabel(job) {
  if (job.kind === "recommendation") return "표기 비교·추천";
  if (job.kind === "chat") return "프로젝트 질문";
  if (job.kind === "business") return "업무 실행";
  return (
    {
      upload: "업로드 자동 처리",
      retry: "다시 처리",
      classify: "유형 지정 후 처리",
      verify: "선택 항목 재검증",
      locate: "원문 위치 재판독",
      resume: "중단 후 재시작",
    }[job.trigger] || "문서 처리"
  );
}
function svgElement(name, attributes = {}) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes))
    element.setAttribute(key, value);
  return element;
}
function flowState(value) {
  if (!value) return "idle";
  if (["failed", "cancelled", "interrupted"].includes(value.status))
    return "failed";
  if (["running", "waiting"].includes(value.status)) return "running";
  return "completed";
}
function executionDiagram(group, job, labelled = false) {
  const section = disclosure("topology:" + group.id, "flow-group", true);
  const summary = el("summary");
  summary.append(el("span", group.title));
  if (labelled)
    summary.append(
      el(
        "small",
        job
          ? "표시 중: " +
              job.title +
              " · " +
              triggerLabel(job) +
              " · " +
              new Date(job.created_at).toLocaleString("ko-KR")
          : "아직 실행한 작업이 없습니다",
        "flow-group-job",
      ),
    );
  section.append(summary);
  const scroll = el("div", undefined, "flow-canvas-scroll");
  const scrollKey = state.review.id + ":" + group.id;
  scroll.dataset.flowScroll = scrollKey;
  scroll.tabIndex = 0;
  scroll.setAttribute(
    "aria-label",
    group.title + " · 좁은 화면에서 가로 스크롤",
  );
  const svg = svgElement("svg", {
    viewBox: `0 0 ${group.width} ${group.height}`,
    class: "flow-canvas",
    role: "img",
    "aria-label": group.title,
  });
  const defs = svgElement("defs");
  for (const status of ["idle", "completed", "running", "failed"]) {
    const marker = svgElement("marker", {
      id: `arrow-${group.id}-${status}`,
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 6,
      markerHeight: 6,
      orient: "auto-start-reverse",
    });
    marker.append(
      svgElement("path", {
        d: "M 0 0 L 10 5 L 0 10 z",
        class: "arrow-" + status,
      }),
    );
    defs.append(marker);
  }
  svg.append(defs);
  const nodes = new Map(group.nodes.map((node) => [node.id, node]));
  const observed = job?.flow?.nodes || {};
  const traversed = new Set(job?.flow?.edges || []);
  const port = (node, other) => {
    const cx = node.x + node.width / 2,
      cy = node.y + node.height / 2;
    const dx = other[0] - cx,
      dy = other[1] - cy;
    return Math.abs(dx) / node.width > Math.abs(dy) / node.height
      ? [cx + (Math.sign(dx) * node.width) / 2, cy]
      : [cx, cy + (Math.sign(dy) * node.height) / 2];
  };
  for (const edge of group.edges) {
    const from = nodes.get(edge.source),
      to = nodes.get(edge.target);
    const start = port(
      from,
      edge.via?.[0] || [to.x + to.width / 2, to.y + to.height / 2],
    );
    const end = port(
      to,
      edge.via?.at(-1) || [from.x + from.width / 2, from.y + from.height / 2],
    );
    const status = traversed.has(edge.id)
      ? flowState(observed[edge.target] || { status: "completed" })
      : "idle";
    let path;
    if (edge.via)
      path =
        "M " +
        [start, ...edge.via, end].map((point) => point.join(" ")).join(" L ");
    else if (start[1] === end[1]) path = `M ${start} L ${end}`;
    else {
      const middle = (start[1] + end[1]) / 2;
      path = `M ${start} C ${start[0]} ${middle}, ${end[0]} ${middle}, ${end}`;
    }
    svg.append(
      svgElement("path", {
        d: path,
        class: "flow-edge edge-" + status,
        "marker-end": `url(#arrow-${group.id}-${status})`,
        "data-flow-edge": edge.id,
      }),
    );
    if (edge.label) {
      const midpoint = edge.via?.[0] || [
        (start[0] + end[0]) / 2,
        (start[1] + end[1]) / 2,
      ];
      const text = svgElement("text", {
        x: Math.max(95, Math.min(group.width - 95, midpoint[0])),
        y: midpoint[1] - 8,
        class: "flow-edge-label",
        "text-anchor": "middle",
      });
      text.textContent = edge.label;
      svg.append(text);
    }
  }
  for (const node of group.nodes) {
    const value = observed[node.id],
      status = flowState(value);
    const wrapper = svgElement("g", {
      class: "flow-node node-" + status,
      "data-flow-node": node.id,
      "data-flow-state": status,
    });
    const title = svgElement("title");
    title.textContent =
      node.label +
      " · " +
      node.detail +
      (value?.tools?.length ? " · 실행: " + value.tools.join(", ") : "");
    wrapper.append(title);
    if (node.kind === "decision")
      wrapper.append(
        svgElement("polygon", {
          points: `${node.x + node.width / 2},${node.y} ${node.x + node.width},${node.y + node.height / 2} ${node.x + node.width / 2},${node.y + node.height} ${node.x},${node.y + node.height / 2}`,
          class: "flow-shape",
        }),
      );
    else
      wrapper.append(
        svgElement("rect", {
          x: node.x,
          y: node.y,
          width: node.width,
          height: node.height,
          rx: node.kind === "human" ? 25 : 10,
          class: "flow-shape",
        }),
      );
    const foreign = svgElement("foreignObject", {
      x: node.x,
      y: node.y,
      width: node.width,
      height: node.height,
    });
    const content = el(
      "div",
      undefined,
      "flow-node-content" +
        (node.kind === "decision" ? " diamond-content" : ""),
    );
    content.append(el("strong", node.label));
    if (node.kind !== "decision")
      content.append(
        el(
          "code",
          node.detail +
            (node.kind === "model" && value && job?.model
              ? " · " + job.model
              : ""),
        ),
      );
    let metric;
    if (!value)
      metric = !job
        ? "실행 대기"
        : job.flow?.route_recorded
          ? "이번 작업 미실행"
          : "당시 실행 기록 없음";
    else if (node.kind === "model")
      metric =
        value.usage?.total_tokens !== undefined
          ? tokens(value.usage.total_tokens) + " 토큰"
          : value.status === "running"
            ? "호출 중 · 집계 대기"
            : job.trace.some((event) => event.usage_status === "not_recorded")
              ? "호출 확인 · 당시 토큰 미수집"
              : "사용량 응답 없음";
    else if (node.kind === "human")
      metric =
        value.status === "waiting" ? "사용자 확인 대기" : "요청·진입 확인";
    else
      metric =
        value.status === "waiting"
          ? "추가 자료·확정 대기"
          : "0 토큰 · " + executionStatus(value.status);
    content.append(el("span", metric, "flow-node-metric"));
    if (node.kind === "model" && value?.usage?.total_tokens !== undefined)
      content.append(
        el(
          "small",
          "입력 " +
            tokens(value.usage.input_tokens) +
            " · 출력 " +
            tokens(value.usage.output_tokens) +
            " · " +
            value.calls +
            "회" +
            (value.unknown_calls ? " / 일부 사용량 미확인" : ""),
        ),
      );
    foreign.append(content);
    wrapper.append(foreign);
    svg.append(wrapper);
  }
  scroll.append(svg);
  requestAnimationFrame(() => {
    if (scroll.isConnected)
      scroll.scrollLeft = state.flowScrolls.get(scrollKey) || 0;
  });
  section.append(scroll);
  return section;
}
function executionJob(job, initiallyOpen) {
  const box = disclosure("execution:" + job.id, "execution-job", initiallyOpen);
  const heading = el("summary");
  heading.append(
    el("strong", job.title),
    el("span", executionStatus(job.status), "badge"),
  );
  if (job.excluded)
    heading.append(badge("제외한 자료 · 사용량 포함", "warning"));
  else if (job.is_current) heading.append(badge("현재 처리", "active"));
  else if (job.kind === "document") heading.append(badge("이전 처리"));
  heading.append(
    el(
      "span",
      usageText(job.usage, job.kind === "business" && !job.usage.total_tokens),
      "execution-job-total",
    ),
  );
  box.append(heading);
  box.append(
    el("p", job.id + (job.model ? " · " + job.model : ""), "execution-caption"),
  );
  if (job.trace_order === "not_recorded" && job.trace.length)
    box.append(
      el(
        "p",
        "과거 실행은 호출 시각·교차 순서를 수집하지 않았습니다. 아래 목록은 호출된 구성요소를 보여주며 시간순 목록이 아닙니다. ‘당시 미수집’은 사용량을 단계별로 저장하지 않았다는 뜻입니다.",
        "source-note",
      ),
    );
  const table = el("table", undefined, "execution-table");
  const thead = el("thead"),
    header = el("tr");
  for (const text of [
    "실행 단계 · 호출 도구",
    "상태",
    "입력",
    "출력",
    "총 토큰",
    "소요 시간",
  ])
    header.append(el("th", text));
  thead.append(header);
  table.append(thead);
  const body = el("tbody");
  for (const [index, event] of job.trace.entries()) {
    const row = el("tr", undefined, "trace-" + event.kind);
    const title = el("td", undefined, event.parent_id ? "trace-child" : "");
    const local = event.kind !== "agent";
    title.append(
      el(
        "small",
        (event.started_at
          ? new Date(event.started_at).toLocaleTimeString("ko-KR", {
              hour12: false,
            })
          : "호출 시각 저장값 없음") +
          " · " +
          (local ? "도구·코드" : "에이전트"),
      ),
      el(
        "code",
        (event.parent_id
          ? "↳ "
          : job.trace_order === "chronological"
            ? index + 1 + ". "
            : "") + event.name,
      ),
    );
    const context = event.context || {};
    const scope = context.pages?.length
      ? context.pages.join(", ") + "쪽"
      : context.page
        ? context.page + "쪽"
        : "";
    if (scope)
      title.append(
        el(
          "small",
          scope +
            (context.target_field_count
              ? " · 대상 " + context.target_field_count + "개"
              : ""),
        ),
      );
    if (event.model) title.append(el("small", event.model));
    const status = el("td", executionStatus(event.status));
    if (event.usage_status === "unavailable")
      status.append(el("small", "사용량 응답 없음"));
    if (event.usage_status === "not_sent")
      status.append(el("small", "API 전송 전 종료"));
    row.append(title, status);
    for (const key of ["input_tokens", "output_tokens", "total_tokens"]) {
      row.append(
        el(
          "td",
          local
            ? "0"
            : event.usage_status === "pending"
              ? "집계 중"
              : event.usage_status === "not_recorded"
                ? "당시 미수집"
                : event.usage_status === "unavailable"
                  ? "응답 없음"
                  : tokens(event.usage?.[key]),
        ),
      );
    }
    row.append(
      el(
        "td",
        event.duration_ms === undefined
          ? "—"
          : event.duration_ms < 1000
            ? event.duration_ms + " ms"
            : (event.duration_ms / 1000).toFixed(2) + " s",
      ),
    );
    body.append(row);
  }
  if (!job.trace.length) {
    const row = el("tr"),
      cell = el("td", "호출 기록이 아직 없습니다.");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
  }
  table.append(body);
  const scroll = el("div", undefined, "execution-table-scroll");
  scroll.append(table);
  box.append(scroll);
  return box;
}

document.addEventListener("click", (event) => {
  const a = event.target.closest("a");
  if (
    a?.getAttribute("href")?.startsWith("/review") &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.shiftKey &&
    !a.target
  ) {
    event.preventDefault();
    safe(() => navigate(a.getAttribute("href")));
  }
});
window.addEventListener("popstate", () => safe(loadRoute));
window.addEventListener("beforeunload", (event) => {
  if (state.pending.size || state.uploading.length) {
    event.preventDefault();
    event.returnValue = "";
  }
});
try {
  const saved = JSON.parse(
    sessionStorage.getItem("review-disclosures-v1") || "[]",
  );
  state.disclosures = new Map(
    saved.filter(
      ([key, value]) => typeof key === "string" && typeof value === "boolean",
    ),
  );
} catch {
  /* Ignore invalid or unavailable UI preference storage. */
}
try {
  const filter = sessionStorage.getItem("review-filter-v1");
  if (reviewFilters.some(([key]) => key === filter))
    state.reviewFilter = filter;
} catch {
  /* Default to showing every card. */
}
window.reviewChatHost = {state, api, ensureReview, enabled: true};
safe(loadRoute);
