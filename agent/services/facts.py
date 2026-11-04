def allowed_facts(result):
    facts = {"summary": "자료상 일치·차이·정보 부족·검토 필요를 항목별로 확인했습니다."}
    for index, comparison in enumerate(result.comparisons):
        prefix = f"comparison_{index}"
        facts[prefix + "_rule"] = comparison.rule_id
        facts[prefix + "_status"] = {
            "match": "자료상 일치",
            "difference": "차이 발견",
            "insufficient": "정보 부족",
            "needs_review": "검토 필요",
        }[comparison.status]
        if comparison.delta is not None:
            facts[prefix + "_delta"] = format(comparison.delta, "f")
    return facts


def allowed_templates(facts):
    templates = ["{{summary}}", "확인 결과: {{summary}}"]
    for key in facts:
        if key.endswith("_status"):
            prefix = key.removesuffix("_status")
            templates.append("{{" + prefix + "_rule}} 항목은 {{" + key + "}}입니다.")
        elif key.endswith("_delta"):
            prefix = key.removesuffix("_delta")
            templates.append(
                "{{" + prefix + "_rule}}의 왼쪽 값에서 오른쪽 값을 뺀 차이는 {{" + key + "}}입니다."
            )
    return templates
