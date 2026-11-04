"""Run three explicitly synthetic auxiliary scenarios with real explanations."""

import argparse
import asyncio
from pathlib import Path

from agent.config import AgentSettings
from agent.dev.contract_corpus import DEFAULT_OUTPUT, read_json, write_json
from agent.dev.evaluate_corpus import summarize
from agent.dev.local_workflow import LocalStore, synthetic_auxiliary


async def validate(output, settings):
    store = LocalStore(settings, corpus=output, origin="corpus_flow")
    index_path = output / "flows/index.json"
    cases = read_json(index_path) if index_path.exists() else {}
    records = []
    for doc_id in ("filled-hourly", "filled-monthly", "filled-missing"):
        if doc_id not in cases:
            case = await store.create_case(doc_id)
            cases[doc_id] = case["id"]
            write_json(index_path, cases)
        cid = cases[doc_id]
        case = store.cases[cid]
        if case["extraction"] is None:
            job = await store.create_job(cid, "extract", use_saved=True)
            await store.tasks[job["id"]]
            assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
        if case["confirmed"] is None:
            draft = {**case["draft"], **synthetic_auxiliary(doc_id)}
            await store.save_draft(cid, draft)
            await store.confirm(cid)
        for stage, result_key in (("analyze", "analysis"), ("explain", "explanation"), ("report", "report")):
            if case[result_key]:
                continue
            prior = next(
                (store.jobs[jid] for jid in reversed(case["jobs"]) if store.jobs[jid]["stage"] == stage), None
            )
            if prior and prior["status"] in ("interrupted", "failed", "cancelled"):
                job = await store.resume_job(prior["id"])
            else:
                job = await store.create_job(cid, stage, include_original=True)
            await store.tasks[job["id"]]
            actual = store.jobs[job["id"]]
            print(doc_id, stage, actual["status"], actual.get("error_code", ""), flush=True)
            if actual["status"] != "completed":
                raise RuntimeError("Local flow failed: " + str(actual.get("error_code")))
            if stage == "explain":
                ports = store.ports[job["id"]]
                cp = ports.checkpoints[job["id"]]
                write_json(
                    output / "usage" / ("flow-" + doc_id + ".json"),
                    {
                        "usage": actual["usage"],
                        "call_budget": cp.call_budget,
                        "model_calls": sum(
                            v for k, v in cp.call_budget.items() if not k.endswith(":format_failures")
                        ),
                    },
                )
        comparisons = {c["rule_id"]: c for c in case["analysis"]["comparisons"]}
        assert case["analysis"]["wage_section"]["gross_total"] is None
        if doc_id == "filled-hourly":
            assert comparisons["R-01"]["status"] == "difference"
        elif doc_id == "filled-monthly":
            assert comparisons["R-06"]["delta"] == "100000"
        else:
            assert comparisons["R-01"]["status"] == "insufficient"
        record = {
            "document_id": doc_id,
            "case_id": cid,
            "comparisons": comparisons,
            "explanation": case["explanation"],
            "report": case["report"],
        }
        write_json(output / "flows" / (doc_id + ".json"), record)
        records.append(record)
    write_json(output / "flows/summary.json", {"count": 3, "passed": True, "records": records})
    summarize(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--env", type=Path, default=Path("agent/.env"))
    args = parser.parse_args()
    asyncio.run(validate(args.output, AgentSettings.from_env(args.env, provider_mode="openai")))
