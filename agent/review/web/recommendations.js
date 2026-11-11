"use strict";

(() => {
  const choices = new Map();
  const acknowledged = new Set();
  const critical = (field) => field?.assessment?.kind === "conflict" && !field.assessment.resolved;
  const analysis = () => state.review?.recommendation_review;
  function fieldFor(target) {
    return target.startsWith("basic:") ? state.review.basic[target.slice(6)] : findField(target);
  }
  function sourceLink(source) {
    return link(source.filename + (source.page ? " · " + source.page + "쪽" : "") + " ↗",
      "/review/" + state.review.id + "/verify?document=" + encodeURIComponent(source.document_id) + "&field=" + encodeURIComponent(source.field_id), "recommendation-source");
  }
  function eligible(proposal) {
    const field = fieldFor(proposal.target_id);
    return field && !state.pending.has(proposal.target_id) && !field.touched && !field.confirmed && field.review_state !== "confirmed" && !critical(field)
      && field.ai_recommendation?.id !== proposal.id;
  }
  function controls(scope = "basic") {
    const result = analysis();
    if (!result) return el("div");
    const targets = scope === "basic" ? new Set(Object.keys(state.review.basic).map(k => "basic:" + k))
      : new Set(state.review.documents.flatMap(d => d.parts.filter(p => p.id === scope).flatMap(p => p.fields.map(f => f.id))));
    const proposals = result.proposals.filter(p => targets.has(p.target_id) && eligible(p));
    const box = el("div", undefined, "recommendation-controls");
    const actions = el("div", undefined, "recommendation-actions");
    const apply = button(result.active_job ? "추천 검토 중…" : "✦ AI가 채우기" + (proposals.length ? " · " + proposals.length + "개" : ""), async () => {
      const current = proposals.filter(eligible);
      if (!current.length) return toast("직접 수정 중인 값은 AI가 덮어쓰지 않습니다.");
      await mutate("recommendations/apply", {recommendation_ids: current.map(p => p.id)});
      toast("AI 추천을 임시로 채웠습니다. 원문 확인 후 확정해 주세요.");
    }, "btn small ai-fill");
    apply.disabled = Boolean(result.active_job) || !proposals.length;
    const refresh = button("추천 다시 검토", () => enqueue(async () => {
      await api("/api/reviews/" + state.review.id + "/recommendations", "POST", {revision: state.review.revision});
      state.review = await api("/api/reviews/" + state.review.id);
      render();
      poll();
    }), "btn small");
    refresh.disabled = Boolean(result.active_job);
    actions.append(apply, refresh);
    box.append(actions, el("small", result.risk_text, "recommendation-risk"));
    if (result.stale) box.append(el("small", "자료가 바뀌어 이전 추천을 만료했습니다. 다시 검토해 주세요.", "warning-text"));
    if (result.warning) box.append(el("small", result.warning, "warning-text"));
    return box;
  }
  function proposalCard(target) {
    const result = analysis();
    const field = fieldFor(target);
    const proposals = result?.proposals.filter(p => p.target_id === target) || [];
    if (!proposals.length && !field?.ai_recommendation) return null;
    const box = el("div", undefined, "recommendation-card");
    const applied = field.ai_recommendation;
    if (applied) box.append(el("strong", field.confirmed || field.review_state === "confirmed" ? "AI 추천 후 사용자 확인" : "AI 임시 추천 · 확인 필요"));
    if (field.recommendation_stale) box.append(el("small", "근거가 변경됐습니다. 추천을 다시 검토·적용한 뒤 확정해 주세요.", "critical-text"));
    for (const proposal of proposals) {
      box.append(el("div", "추천값: " + proposal.value + (proposal.unit.startsWith("KRW") ? "원" : ""), "recommendation-value"), el("p", proposal.reason));
      const refs = el("div", undefined, "recommendation-sources");
      for (const sid of proposal.source_ids) if (result.sources[sid]) refs.append(sourceLink(result.sources[sid]));
      box.append(refs);
      const apply = button("이 추천 적용", () => eligible(proposal) ? mutate("recommendations/apply", {recommendation_ids: [proposal.id]}) : toast("직접 수정 중인 값은 AI가 덮어쓰지 않습니다."), "btn small ai-fill");
      apply.disabled = !eligible(proposal) || Boolean(result.active_job);
      box.append(apply);
    }
    if (!proposals.length && applied) box.append(el("p", applied.reason));
    return box;
  }
  function queue() {
    const result = analysis();
    const conflicts = result?.conflicts.filter(c => !c.resolved) || [];
    if (!conflicts.length) return null;
    const box = el("section", undefined, "critical-review card");
    box.id = "critical-review";
    box.setAttribute("aria-label", "필수 확인 항목");
    box.append(el("h2", "⚠ 필수 확인 · " + conflicts.length + "개"), el("p", "원문 후보가 실제로 다릅니다. 사용할 기준값 하나를 선택하고 원문을 확인한 뒤 개별 확정해 주세요. 원문별 값은 이력에 보존됩니다."));
    for (const conflict of conflicts) {
      const card = el("article", undefined, "critical-conflict");
      card.dataset.conflictId = conflict.conflict_id;
      const target = conflict.target_id;
      const field = fieldFor(target);
      const title = target.startsWith("basic:") ? basicLabels[target.slice(6)] : conflict.label;
      card.append(el("h3", title + (conflict.filename ? " · " + conflict.filename : " · 기본 정보")), el("p", conflict.reason));
      const key = state.review.id + ":" + conflict.conflict_id;
      const options = el("div", undefined, "critical-options");
      const sources = conflict.source_ids.map(sid => result.sources[sid]).filter(Boolean);
      for (const source of sources) {
        const row = el("label", undefined, "critical-option");
        const radio = el("input");
        radio.type = "radio";
        radio.name = key;
        radio.checked = choices.get(key)?.source_id === source.id;
        radio.addEventListener("change", () => {choices.set(key, {value: source.value, source_id: source.id, unit: source.unit}); acknowledged.delete(key); render(true);});
        const text = el("span");
        text.append(el("strong", source.raw_value), el("small", source.filename + (source.page ? " · " + source.page + "쪽" : "")));
        row.append(radio, text, sourceLink(source));
        options.append(row);
      }
      if (field?.touched && field.value) {
        const row = el("label", undefined, "critical-option");
        const radio = el("input");
        radio.type = "radio";
        radio.name = key;
        radio.checked = choices.get(key)?.source_id === "manual";
        radio.addEventListener("change", () => {choices.set(key, {value: field.value, source_id: "manual", unit: field.unit || conflict.unit}); acknowledged.delete(key); render(true);});
        row.append(radio, el("span", "직접 입력한 값: " + field.value));
        options.append(row);
      }
      card.append(options);
      const selected = choices.get(key);
      const value = el("input");
      value.type = "text";
      value.value = selected?.value || "";
      value.readOnly = true;
      value.placeholder = "사용할 기준값을 선택해 주세요";
      value.setAttribute("aria-label", title + " 기준값");
      card.append(value);
      const check = el("input");
      check.type = "checkbox";
      check.checked = acknowledged.has(key);
      const confirm = button("원문 확인 후 이 값 개별 확정", async () => {
        if (!choices.has(key) || !acknowledged.has(key)) return;
        const choice = choices.get(key);
        if (target.startsWith("basic:")) {
          await mutate("", {basic: {[target.slice(6)]: choice.value}, confirm_basic_field: target.slice(6), conflict_acknowledgement: conflict.conflict_id}, "PUT");
        } else {
          const ownCandidate = field.candidates.find(c => c.id === choice.source_id);
          await mutate("fields", ownCandidate ? {field_id: target, candidate_id: ownCandidate.id} : {field_id: target, value: choice.value, unit: choice.unit}, "PUT");
          await mutate("confirm", {field_ids: [target], conflict_acknowledgement: conflict.conflict_id});
        }
        choices.delete(key); acknowledged.delete(key);
        toast("원문 확인과 기준값 선택을 기록했습니다.");
      }, "btn small critical-confirm");
      confirm.disabled = !selected || !check.checked;
      check.addEventListener("change", () => {if (check.checked) acknowledged.add(key); else acknowledged.delete(key); confirm.disabled = !choices.has(key) || !check.checked;});
      const label = el("label", undefined, "critical-ack");
      label.append(check, el("span", "원문과 선택한 기준값을 확인했습니다."));
      card.append(label, confirm);
      box.append(card);
    }
    return box;
  }
  window.reviewRecommendationUI = {controls, proposalCard, queue, critical};
})();
