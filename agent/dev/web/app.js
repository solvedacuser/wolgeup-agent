"use strict";
const $ = id => document.getElementById(id);
const labels = {pay_type:"임금 형태",contract_rate:"임금 금액",contract_start:"계약 시작일",contract_end:"계약 종료일",work_start:"출근 시각",work_end:"퇴근 시각",break_minutes:"휴게 시간 (분)",scheduled_daily_minutes:"일 소정근로시간 (분)",scheduled_weekly_minutes:"주 소정근로시간 (분)",payday:"지급일"};
const manualLabels = {slip_rate:"명세서 단가 (선택 단위)",worked_minutes:"근태 시간 (분)",slip_minutes:"명세서 시간 (분)",gross:"명세서 총지급액 (원)",deduction:"명세서 총공제액 (원)",net:"명세서 실수령액 (원)",deposit:"연결된 입금액 (원)",earning_item:"지급 항목 합계 (원)",deduction_item:"공제 항목 합계 (원)"};
const kinds = {json:"JSON 재구성",blank_form:"빈 표준 양식",synthetic:"가상 작성본",upload:"내 PDF"};
const statuses = {match:"일치",difference:"차이 발견",insufficient:"정보 부족",needs_review:"검토 필요"};
const ruleLabels = {"R-01":"계약 단가 ↔ 명세서 단가","R-02":"근태 시간 ↔ 명세서 시간","R-03":"지급 항목 ↔ 총지급액","R-04":"공제 항목 ↔ 총공제액","R-05":"계산 실수령액 ↔ 명세서","R-06":"명세서 실수령액 ↔ 입금액","R-07":"법정 임금 규칙 검토"};
const comparisonSides = {"R-01":["명세서 단가","계약서 단가"],"R-02":["명세서 시간","근무기록 시간"],"R-03":["확인한 지급 항목 합계","명세서 총지급액"],"R-04":["확인한 공제 항목 합계","명세서 총공제액"],"R-05":["총지급액 − 총공제액","명세서 실수령액"],"R-06":["명세서 실수령액","선택·배분한 입금액"]};
const stageLabels = {extract:"추출",analyze:"대조",explain:"설명",report:"보고서"};
const sourceConversions = new Set(["SOURCE_UNIT_FROM_HEADER","SOURCE_TEXT_SEPARATOR_NORMALIZED"]);
function reviewIssues(field) { return (field.issues||[]).filter(issue=>!sourceConversions.has(issue)); }
let docs = [], profiles = {}, current = null, currentJob = null, pollTimer, draftTimer, pendingDraft = false, changingType = false, saveChain = Promise.resolve(), evaluation;
let comparisonSources = null, comparisonSourcesRequest = null, loadingMaterials = false;
let materialSelectors = {}, materialInputs = {earnings:new Map(),deductions:new Map(),transactions:new Map()};
let distinctTransactionsInput = null;
function node(tag, text, className) { const el=document.createElement(tag); if(text!==undefined) el.textContent=text; if(className) el.className=className; return el; }
function notice(message, error=false) { $("notice").textContent=message; $("notice").className="notice"+(error?" error":""); $("notice").hidden=!message; }
async function api(path, method="GET", body) {
  const headers={"X-Local-Agent":"1"};
  if(body!==undefined) headers["Content-Type"]="application/json";
  const response=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)});
  const result=await response.json(); if(!response.ok) throw new Error(result.error||"요청을 처리하지 못했습니다."); return result;
}
function safeAction(fn) { return async event => { try { await fn(event); } catch(error) { notice(error.message,true); } }; }
function busy() { return currentJob && ["queued","running"].includes(currentJob.status); }
function docForCase() { return docs.find(d=>d.id===current?.document_id); }
function rememberCase() { localStorage.setItem("agent-last-case",current.id);localStorage.setItem("agent-case-"+current.document_id,current.id);history.replaceState(null,"","/?case="+current.id); }
function number(value) { return value===null||value===undefined||String(value).trim()===""?"미확인":Number.isFinite(Number(value))?Number(value).toLocaleString("ko-KR"):String(value); }
function buildFields() {
  for(const [key,label] of Object.entries(labels)) {
    const wrap=node("label",undefined,"field"); wrap.htmlFor="value-"+key; wrap.append(node("span",label));
    const input=node("input"); input.id="value-"+key; input.placeholder="미확인";
    input.type=key.startsWith("contract_")&&["contract_start","contract_end"].includes(key)?"date":"text";
    if(key==="contract_rate"||key.includes("minutes")) input.inputMode="decimal";
    wrap.append(input); const meta=node("div",undefined,"field-meta"); meta.id="meta-"+key; wrap.append(meta); $("contract-fields").append(wrap);
  }
  for(const [key,label] of Object.entries(manualLabels)) {
    const wrap=node("label",label,"field"); wrap.htmlFor="manual-"+key;
    const input=node("input"); input.id="manual-"+key; input.placeholder="미확인"; input.inputMode="decimal";
    wrap.append(input); $("manual-fields").append(wrap);
  }
}
async function loadDocuments() { docs=(await api("/api/documents")).documents; $("document-count").textContent=docs.length; renderDocuments(); }
function renderDocuments() {
  const filter=$("filter").value; $("documents").replaceChildren();
  for(const doc of docs.filter(d=>filter==="all"||d.kind===filter)) {
    const button=node("button",undefined,"document-card"+(current?.document_id===doc.id?" selected":""));
    button.dataset.documentId=doc.id; button.setAttribute("aria-pressed",String(current?.document_id===doc.id));
    const meta=node("div",undefined,"doc-meta"); const state=node("span"); state.append(node("i",undefined,"doc-dot"+(doc.evaluation_status==="failed"?" failed":"")),node("span",kinds[doc.kind]));
    meta.append(state,node("span",doc.pages+"p")); button.append(meta,node("strong",doc.title));
    button.addEventListener("click",safeAction(()=>selectDocument(doc.id))); $("documents").append(button);
  }
}
async function selectDocument(docId) {
  await flushDraft(); clearTimeout(pollTimer); currentJob=null;
  const stored=localStorage.getItem("agent-case-"+docId);
  if(stored) { try { current=await api("/api/cases/"+stored); } catch { current=null; } }
  else current=null;
  if(!current) current=await api("/api/cases","POST",{document_id:docId});
  rememberCase();
  notice(""); renderCase();
  await restoreCurrentJob();
}
async function restoreCurrentJob() {
  currentJob=null;
  if(current.jobs.length){
    const job=await api("/api/jobs/"+current.jobs.at(-1));
    const obsoleteExtraction=job.stage==="extract"&&current.materials?.sources?.length&&!["queued","running"].includes(job.status);
    if(!obsoleteExtraction&&(job.document_type||"contract")===(current.document_type||"contract")&&job.revision>=Math.max(current.document_type_revision||1,current.materials_revision||0))currentJob=job;
  }
  renderJob();if(busy())pollJob();
}
function renderCase() {
  if(!current) return;
  const type=current.document_type||"contract", isContract=type==="contract", hasContract=isContract||Boolean(current.materials?.selection?.contract_case_id);
  $("document-type").value=type;
  $("extract").textContent=(profiles[type]?.label||"문서")+" 추출하기 ↗";
  $("saved-option").hidden=!isContract; $("model-call-note").hidden=isContract;
  $("model-call-note").textContent=type==="payslip_ocr"?"로컬 Python OCR로 읽습니다. 이 추출에는 AI 호출과 토큰 사용이 없습니다.":"추출하기를 누르면 선택한 유형으로 모델을 호출합니다.";
  $("contract-inputs").hidden=!hasContract;
  $("contract-period-confirmed").closest("label").hidden=!hasContract;
  if(!isContract) $("manual-section").open=true;
  const doc=docForCase(); renderDocuments();
  $("document-title").textContent=doc?.title||"업로드 PDF"; $("document-kind").textContent=kinds[doc?.kind]||"PDF";
  const url="/preview.html?document="+current.document_id;
  if($("pdf").dataset.documentId!==current.document_id) { $("pdf").src=url+"#page=1"; $("pdf").dataset.documentId=current.document_id; $("evidence-box").hidden=true; }
  $("pdf").hidden=false; $("pdf-empty").hidden=true; $("open-pdf").hidden=false; $("open-pdf").href=url; $("page-info").textContent=(doc?.pages||"—")+"페이지 · 원문 PDF";
  const extraction=current.extraction; const fields=new Map((extraction?.fields||[]).map(f=>[f.field_id,f]));
  const linkedContract=Boolean(current.materials?.selection?.contract_case_id);
  renderDocumentExtraction(extraction, type);
  for(const key of Object.keys(labels)) {
    $("value-"+key).value=current.draft.values[key]??"";
    const field=linkedContract?null:fields.get(key),meta=$("meta-"+key);meta.replaceChildren();meta.title="";
    const unit=linkedContract?(key==="contract_rate"?current.draft.contract_unit||current.materials.suggested?.contract_unit:profiles.contract?.fields[key]?.unit):field?.unit;
    meta.append(node("span",unit||(current.draft.values[key]?"원문 값":"미기입 / 미확인")));
    if(linkedContract){
      const refs=current.materials.field_evidence?.[key]||[];
      meta.append(materialEvidenceButtons(refs,current.materials.evidence_catalog));
    }else if(field?.evidence_refs?.length){
      const evidence=extraction.evidence_catalog.find(e=>e.evidence_id===field.evidence_refs[0]);
      if(evidence){const button=node("button","근거 "+evidence.page+"쪽 ↗","evidence-link");button.type="button";button.addEventListener("click",()=>showEvidence(evidence,current.document_id));meta.append(button);}
    }
    if(field?.issues?.length)meta.title=field.issues.join(" · ");
  }
  for(const key of Object.keys(manualLabels)) $("manual-"+key).value=current.draft.manual[key]??"";
  $("contract-unit").value=current.draft.contract_unit; $("period-start").value=current.draft.period_start; $("period-end").value=current.draft.period_end;
  $("time-kind").value=current.draft.time_kind;
  $("slip-unit").value=current.draft.slip_unit||"";
  $("contract-period-confirmed").checked=Boolean(current.draft.contract_period_confirmed);
  for(const [id,key] of [["earnings-complete","earnings_complete"],["deductions-complete","deductions_complete"],["payment-confirmed","payment_scope_confirmed"],["synthetic-auxiliary","synthetic_auxiliary"]]) $(id).checked=current.draft[key];
  $("auxiliary-label").hidden=doc?.kind!=="synthetic"||Boolean(current.materials?.sources?.length);
  $("extraction-note").textContent=extraction?(extraction.verification_method==="ocr_unverified"?"Python OCR 인식 결과입니다. OCR 원문과 원본 PDF를 대조하고 숫자를 확인해 주세요.":extraction.verification_method==="vision_unverified"?"이미지 판독 결과입니다. 인용문과 값을 원문에서 직접 확인해 주세요.":"텍스트 원문과 근거를 대조했습니다. 계산 전 값을 확인해 주세요."):"추출 후에도 사용자 확인이 필요합니다.";
  $("extraction-issues").replaceChildren();
  const issueCount=(extraction?.fields||[]).filter(f=>reviewIssues(f).length).length;
  if(issueCount) $("extraction-issues").append(node("p",issueCount+"개 항목에 확인할 이슈가 있습니다. 각 입력란의 원문 근거를 확인해 주세요.","issue"));
  if((extraction?.fields||[]).some(f=>(f.issues||[]).some(issue=>sourceConversions.has(issue)))) $("extraction-issues").append(node("p","표 머리글의 단위와 원문 표기를 대조해 값을 변환했습니다.","muted"));
  const issueLabels={OCR_REQUIRES_USER_CONFIRMATION:"OCR로 인식한 값은 원본과 대조해 확인해 주세요.",OCR_RULE_BASED_EXTRACTION:"항목은 Python 규칙으로 분류했습니다. 미분류 내용은 OCR 전체 원문에서 확인할 수 있습니다.",OCR_LAYOUT_REQUIRES_MANUAL_REVIEW:"자동으로 분류한 항목이 없습니다. OCR 전체 원문을 보고 직접 입력해 주세요."};
  for(const issue of extraction?.issues||[]) if(issue!=="VISION_REQUIRES_USER_CONFIRMATION") $("extraction-issues").append(node("p",issueLabels[issue]||issue,"issue"));
  if(current.draft.synthetic_auxiliary) $("extraction-issues").append(node("p","가상 보조자료가 연결되어 있습니다. 아래 비교값은 실제 자료가 아닙니다.","issue"));
  renderMaterials(); renderMaterialReview(); renderResults(); renderJob();
  if(comparisonSources===null&&!comparisonSourcesRequest) loadComparisonSources().catch(error=>notice("자료 목록을 불러오지 못했습니다. "+error.message,true));
}
function evidenceButton(field, extraction) {
  const evidence=extraction.evidence_catalog.find(e=>field.evidence_refs.includes(e.evidence_id));
  if(!evidence) return null;
  const button=node("button","근거 "+evidence.page+"쪽 ↗","evidence-link"); button.type="button";
  button.addEventListener("click",()=>showEvidence(evidence,current.document_id)); return button;
}
function extractedValue(field, definition, extraction) {
  const cell=node("div",undefined,"extracted-value");
  const numeric=["KRW","rate","minute","count"].includes(definition?.unit);
  cell.append(node("span",field.normalized_value===null?(field.raw_value===null?"미기입":"값 확인 필요"):numeric?number(field.normalized_value):field.normalized_value));
  if(field.raw_value!==null&&field.raw_value!==field.normalized_value) cell.append(node("small","원문: "+field.raw_value));
  const evidence=evidenceButton(field,extraction); if(evidence)cell.append(evidence);
  for(const issue of reviewIssues(field))cell.append(node("small",issue,"issue"));
  return cell;
}
function renderDocumentExtraction(extraction, type) {
  const container=$("document-extraction");container.replaceChildren();container.hidden=type==="contract";
  if(type==="contract")return;
  if(!extraction){container.append(node("p","문서 유형을 확인한 뒤 추출하기를 눌러 주세요.","muted"));return;}
  if(extraction.recognized_pages?.length){const details=node("details");details.open=true;details.append(node("summary","Python OCR 전체 원문"));extraction.recognized_pages.forEach((text,index)=>{details.append(node("h4",(index+1)+"페이지"),node("pre",text,"ocr-text"));});container.append(details);}
  const profile=profiles[type]||{fields:{},rows:{}}, summary=node("div",undefined,"extracted-summary");
  const groups=new Map(),extras=[];
  for(const field of extraction.fields) {
    const definition=profile.fields[field.field_id];
    if(definition){const item=node("div");item.append(node("strong",definition.label),extractedValue(field,definition,extraction));summary.append(item);continue;}
    const match=/^([a-z]+)_([1-9]\d{0,3})_([a-z_]+)$/.exec(field.field_id);
    if(match&&profile.rows[match[1]]?.fields[match[3]]){
      if(!groups.has(match[1]))groups.set(match[1],new Map());
      const rows=groups.get(match[1]);if(!rows.has(Number(match[2])))rows.set(Number(match[2]),new Map());rows.get(Number(match[2])).set(match[3],field);
    }else extras.push(field);
  }
  container.append(node("h3","문서 요약"),summary);
  for(const [prefix,definition] of Object.entries(profile.rows)) {
    const rows=groups.get(prefix)||new Map();container.append(node("h3",definition.label+" · "+rows.size+"행"));
    if(!rows.size){container.append(node("p","추출된 상세 행이 없습니다. 원문을 확인해 주세요.","muted"));continue;}
    const scroll=node("div",undefined,"table-scroll extracted-rows"),table=node("table"),head=node("tr");
    head.append(node("th","행"));for(const column of Object.values(definition.fields))head.append(node("th",column.label));table.append(head);
    for(const [index,fields] of [...rows].sort((a,b)=>a[0]-b[0])){
      const row=node("tr");row.append(node("td",String(index)));
      for(const [key,column] of Object.entries(definition.fields)){
        const td=node("td");td.append(fields.has(key)?extractedValue(fields.get(key),column,extraction):node("span","미기입"));row.append(td);
      }table.append(row);
    }scroll.append(table);container.append(scroll);
  }
  if(extras.length){const details=node("details");details.append(node("summary","추가 추출 항목 "+extras.length+"개"));for(const field of extras){details.append(node("strong",field.field_id),extractedValue(field,{},extraction));}container.append(details);}
  container.append(node("p","요약과 상세 행은 추출 후보입니다. 대조에 사용할 숫자는 아래 입력란에서 확인·수정하세요. 상세 행을 합산하거나 급여 거래로 자동 확정하지 않습니다.","muted"));
}
async function loadComparisonSources() {
  if(comparisonSourcesRequest)return comparisonSourcesRequest;
  comparisonSourcesRequest=(async()=>{comparisonSources=(await api("/api/comparison-sources")).sources;renderMaterials();updateControls();})();
  try{await comparisonSourcesRequest;}finally{comparisonSourcesRequest=null;}
}
function sourceTitle(source) {
  const period=source.period_start||source.period_end?" · "+(source.period_start||"미확인")+" ~ "+(source.period_end||"미확인"):"";
  return (source.title||source.case_id)+" · "+(profiles[source.document_type]?.label||source.document_type)+period+(!source.has_extraction?" · 추출 필요":"");
}
function renderMaterials() {
  if(!current)return;
  const target=$("materials-selectors"),selection=current.materials?.selection||{};
  target.replaceChildren();materialSelectors={};
  if(comparisonSources===null){target.append(node("p","추출한 자료 목록을 불러오는 중입니다.","muted"));return;}
  const definitions=[{key:"contract_case_id",label:"계약서",types:["contract"]},{key:"attendance_case_ids",label:"근무기록",types:["attendance"],multiple:true},{key:"payslip_case_id",label:"급여명세서",types:["payslip","payslip_ocr"]},{key:"deposit_case_ids",label:"입금기록",types:["deposit"],multiple:true}];
  for(const definition of definitions){
    const sources=comparisonSources.filter(source=>definition.types.includes(source.document_type));
    if(definition.multiple){
      const group=node("fieldset",undefined,"material-source-group"),legend=node("legend",definition.label+" · 여러 건 선택");group.append(legend);materialSelectors[definition.key]=[];
      if(!sources.length)group.append(node("p","등록된 자료가 없습니다.","muted"));
      for(const source of sources){const label=node("label",undefined,"checkbox"),input=node("input");input.type="checkbox";input.value=source.case_id;input.checked=(selection[definition.key]||[]).includes(source.case_id);input.disabled=!source.has_extraction||busy()||loadingMaterials;input.dataset.materialSource="1";input.addEventListener("change",materialSelectionChanged);label.append(input,node("span",sourceTitle(source)));group.append(label);materialSelectors[definition.key].push(input);}
      target.append(group);
    }else{
      const label=node("label",definition.label,"field"),select=node("select"),empty=node("option","연결 안 함");empty.value="";select.append(empty);select.setAttribute("aria-label",definition.label+" 자료 선택");
      for(const source of sources){const option=node("option",sourceTitle(source));option.value=source.case_id;option.disabled=!source.has_extraction;select.append(option);}
      select.value=selection[definition.key]||"";select.disabled=busy()||loadingMaterials;select.dataset.materialSource="1";select.addEventListener("change",materialSelectionChanged);materialSelectors[definition.key]=select;label.append(select);target.append(label);
    }
  }
  $("materials-state").textContent=current.materials_invalid?"연결한 원본이 변경되었습니다. 자료를 다시 선택하고 ‘선택 자료 불러오기’를 눌러 주세요.":current.materials?.sources?.length?"자료 "+current.materials.sources.length+"건 연결됨 · 자료·값 확인 → 입력값 확인 → 자료 대조 순서로 진행합니다.":"연결할 자료를 선택하세요. 모두 ‘연결 안 함’으로 불러오면 연결이 해제됩니다.";
  $("materials-warnings").replaceChildren();
  for(const warning of current.materials?.warnings||[])$("materials-warnings").append(node("p",warning,"issue"));
  if(current.materials?.sources?.length){const list=node("div",undefined,"linked-source-list");for(const source of current.materials.sources){const button=node("button",(profiles[source.document_type]?.label||"자료")+" · "+(source.title||source.case_id)+" 원문 ↗","evidence-link");button.type="button";button.addEventListener("click",()=>openSourceDocument(source.document_id,source.title));list.append(button);}target.append(list);}
}
function selectedMaterials() {
  return {contract_case_id:materialSelectors.contract_case_id?.value||null,attendance_case_ids:(materialSelectors.attendance_case_ids||[]).filter(input=>input.checked).map(input=>input.value),payslip_case_id:materialSelectors.payslip_case_id?.value||null,deposit_case_ids:(materialSelectors.deposit_case_ids||[]).filter(input=>input.checked).map(input=>input.value)};
}
function materialsSelectionPending() {
  if(!current||!materialSelectors.contract_case_id)return false;
  const selected=selectedMaterials(),saved=current.materials?.selection||{};
  return Object.keys(selected).some(key=>Array.isArray(selected[key])?JSON.stringify([...selected[key]].sort())!==JSON.stringify([...(saved[key]||[])].sort()):(selected[key]||null)!==(saved[key]||null));
}
function materialSelectionChanged() {
  renderResults();
  $("materials-state").textContent=materialsSelectionPending()?"파일 선택이 바뀌었습니다. ‘선택 자료 불러오기’를 눌러 적용한 뒤 값을 확인해 주세요.":"연결한 파일과 같은 선택입니다.";
  $("materials-state").className="muted"+(materialsSelectionPending()?" selection-pending":"");
}
async function applyMaterials() {
  if(!current||comparisonSources===null)return;
  const selection=selectedMaterials(),id=current.id;loadingMaterials=true;updateControls();
  try{if(current.materials_invalid){clearTimeout(draftTimer);pendingDraft=false;}else await flushDraft();const saved=await api("/api/cases/"+id+"/materials","PUT",selection);if(current?.id!==id)return;current=saved;currentJob=null;clearTimeout(pollTimer);renderCase();await loadComparisonSources();showConfirmation();notice(current.materials?.sources?.length?"선택한 자료를 불러왔습니다. 아래 값을 확인하고 ‘입력값 확인’을 누르면 자료 대조를 실행할 수 있습니다.":"자료 연결을 해제했습니다.");}
  finally{loadingMaterials=false;updateControls();}
}
function materialEvidenceButtons(refs, catalog) {
  const group=node("span",undefined,"material-evidence");
  for(const id of [...new Set(refs||[])]){const evidence=(catalog||[]).find(item=>item.evidence_id===id);if(!evidence)continue;const button=node("button",evidence.page?"근거 "+evidence.page+"쪽 ↗":"확인 근거 ↗","evidence-link");button.type="button";button.addEventListener("click",()=>showEvidence(evidence));group.append(button);}
  return group;
}
function renderMaterialReview() {
  const target=$("material-review"),materials=current.materials,review=current.draft.material_review||{};
  target.replaceChildren();materialInputs={earnings:new Map(),deductions:new Map(),transactions:new Map()};distinctTransactionsInput=null;
  target.hidden=!materials?.sources?.length;
  const hasPayslip=Boolean(materials?.selection?.payslip_case_id),hasDeposits=Boolean(materials?.selection?.deposit_case_ids?.length);
  for(const key of ["earning_item","deduction_item"])$("manual-"+key).closest("label").hidden=hasPayslip;
  $("manual-deposit").closest("label").hidden=hasDeposits;
  if(target.hidden)return;
  target.append(node("h3","연결한 자료의 상세 항목 확인"),node("p","원문의 금액을 확인하고 잘못 읽은 값은 수정하세요. 비어 있는 금액은 미확인으로 남습니다. 명세서 항목과 선택한 입금 행이 아래 수동 합계 입력보다 우선합니다.","muted"));
  for(const [key,label] of [["earnings","지급 항목"],["deductions","공제 항목"]]){
    if(!hasPayslip)continue;
    target.append(node("h4",label));const items=materials[key]||[];
    if(!items.length)target.append(node("p","추출한 "+label+"이 없습니다. 원문을 확인하고 누락된 내용이 있으면 자료를 수정해 다시 추출하세요.","issue"));
    for(const [index,item] of items.entries()){
      const row=node("div",undefined,"material-review-row"),labelNode=node("label",item.label||label+" "+(index+1),"field"),input=node("input");input.id="material-"+key+"-"+index;input.inputMode="decimal";input.placeholder="미확인 · 원문 확인 후 입력";input.value=Object.hasOwn(review[key]||{},item.item_id)?review[key][item.item_id]??"":item.amount??"";labelNode.htmlFor=input.id;labelNode.append(input);row.append(labelNode,materialEvidenceButtons(item.evidence_refs,materials.evidence_catalog));target.append(row);materialInputs[key].set(item.item_id,input);
    }
  }
  if(hasDeposits){
    target.append(node("h4","이 급여 기간에 배분할 입금"),node("p","급여에 해당하는 거래만 직접 선택하세요. 일부 금액만 해당하면 배분액을 수정하세요. 기본 상태에서는 어떤 거래도 급여로 확정하지 않습니다.","muted"));
    if(!materials.transactions?.length)target.append(node("p","추출한 입금 거래가 없습니다. 원문과 문서 유형을 확인해 주세요.","issue"));
    for(const [index,item] of (materials.transactions||[]).entries()){
      const row=node("div",undefined,"material-transaction"),checkLabel=node("label",undefined,"checkbox"),check=node("input"),principalLabel=node("label","원문 대조한 입금액 (원)","field"),principal=node("input"),amountLabel=node("label","이 기간에 배분할 금액 (원)","field"),amount=node("input"),saved=review.transactions?.[item.transaction_id];
      check.type="checkbox";check.checked=Boolean(saved?.selected);check.id="transaction-select-"+index;checkLabel.append(check,node("span",(item.date||"날짜 미확인")+" · "+(item.description||"내용 미확인")+" · 거래금액 "+number(item.transaction_amount??item.amount)+"원"));
      principal.inputMode="decimal";principal.placeholder="미확인 · 원문 확인 후 입력";principal.id="transaction-principal-"+index;principal.value=saved&&Object.hasOwn(saved,"transaction_amount")?saved.transaction_amount??"":item.transaction_amount??"";principalLabel.htmlFor=principal.id;principalLabel.append(principal);
      amount.inputMode="decimal";amount.placeholder="미확인 · 원문 확인 후 입력";amount.id="transaction-amount-"+index;amount.value=saved&&Object.hasOwn(saved,"amount")?saved.amount??"":item.amount??item.transaction_amount??"";amountLabel.htmlFor=amount.id;amountLabel.append(amount);row.append(checkLabel,principalLabel,amountLabel,materialEvidenceButtons(item.evidence_refs,materials.evidence_catalog));target.append(row);materialInputs.transactions.set(item.transaction_id,{selected:check,amount,transaction_amount:principal});
      if(item.duplicate_candidate_group)row.append(node("p","추출한 원문에서 중복 후보였던 거래입니다. 교정한 날짜·금액과 원문을 확인하세요.","issue"));
    }
    if(materials.transactions?.length>1||materials.transactions?.some(item=>item.duplicate_candidate_group)){
      const label=node("label",undefined,"checkbox"),input=node("input");input.type="checkbox";input.id="distinct-transactions-confirmed";input.checked=Boolean(review.distinct_transactions_confirmed);label.append(input,node("span","같은 날짜·금액의 중복 후보를 각각 별개의 거래로 확인했음"));distinctTransactionsInput=input;target.append(label,node("p","같은 중복 후보 그룹에서 한 건만 선택하면 이 확인은 필요하지 않습니다. 두 건 이상을 선택하려면 각각 다른 거래인지 확인해 주세요.","muted"));
    }
  }
  $("manual-section").open=true;
}
function readMaterialReview() {
  return {earnings:Object.fromEntries([...materialInputs.earnings].map(([id,input])=>[id,input.value.trim()||null])),deductions:Object.fromEntries([...materialInputs.deductions].map(([id,input])=>[id,input.value.trim()||null])),transactions:Object.fromEntries([...materialInputs.transactions].map(([id,inputs])=>[id,{selected:inputs.selected.checked,amount:inputs.amount.value.trim()||null,transaction_amount:inputs.transaction_amount.value.trim()||null}])),distinct_transactions_confirmed:Boolean(distinctTransactionsInput?.checked)};
}
function duplicateTransactionReviewNeeded() {
  if(distinctTransactionsInput?.checked)return false;
  const groups=new Map();
  for(const item of current?.materials?.transactions||[]){
    const inputs=materialInputs.transactions.get(item.transaction_id),principal=inputs?.transaction_amount.value.trim();
    if(!inputs?.selected.checked||!item.date||!/^\d{1,13}(?:\.\d{1,2})?$/.test(principal||""))continue;
    const key=item.date+"|"+Number(principal),entries=groups.get(key)||[];
    for(const other of entries){
      if(item.account_number&&other.account_number){
        if(item.account_number!==other.account_number)continue;
        if(item.reference&&other.reference&&item.reference!==other.reference)continue;
      }
      return true;
    }
    entries.push(item);groups.set(key,entries);
  }
  return false;
}
function inputsConfirmed() {return Boolean(current&&!current.dirty&&!pendingDraft&&!current.materials_invalid&&current.confirmed_revision!=null);}
function comparisonProgressMessage() {
  if(materialsSelectionPending())return "선택한 자료를 먼저 불러온 뒤 입력값을 확인해 주세요.";
  if(current?.materials_invalid)return "연결 원본이 변경되었습니다. 선택 자료를 다시 불러와 주세요.";
  if(inputsConfirmed())return "입력값 확인 완료 · ‘자료 대조’를 눌러 선택한 자료를 비교하세요.";
  return current?.materials?.sources?.length?"자료 "+current.materials.sources.length+"건 연결 · 값 확인 대기 · ‘자료·값 확인’에서 입력값을 확인해 주세요.":"비교할 자료를 불러오고 입력값을 확인하면 대조를 시작할 수 있습니다.";
}
function renderComparisonGuidance() {
  const target=$("comparison-guidance"),materials=current?.materials;
  target.replaceChildren();target.hidden=!materials?.sources?.length;
  if(!target.hidden){
    target.append(node("strong","선택한 자료 "+materials.sources.length+"건의 값을 확인하세요."));
    const messages=[],hasDeposits=Boolean(materials.selection?.deposit_case_ids?.length),hasPayslip=Boolean(materials.selection?.payslip_case_id),hasAttendance=Boolean(materials.selection?.attendance_case_ids?.length);
    if(!$("period-start").value||!$("period-end").value)messages.push("대조할 급여 기간의 시작일과 종료일을 입력해 주세요.");
    if(($("manual-worked_minutes").value||$("manual-slip_minutes").value)&&!$("time-kind").value)messages.push("시간 비교 기준을 실제 근로시간 또는 유급 인정시간으로 확인해 주세요.");
    if(hasAttendance&&hasDeposits&&!hasPayslip&&!$("manual-net").value)messages.push("근무기록과 입금기록으로 예상 급여를 자동 계산하는 기능은 아직 없습니다. 입금액과 비교할 급여명세서나 직접 확인한 실수령액이 필요합니다.");
    else if(hasDeposits&&!$("manual-net").value)messages.push("입금액과 비교할 실수령액이 없습니다. 급여명세서를 연결하거나 원문에서 확인한 실수령액을 입력해 주세요.");
    if(hasDeposits&&(![...materialInputs.transactions.values()].some(row=>row.selected.checked)||!$("payment-confirmed").checked))messages.push("이번 급여에 해당하는 입금 거래를 선택하고 ‘이 기간의 입금 연결 범위를 확인했음’을 확인해 주세요.");
    for(const message of messages)target.append(node("p",message));
    target.append(node("p",inputsConfirmed()?"입력값 확인이 끝났습니다. ‘대조 결과’에서 자료 대조를 실행하세요.":"기간과 값을 확인한 뒤 아래 ‘입력값 확인’을 누르세요. 값이 없는 비교 항목은 정보 부족으로 표시됩니다."));
  }
  if(!currentJob||currentJob.stage==="extract")$("job-status").textContent=comparisonProgressMessage();
}
function updateControls() {
  const running=busy()||changingType||loadingMaterials, invalid=Boolean(current?.materials_invalid), confirmed=inputsConfirmed();
  const selectionPending=materialsSelectionPending();
  const viewingHistory=Boolean(current?.analysis&&resultView().historical);
  const fresh=confirmed&&current.analysis&&!current.stale&&!selectionPending&&!viewingHistory;
  const duplicateReviewNeeded=duplicateTransactionReviewNeeded();
  $("extract").disabled=!current||running; $("confirm").disabled=!current||running||invalid||duplicateReviewNeeded||selectionPending;
  $("document-type").disabled=!current||running;
  $("analyze").disabled=!confirmed||running||selectionPending; $("explain").disabled=!fresh||running; $("report").disabled=!fresh||!current.explanation||running;
  $("confirmation-state").textContent=duplicateReviewNeeded?"중복 거래 후보 확인 필요":confirmed?"확인 완료 · v"+current.confirmed_revision:"사용자 확인 필요";
  $("confirmation-state").className="tag"+(confirmed?"":" warning");
  $("freshness").textContent=invalid?"연결 원본 변경 · 다시 불러오기 필요":current?.stale||pendingDraft?"입력 변경 · 갱신 필요":current?.analysis?"최신 확인값 결과":"결과 대기";
  $("freshness").className="tag"+(current?.stale||pendingDraft||invalid?" warning":"");
  $("load-materials").disabled=!current||running||comparisonSources===null;$("refresh-materials").disabled=!current||running;
  for(const [key,inputs] of Object.entries(materialSelectors)){for(const input of Array.isArray(inputs)?inputs:[inputs])input.disabled=running||(key.endsWith("_ids")&&!comparisonSources?.find(source=>source.case_id===input.value)?.has_extraction);}
  document.querySelectorAll("#confirmation-form input, #confirmation-form select").forEach(input=>{input.disabled=invalid;});
  document.querySelectorAll(".steps li").forEach((el,index)=>el.classList.toggle("active",index===(!current?.extraction?0:!confirmed?1:!current.analysis?2:3)));
  renderComparisonGuidance();
}
function openSourceDocument(documentId,title,page=1) {
  if(!documentId)return;
  const url="/preview.html?document="+encodeURIComponent(documentId);
  $("pdf").src=url+"#page="+page;$("pdf").dataset.documentId=documentId;$("pdf").hidden=false;$("pdf-empty").hidden=true;
  $("open-pdf").href=url;$("open-pdf").hidden=false;$("page-info").textContent=(title||docs.find(doc=>doc.id===documentId)?.title||"원문 PDF")+" · "+page+"페이지";$("evidence-box").hidden=true;
}
function showEvidence(evidence,documentId) {
  const source=current.materials?.sources?.find(item=>item.ref?.source_id===evidence.source_id);
  const resolvedId=documentId||source?.document_id||(evidence.source_id===current.document_id?current.document_id:null);
  if(resolvedId&&evidence.page)openSourceDocument(resolvedId,source?.title,evidence.page);
  $("evidence-title").textContent="원문 근거"+(evidence.page?" · "+evidence.page+"페이지":" · 사용자 확인값"); $("evidence-quote").textContent=evidence.quote||"별도의 원문 인용이 없는 확인값입니다.";
  $("evidence-method").textContent=evidence.verification_method==="manual_confirmed"?"사용자가 직접 확인한 값":evidence.verification_method==="ocr_unverified"?"근거: 로컬 Tesseract OCR 인식문 · 원본 일치 미검증 · 사용자 확인 필요":evidence.verification_method==="text_match"?"검증 방법: PDF 텍스트와 인용문 일치":"검증 방법: 이미지 판독 · 독립 OCR 검증 없음 · 사용자 확인 필요";
  if(!resolvedId&&evidence.page)$("evidence-method").textContent+=" · 원본 연결 정보가 없어 인용문만 표시합니다.";
  $("evidence-box").hidden=false;
}
function readForm() {
  return {values:Object.fromEntries(Object.keys(labels).map(k=>[k,$("value-"+k).value.trim()||null])),
    contract_unit:$("contract-unit").value,slip_unit:$("slip-unit").value,contract_period_confirmed:$("contract-period-confirmed").checked,period_start:$("period-start").value,period_end:$("period-end").value,
    manual:Object.fromEntries(Object.keys(manualLabels).map(k=>[k,$("manual-"+k).value.trim()||null])),time_kind:$("time-kind").value,
    earnings_complete:$("earnings-complete").checked,deductions_complete:$("deductions-complete").checked,payment_scope_confirmed:$("payment-confirmed").checked,synthetic_auxiliary:$("synthetic-auxiliary").checked,material_review:readMaterialReview()};
}
function changed() { if(!current)return; pendingDraft=true; current.dirty=true; current.stale=Boolean(current.analysis); updateControls(); renderResults(); clearTimeout(draftTimer); draftTimer=setTimeout(()=>flushDraft().catch(e=>notice(e.message,true)),400); }
async function flushDraft() {
  clearTimeout(draftTimer); if(!current||!pendingDraft) return saveChain;
  const id=current.id, draft=readForm(); pendingDraft=false;
  saveChain=saveChain.catch(()=>{}).then(async()=>{const saved=await api("/api/cases/"+id+"/draft","PUT",draft); if(current?.id===id){current=saved;updateControls();renderResults();}});
  try {await saveChain;} catch(error) {pendingDraft=true;updateControls();throw error;}
}
async function start(stage) {
  await flushDraft(); notice("");
  if(stage==="analyze")switchSection("results");
  currentJob=await api("/api/cases/"+current.id+"/jobs","POST",{stage,use_saved:current.document_type==="contract"&&$("use-saved").checked,include_original:$("include-original").checked});
  renderJob(); pollJob();
}
function renderJob() {
  const extraction=currentJob?.stage==="extract",resumable=currentJob&&["failed","cancelled","interrupted"].includes(currentJob.status);
  $("extraction-job-status").hidden=!extraction;
  for(const [prefix,visible] of [["",!extraction],["extraction-",extraction]]){$(prefix+"cancel").hidden=!(visible&&busy());$(prefix+"resume").hidden=!(visible&&resumable);}
  if(!currentJob){$("job-status").textContent=comparisonProgressMessage();$("extraction-job-status").textContent="";return updateControls();}
  const statusLabels={queued:"접수됨",running:"실행 중",completed:"완료",failed:"실패",cancelled:"취소됨",interrupted:"중단됨",superseded:"이전 입력 결과 보류"};
  const last=currentJob.events?.at(-1);
  $(extraction?"extraction-job-status":"job-status").textContent=(stageLabels[currentJob.stage]||"")+" · "+(statusLabels[currentJob.status]||currentJob.status)+(last?" / "+currentJob.events.length+"개 단계 진행":"")+(currentJob.cache_source?" · "+currentJob.cache_source:"")+(currentJob.error?" · "+currentJob.error+(currentJob.error_code?" ("+currentJob.error_code+")":""):"")+(currentJob.usage?.total_tokens?" · 누적 "+number(currentJob.usage.total_tokens)+" 토큰":"");
  updateControls();
}
async function pollJob() {
  clearTimeout(pollTimer); if(!currentJob) return;
  const id=currentJob.id;
  try {
    const job=await api("/api/jobs/"+id); if(currentJob?.id!==id)return; currentJob=job;renderJob();
    if(busy()) pollTimer=setTimeout(pollJob,800);
    else {await flushDraft();current=await api("/api/cases/"+current.id);renderCase();renderJob();if(job.stage==="extract"&&job.status==="completed")await loadComparisonSources();}
  } catch(error) {notice("상태 조회가 중단됐습니다. 새로고침하면 저장한 작업을 다시 확인합니다. "+error.message,true);}
}
function renderResults() { renderComparisonResults(); updateControls(); }

async function loadEvaluation() {
  evaluation=await api("/api/evaluation");const total=evaluation.count||0; $("evaluation-count").textContent=(evaluation.evaluated||0)+"/"+total;
  $("evaluation-summary").replaceChildren();
  const data=[["실행 완료",(evaluation.completed||0)+" / "+total,"검증 거절·실패 "+(evaluation.failed||0)+"건"],
    ["명시값 추출",(evaluation.positive_values?.correct||0)+" / "+(evaluation.positive_values?.total||0),"판단 보류 "+(evaluation.unscored||0)+"항목 제외"],
    ["빈칸 오인식",(evaluation.blanks?.hallucinated||0)+"건","완료 문서의 빈칸 "+(evaluation.blanks?.total||0)+"항목"],
    ["실제 사용량",number(evaluation.usage?.total_tokens||0),"토큰 · 누적 "+(evaluation.model_calls||0)+"회 호출"]];
  for(const [label,value,detail] of data){const el=node("div",undefined,"metric");el.append(node("span",label),node("strong",value),node("small",detail));$("evaluation-summary").append(el);}
  $("evaluation-rows").replaceChildren();
  for(const record of evaluation.records||[]) {const row=node("tr"); const positive=record.fields.filter(f=>f.gold_state==="present"), blank=record.fields.filter(f=>f.blank_hallucination!==null); row.append(node("td",record.document_id+" · "+record.title),node("td",kinds[record.kind]),node("td",record.status==="completed"?"완료":"검증 거절"),node("td",positive.filter(f=>f.exact).length+" / "+positive.length),node("td",blank.filter(f=>f.blank_hallucination).length+" / "+blank.length));const cell=node("td"),button=node("button","정답 비교");button.addEventListener("click",()=>evaluationDetail(record));cell.append(button);row.append(cell);$("evaluation-rows").append(row);}
}
function evaluationDetail(record) {
  const target=$("evaluation-detail");target.replaceChildren();target.hidden=false;
  const heading=node("div",undefined,"evaluation-detail-heading"); heading.append(node("h3",record.document_id+" · 정답과 추출값"));if(record.error)heading.append(node("p","실패 코드: "+record.error.code));
  const open=node("button","이 PDF로 테스트하기 ↗");open.addEventListener("click",safeAction(async()=>{switchTab(false);await selectDocument(record.document_id);}));heading.append(open);target.append(heading);
  const scroll=node("div",undefined,"table-scroll"),table=node("table"),head=node("tr");for(const title of ["항목","정답 상태","정답","모델 결과","근거 / 판정"])head.append(node("th",title));table.append(head);
  for(const field of record.fields) {const row=node("tr");const state={present:"명시된 값",absent:"미기입",unscored:"판단 보류"}[field.gold_state];const verdict=field.gold_state==="unscored"?field.reason||"분모 제외":field.gold_state==="absent"?(field.blank_hallucination===null?"실패로 평가 불가":field.blank_hallucination?"빈칸 오인식":"빈칸 유지"):(field.exact?"값 일치":"누락 / 불일치");row.append(node("td",field.label),node("td",state),node("td",(field.expected??"—")+" "+(field.expected_unit??"")),node("td",(field.actual??"—")+" "+(field.actual_unit??"")),node("td",verdict+(field.vision_requires_confirmation?" · 이미지 확인 필요":"")));table.append(row);}scroll.append(table);target.append(scroll);target.scrollIntoView({behavior:"smooth",block:"start"});
}
function switchSection(section) {
  for(const [name,tab,panel] of [["workspace","workspace-tab","workspace"],["results","results-tab","results-view"],["evaluation","evaluation-tab","evaluation"]]) {$(panel).hidden=section!==name;$(tab).setAttribute("aria-selected",String(section===name));}
  $("materials-panel").hidden=section==="evaluation";
  if(section==="results"&&typeof $("results-view").scrollIntoView==="function")$("results-view").scrollIntoView({block:"start",behavior:"auto"});
}
function switchTab(isEvaluation) {switchSection(isEvaluation?"evaluation":"workspace");}
function showConfirmation() {
  switchSection("workspace");$("manual-section").open=true;
  const heading=$("confirmation-heading");
  if(typeof heading.focus==="function")heading.focus({preventScroll:true});
  if(typeof heading.scrollIntoView==="function")heading.scrollIntoView({block:"start",behavior:"auto"});
}
function populateDocumentTypes(select) {
  const selected=select.value;
  select.replaceChildren();
  for(const [key,profile] of Object.entries(profiles)){const option=node("option",profile.label);option.value=key;select.append(option);}
  if(Object.hasOwn(profiles,selected))select.value=selected;
}
async function initialize() {
  profiles=await api("/api/extraction-profiles");
  const uploadType=node("select");uploadType.id="upload-document-type";uploadType.setAttribute("aria-label","업로드할 문서 유형");
  populateDocumentTypes(uploadType);
  populateDocumentTypes($("document-type"));
  const uploadLabel=node("label","업로드할 문서 유형","field upload-type");uploadLabel.htmlFor=uploadType.id;uploadLabel.append(uploadType);$("upload").closest("label").before(uploadLabel);
  buildFields();
  $("refresh-materials").addEventListener("click",safeAction(loadComparisonSources));
  $("load-materials").addEventListener("click",safeAction(applyMaterials));
  $("document-type").addEventListener("change",safeAction(async()=>{
    const selected=$("document-type").value;
    changingType=true; updateControls();
    try{if(current.materials_invalid){clearTimeout(draftTimer);pendingDraft=false;}else await flushDraft();current=await api("/api/cases/"+current.id+"/document-type","PUT",{document_type:selected});clearTimeout(pollTimer);currentJob=null;renderCase();await loadComparisonSources();notice("문서 유형을 "+profiles[selected].label+"로 변경했습니다. 추출하기를 눌러 주세요.");}
    catch(error){$("document-type").value=current.document_type;throw error;}
    finally{changingType=false;updateControls();}
  }));
  $("filter").addEventListener("change",renderDocuments);
  $("workspace-tab").addEventListener("click",()=>switchTab(false));$("results-tab").addEventListener("click",()=>switchSection("results"));$("evaluation-tab").addEventListener("click",safeAction(async()=>{await loadEvaluation();switchTab(true);}));
  $("refresh-evaluation").addEventListener("click",safeAction(loadEvaluation));
  for(const stage of ["extract","analyze","explain","report"])$(stage).addEventListener("click",safeAction(()=>start(stage)));
  $("confirmation-form").addEventListener("input",event=>{if(event.target.id!=="synthetic-auxiliary")changed();});
  $("confirmation-form").addEventListener("submit",safeAction(async event=>{event.preventDefault();await flushDraft();current=await api("/api/cases/"+current.id+"/confirm","POST",{});renderCase();switchSection("results");notice("입력값을 확인했습니다. 자료 대조 버튼을 눌러 결과를 확인하세요.");}));
  $("synthetic-auxiliary").addEventListener("change",safeAction(async()=>{if(!current)return;if($("synthetic-auxiliary").checked){const aux=await api("/api/documents/"+current.document_id+"/auxiliary");current.draft={...readForm(),...aux};}else{current.draft={...readForm(),manual:{},period_start:"",period_end:"",time_kind:"",earnings_complete:false,deductions_complete:false,payment_scope_confirmed:false,synthetic_auxiliary:false};}renderCase();changed();await flushDraft();renderCase();}));
  for(const prefix of ["","extraction-"]){
    $(prefix+"cancel").addEventListener("click",safeAction(async()=>{currentJob=await api("/api/jobs/"+currentJob.id+"/cancel","POST",{});renderJob();await pollJob();}));
    $(prefix+"resume").addEventListener("click",safeAction(async()=>{await flushDraft();currentJob=await api("/api/jobs/"+currentJob.id+"/resume","POST",{});renderJob();pollJob();}));
  }
  $("upload").addEventListener("change",safeAction(async event=>{const file=event.target.files[0];if(!file)return;await flushDraft();if(file.size>20*1024*1024)throw new Error("20MB 이하 PDF를 선택해 주세요.");const selected=$("upload-document-type").value;const response=await fetch("/api/uploads?document_type="+encodeURIComponent(selected),{method:"POST",headers:{"Content-Type":"application/pdf","X-Local-Agent":"1","X-Document-Name":encodeURIComponent(file.name)},body:file});const result=await response.json();if(!response.ok)throw new Error(result.error);current=result;currentJob=null;clearTimeout(pollTimer);await loadDocuments();rememberCase();renderCase();await loadComparisonSources();notice(profiles[selected].label+" PDF를 업로드했습니다. 유형을 확인하고 추출하기를 눌러 주세요.");event.target.value="";}));
  const health=await api("/api/health");$("connection").textContent=health.provider_mode==="fixture"?"가상 호출 모드 · 실제 API 사용 안 함":"로컬 연결됨 · "+health.model;
  await loadDocuments();await loadEvaluation();
  const saved=new URLSearchParams(location.search).get("case")||localStorage.getItem("agent-last-case");if(saved){try{current=await api("/api/cases/"+saved);rememberCase();renderCase();await restoreCurrentJob();return;}catch{current=null;}}
  const first=docs.find(d=>d.id==="filled-hourly")||docs[0];if(first)await selectDocument(first.id);
}
initialize().catch(error=>{$("connection").textContent="연결 확인 필요";notice(error.message,true);});
