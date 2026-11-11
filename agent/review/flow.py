"""Implemented topology and observed routes, kept separate from token accounting."""

from agent.review.telemetry import add_usage, timestamp, usage_numbers


def node(key, label, kind, x, y, detail="", width=250):
    return dict(id=key, label=label, kind=kind, x=x, y=y, width=width, height=104, detail=detail)


DOCUMENT_NODES = [
    node("start", "파일 처리 요청", "code", 415, 20, "업로드 · 재시도 · 유형 지정"),
    node("format", "파일 형식 분기", "decision", 415, 170, "PDF / JPG · PNG"),
    node("quality", "PDF 텍스트 판독 가능?", "decision", 65, 320, "pdf.inspect_text"),
    node("image", "이미지 방향 보정", "tool", 765, 320, "image.prepare"),
    node("native", "PDF 텍스트·좌표 읽기", "tool", 65, 480, "pdf.read_native"),
    node("render_ocr", "스캔 페이지 이미지 변환", "tool", 415, 480, "page.render"),
    node("ocr", "한국어·영어 OCR", "tool", 765, 480, "ocr.tesseract"),
    node("classify", "문서 분류 에이전트", "model", 415, 640, "model.classify"),
    node("manual_type", "유형·페이지 사용자 지정", "human", 765, 640, "분류 모델 호출 생략 가능"),
    node("type_gate", "유형·구간 확인됨?", "decision", 415, 800, "계약 · 명세 · 근태 · 입금"),
    node("type_wait", "유형 확인·직접 입력", "human", 65, 800, "모호한 구간 · 판독 실패"),
    node("extract", "필드 추출 에이전트", "model", 415, 960, "model.extract"),
    node("cache", "기존 판독 결과 재사용", "code", 65, 1120, "선택 항목 재검증 요청"),
    node("validate", "필드·근거 검증", "tool", 415, 1120, "fields.validate"),
    node("recheck", "재검증 대상 있음?", "decision", 415, 1280, "누락 · 모호성 · 충돌 / 요청"),
    node("render_verify", "대상 페이지 이미지 준비", "tool", 765, 1280, "page.render"),
    node("locate", "원문 위치 다시 연결", "code", 65, 1440, "locate · 좌표 재판독 요청"),
    node("verify", "원본 재검증 에이전트", "model", 765, 1440, "model.verify · 자동 1회"),
    node("review", "원문 비교·수정·사용자 확정", "human", 415, 1600, "확인 대기 / 직접 입력 가능"),
    node("validate_again", "재검증 응답 검증", "tool", 765, 1600, "fields.validate"),
]
WORK_NODES = [
    node("request", "업무 실행 요청", "human", 415, 20, "준비 · 대조 · 임금 검토"),
    node("ready", "필요한 확정값 준비됨?", "decision", 415, 180, "capabilities · wake"),
    node("wait", "자료·확정값 추가 대기", "code", 765, 180, "요건 충족 시 1회 자동 재개"),
    node("snapshot", "확정값 스냅샷 생성", "code", 415, 340, "prepare_confirmed_snapshot"),
    node("supervisor", "계산 컨텍스트 구성", "code", 415, 500, "supervisor"),
    node("worker_wage", "임금 계산", "code", 25, 660, "worker_wage", 230),
    node("worker_terms", "계약 조건 대조", "code", 290, 660, "worker_terms", 230),
    node("worker_statement", "명세서 대조", "code", 555, 660, "worker_statement", 230),
    node("worker_payment", "입금 대조", "code", 820, 660, "worker_payment", 230),
    node("aggregate", "병렬 결과 통합", "code", 415, 820, "aggregate"),
    node("business_validate", "계산·근거 재검증", "code", 415, 980, "validator"),
    node("finalize", "결과·확인 질문 생성", "code", 415, 1140, "finalize"),
    node("compose", "결과 설명 작성", "code", 65, 1300, "compose · 현재 화면은 서식 방식"),
    node("explain_model", "설명 모델 (선택 모드)", "model", 65, 1460, "explain · API model 모드"),
    node("guard", "설명 문구 검증", "code", 415, 1460, "guard"),
    node("publish", "설명 결과 저장", "code", 415, 1620, "publish"),
    node("build_report", "보고서·원본 ZIP 생성", "code", 765, 1300, "build_report · 별도 요청"),
]
LEGACY_NODES = [
    node("legacy_ocr", "기존 OCR 저장", "tool", 25, 20, "/ocr · Tesseract", 230),
    node("legacy_inspect", "문서 판독", "code", 290, 20, "inspect_document", 230),
    node("legacy_extract", "후보 추출 모델", "model", 555, 20, "extract_candidates", 230),
    node("legacy_validate", "후보 검증·패키징", "code", 820, 20, "verify / package_candidates", 230),
    node(
        "legacy_supplement", "선택 항목 원본 보완", "model", 555, 180, "supplement_graph · PDF 시각 판독", 230
    ),
]


def edge(source, target, label="", via=None):
    return dict(id=f"{source}>{target}", source=source, target=target, label=label, via=via)


DOCUMENT_EDGES = [
    edge("start", "format"),
    edge("format", "quality", "PDF"),
    edge("format", "image", "JPG / PNG"),
    edge("quality", "native", "가능"),
    edge("quality", "render_ocr", "불량·스캔"),
    edge("image", "ocr"),
    edge("render_ocr", "ocr"),
    edge("native", "classify"),
    edge("ocr", "classify"),
    edge("native", "type_wait", "읽을 내용 없음"),
    edge("ocr", "type_wait", "읽을 내용 없음", [[1040, 532], [1040, 755], [190, 755]]),
    edge("classify", "type_gate"),
    edge("native", "manual_type", "사용자 지정", [[345, 600], [1040, 600], [1040, 692]]),
    edge("ocr", "manual_type", "사용자 지정"),
    edge("manual_type", "type_gate"),
    edge("type_gate", "type_wait", "아니오"),
    edge("type_gate", "extract", "예"),
    edge("extract", "validate"),
    edge("validate", "recheck"),
    edge("recheck", "render_verify", "예"),
    edge("render_verify", "verify"),
    edge("verify", "validate_again"),
    edge("validate_again", "review"),
    edge("recheck", "review", "없음"),
    edge("type_wait", "review", "직접 입력·확인", [[35, 852], [35, 1735], [540, 1735]]),
    edge("start", "cache", "선택 항목 재검증", [[15, 72], [15, 1172]]),
    edge("cache", "recheck"),
    edge("native", "recheck", "항목 재검증", [[350, 532], [350, 1332]]),
    edge("ocr", "recheck", "항목 재검증", [[1045, 532], [1045, 1235], [540, 1235]]),
    edge("native", "locate", "위치 재판독", [[335, 532], [335, 1492]]),
    edge("ocr", "locate", "위치 재판독", [[1060, 532], [1060, 1760], [190, 1760]]),
    edge("locate", "review"),
]
WORK_EDGES = [
    edge("request", "ready"),
    edge("ready", "wait", "부족"),
    edge("wait", "ready", "요건 추가", [[1015, 300], [540, 300]]),
    edge("ready", "snapshot", "충족"),
    edge("snapshot", "supervisor", "계산·대조 요청"),
    *[
        edge("supervisor", name, "병렬")
        for name in ("worker_wage", "worker_terms", "worker_statement", "worker_payment")
    ],
    *[
        edge(name, "aggregate")
        for name in ("worker_wage", "worker_terms", "worker_statement", "worker_payment")
    ],
    edge("aggregate", "business_validate"),
    edge("business_validate", "finalize"),
    edge("finalize", "compose", "설명 요청"),
    edge("finalize", "build_report", "보고서 요청"),
    edge("compose", "guard", "서식 모드"),
    edge("compose", "explain_model", "모델 모드"),
    edge("explain_model", "guard"),
    edge("guard", "publish"),
]
LEGACY_EDGES = [
    edge("legacy_ocr", "legacy_inspect"),
    edge("legacy_inspect", "legacy_extract"),
    edge("legacy_extract", "legacy_validate"),
    edge("legacy_validate", "legacy_supplement", "보완 요청"),
]

CHAT_NODES = [
    node("chat_start", "검토별 질문·대화 이력", "human", 415, 20, "자료·확정 상태를 변경하지 않는 요청"),
    node("chat_context", "프로젝트 근거 조회", "tool", 415, 180, "현재 자료 · 필드 · 실행 조건 · 대조 결과"),
    node("chat_answer", "챗봇 에이전트", "model", 415, 340, "model.chat · 근거 기반 안내"),
    node("chat_fallback", "저장 상태로 기본 안내", "code", 765, 500, "모델 연결 실패 시"),
    node("chat_guard", "출처·프로젝트 범위 검증", "code", 415, 660, "서버에서 생성한 실제 자료 링크만 연결"),
    node("chat_reply", "답변·근거·대화 저장", "code", 415, 820, "필요한 화면으로 이동 · 사용자 직접 수행"),
]
CHAT_EDGES = [
    edge("chat_start", "chat_context"),
    edge("chat_context", "chat_answer"),
    edge("chat_answer", "chat_guard", "응답 완료"),
    edge("chat_answer", "chat_fallback", "모델 연결 실패"),
    edge("chat_fallback", "chat_guard"),
    edge("chat_guard", "chat_reply"),
]

RECOMMEND_NODES = [
    node("recommend_start", "문서 처리 완료·추천 요청", "code", 415, 20, "입력값을 바꾸지 않고 분석"),
    node("recommend_compare", "표기·역할·기간 비교", "tool", 415, 180, "회사명 정리 · 동일 항목의 근거 비교"),
    node("recommend_evidence", "원문·재검증 근거 종합", "tool", 415, 340, "명확한 근거가 있는 값만 추천"),
    node("recommend_model", "표기 비교·추천 에이전트", "model", 765, 500, "model.recommend · 애매한 항목만"),
    node(
        "recommend_gate", "추천 가능·실제 충돌 분기", "decision", 415, 660, "모델 추천도 서버에서 근거 검증"
    ),
    node("recommend_proposals", "추천값·이유 저장", "code", 65, 820, "사용자 적용 전 입력값 유지"),
    node(
        "recommend_conflict", "필수 확인 · 기준값 선택", "human", 765, 820, "일괄 확정 제외 · 원문별 값 보존"
    ),
    node("recommend_apply", "AI가 채우기 · 임시 적용", "human", 415, 980, "수정·확정된 값 덮어쓰기 금지"),
    node("recommend_confirm", "원문 확인 후 개별 확정", "human", 415, 1140, "확정값만 계산·대조에 사용"),
]
RECOMMEND_EDGES = [
    edge("recommend_start", "recommend_compare"),
    edge("recommend_compare", "recommend_evidence"),
    edge("recommend_evidence", "recommend_model", "추가 판단 필요"),
    edge("recommend_evidence", "recommend_gate", "근거로 판별 가능"),
    edge("recommend_model", "recommend_gate"),
    edge("recommend_gate", "recommend_proposals", "추천 가능"),
    edge("recommend_gate", "recommend_conflict", "실제 충돌"),
    edge("recommend_gate", "recommend_apply", "추천 없음"),
    edge("recommend_proposals", "recommend_apply"),
    edge("recommend_apply", "recommend_confirm"),
    edge("recommend_conflict", "recommend_confirm"),
]

TOPOLOGY = [
    dict(
        id="document",
        title="문서 처리 그래프",
        width=1080,
        height=1790,
        nodes=DOCUMENT_NODES,
        edges=DOCUMENT_EDGES,
    ),
    dict(
        id="work",
        title="업무 실행 그래프 · 사용자 요청 후 실행",
        width=1080,
        height=1760,
        nodes=WORK_NODES,
        edges=WORK_EDGES,
    ),
    dict(
        id="chat",
        title="챗봇 그래프 · 현재 검토의 자료와 결과 안내",
        width=1080,
        height=970,
        nodes=CHAT_NODES,
        edges=CHAT_EDGES,
    ),
    dict(
        id="recommendation",
        title="표기 비교·추천 그래프 · 적용과 확정은 사용자 수행",
        width=1080,
        height=1300,
        nodes=RECOMMEND_NODES,
        edges=RECOMMEND_EDGES,
    ),
    dict(
        id="legacy",
        title="기존 /ocr · /ai 화면의 별도 경로",
        width=1080,
        height=320,
        nodes=LEGACY_NODES,
        edges=LEGACY_EDGES,
    ),
]
NODE_IDS = {n["id"] for group in TOPOLOGY for n in group["nodes"]}
EDGE_IDS = {e["id"] for group in TOPOLOGY for e in group["edges"]}


def visit(job, node_id, *sources, status="completed"):
    """Called only when control actually reaches a node/branch, never from the UI."""
    assert node_id in NODE_IDS, node_id
    job.setdefault("route", []).append(
        dict(node=node_id, sources=list(sources), status=status, at=timestamp())
    )


def reading_route(job, reading):
    """Reader spans carry page-local ordering, including failed OCR calls."""
    ends = []
    by_page = {}
    for event in reading.get("reader_calls", []):
        page = event.get("context", {}).get("page")
        before = by_page.get(page, "format")
        name = {
            "pdf.inspect_text": "quality",
            "pdf.read_native": "native",
            "image.prepare": "image",
            "page.render": "render_ocr",
            "ocr.tesseract": "ocr",
        }.get(event["name"])
        if name:
            visit(job, name, before, status=event["status"])
            event["flow_node"] = name
            by_page[page] = name
    ends.extend(dict.fromkeys(by_page.values()))
    return ends


def event_node(event, business=False):
    if event.get("flow_node"):
        return event["flow_node"]
    name = event.get("name", "")
    if business:
        return "business_validate" if name == "validator" else name if name in NODE_IDS else None
    return {
        "model.classify": "classify",
        "model.extract": "extract",
        "model.verify": "verify",
        "document.read": "format",
        "pdf.inspect_text": "quality",
        "pdf.read_native": "native",
        "image.prepare": "image",
        "ocr.tesseract": "ocr",
        "fields.validate": "validate",
        "page.render": "render_verify" if event.get("stage") == "verify" else "render_ocr",
    }.get(name)


def project(job, trace, *, business=False, route=None):
    states, traversed = {}, set()
    observed = route if route is not None else job.get("route", [])
    for item in observed:
        states.setdefault(item["node"], {})["status"] = item["status"]
        for source in item.get("sources", []):
            key = f"{source}>{item['node']}"
            if key in EDGE_IDS:
                traversed.add(key)
    for event in trace:
        key = event_node(event, business)
        if not key:
            continue
        state = states.setdefault(key, {})
        status = event.get("status", "completed")
        if state.get("status") not in ("failed", "cancelled", "interrupted"):
            state["status"] = status
        state["calls"] = state.get("calls", 0) + 1
        if event.get("kind") == "agent":
            add_usage(state.setdefault("usage", {}), event.get("usage"))
            state["unknown_calls"] = state.get("unknown_calls", 0) + int(
                not usage_numbers(event.get("usage"))
            )
        state.setdefault("tools", [])
        if event["name"] not in state["tools"]:
            state["tools"].append(event["name"])
    if job.get("status") in ("failed", "cancelled", "interrupted", "superseded", "completed", "needs_review"):
        for state in states.values():
            if state.get("status") == "running":
                state["status"] = "interrupted"
            elif business and state.get("status") == "waiting" and job.get("status") != "completed":
                state["status"] = "interrupted"
    # Parallel engine edges are defined by the graph, not the order of worker completion.
    if business:
        for item in WORK_EDGES:
            if item["source"] in states and item["target"] in states:
                traversed.add(item["id"])
    return dict(nodes=states, edges=sorted(traversed), route_recorded="route" in job or route is not None)
