import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.stage_ledger import assert_no_secrets
from tools.matrix_contract import review
from tools.matrix_pair import (
    main, matrix_point, parse_matrix_payload, plan_document, plan_hash, run_paired, validate_plan,
)

ORIGIN = (121.513926, 31.313077)


def settings():
    return Settings(_env_file=None, baidu_map_ak="offline-fixture", analysis_provider="synthetic")


def walking_payload(origin, destination, distance=900, duration=600, status=0):
    start = {"lng": str(origin[0]), "lat": str(origin[1])}
    end = {"lng": str(destination[0]), "lat": str(destination[1])}
    routes = [] if status else [{
        "distance": distance, "duration": duration,
        "steps": [{"start_location": start, "end_location": end,
                   "path": f"{origin[0]},{origin[1]};{destination[0]},{destination[1]}"}],
    }]
    return {"status": status, "result": {"routes": routes}}


def handler(walking_status=0, matrix_status=0, matrix_cells=None, capture=None, matrix_http=200):
    async def handle(request):
        if capture is not None:
            capture.append(request)
        if "routematrix" in request.url.path:
            count = len(request.url.params["destinations"].split("|"))
            cells = matrix_cells if matrix_cells is not None else [
                {"distance": {"value": 905}, "duration": {"value": 605}} for _ in range(count)]
            return httpx.Response(matrix_http, json={"status": matrix_status, "result": cells})
        origin = tuple(float(part) for part in reversed(request.url.params["origin"].split(",")))
        destination = tuple(float(part) for part in reversed(request.url.params["destination"].split(",")))
        return httpx.Response(200, json=walking_payload(origin, destination, status=walking_status))
    return handle


def test_plan_document_is_frozen_and_hashed():
    plan = validate_plan(plan_document(5))
    assert plan["schemaVersion"] == "matrix-pair-plan-v1"
    assert len(plan["cases"]) == 5
    assert plan["planHash"] == plan_hash(plan["cases"])
    assert {tuple(case["origin"]) for case in plan["cases"]} == {ORIGIN}
    assert_no_secrets(plan)


def test_plan_cli_refuses_overwrite(tmp_path):
    output = tmp_path / "plan.json"
    assert main(["--mode", "plan", "--n", "3", "--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["cases"]) == 3
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "plan", "--n", "3", "--output", str(output)])
    assert "output_already_exists" in str(exc.value)


def test_validate_plan_rejects_hash_mismatch_and_mixed_origins():
    plan = plan_document(3)
    plan["planHash"] = "deadbeef"
    with pytest.raises(ValueError):
        validate_plan(plan)
    plan = plan_document(3)
    plan["cases"][1]["origin"] = [121.0, 31.0]
    plan["planHash"] = plan_hash(plan["cases"])
    with pytest.raises(ValueError):
        validate_plan(plan)


def test_parse_matrix_payload_zero_and_errors():
    parsed = parse_matrix_payload({
        "status": 0,
        "result": [
            {"distance": {"value": 0}, "duration": {"value": 0}},
            {"distance": 905, "duration": 605},
        ],
    }, 2)
    assert parsed["error"] is None
    assert parsed["cells"][0] == {"distance": 0.0, "duration": 0.0}
    assert parsed["cells"][1] == {"distance": 905.0, "duration": 605.0}
    assert parse_matrix_payload({"status": 2, "result": []}, 1)["error"] == "upstream_status"
    assert parse_matrix_payload({"status": 0, "result": []}, 1)["error"] == "invalid_response"
    assert parse_matrix_payload("nope", 1)["error"] == "invalid_response"


def test_matrix_point_carries_uid_suffix():
    assert matrix_point((121.513926, 31.313077)) == "31.313077,121.513926"
    assert matrix_point((121.513926, 31.313077), "poi-9") == "31.313077,121.513926;poi-9"


def test_run_paired_mock_end_to_end():
    plan = validate_plan(plan_document(3))
    capture = []
    result = asyncio.run(run_paired(plan, settings(), qps=50,
                                    transport=httpx.MockTransport(handler(capture=capture))))
    cases, collection = result["cases"], result["collection"]
    assert cases["label"] == "paired-live"
    assert cases["planHash"] == plan["planHash"]
    assert len(cases["cases"]) == 3
    for row in cases["cases"]:
        assert row["single"]["endpointVerified"] is True
        assert row["single"]["distance"] == 900
        assert row["single"]["duration"] == 600
        assert row["matrix"] == {"distance": 905.0, "duration": 605.0}
    assert collection["stopReason"] is None
    assert collection["singleRequests"] == 3
    assert collection["matrixHttpRequests"] == 1
    assert collection["realNetworkRequests"] == 4
    matrix_requests = [request for request in capture if "routematrix" in request.url.path]
    assert len(matrix_requests) == 1
    assert matrix_requests[0].url.params["destinations"].count("|") == 2
    assert matrix_requests[0].url.params["coord_type"] == "bd09ll"
    assert_no_secrets(cases)
    assert_no_secrets(collection)
    verdict = review(cases, label="paired-live")
    assert verdict["gateEligible"] is True
    assert verdict["verdict"] == "compatible"


def test_run_paired_zero_cells_stay_unknown():
    plan = validate_plan(plan_document(2))
    cells = [{"distance": {"value": 0}, "duration": {"value": 0}}] * 2
    result = asyncio.run(run_paired(plan, settings(), qps=50,
                                    transport=httpx.MockTransport(handler(matrix_cells=cells))))
    verdict = review(result["cases"], label="paired-live")
    assert verdict["totals"]["matrixUnknown"] == 2
    assert verdict["verdict"] == "not_run"


def test_run_paired_aborts_on_quota():
    plan = validate_plan(plan_document(3))
    result = asyncio.run(run_paired(plan, settings(), qps=50,
                                    transport=httpx.MockTransport(handler(walking_status=302))))
    collection = result["collection"]
    assert collection["stopReason"] == "quota"
    assert collection["matrix"] is None
    assert collection["realNetworkRequests"] == 1
    assert all(row["single"] is None for row in result["cases"]["cases"])


def test_live_cli_guards(tmp_path):
    plan_path = tmp_path / "plan.json"
    assert main(["--mode", "plan", "--n", "2", "--output", str(plan_path)]) == 0
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "live", "--plan", str(plan_path), "--output", str(tmp_path / "a.json")])
    assert "accept-quota" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "live", "--plan", str(plan_path), "--output", str(tmp_path / "b.json"),
              "--accept-quota", "--qps", "50"])
    assert "live-max-qps" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "live", "--output", str(tmp_path / "c.json"), "--accept-quota"])
    assert "frozen --plan" in str(exc.value)
