import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.stage_ledger import assert_no_secrets
from tools.qps_probe import main, plan_document, run_probe

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


def handler(walking_status=0, capture=None):
    async def handle(request):
        if capture is not None:
            capture.append(request)
        origin = tuple(float(part) for part in reversed(request.url.params["origin"].split(",")))
        destination = tuple(float(part) for part in reversed(request.url.params["destination"].split(",")))
        return httpx.Response(200, json=walking_payload(origin, destination, status=walking_status))
    return handle


def test_plan_is_zero_request_and_hashed(tmp_path):
    output = tmp_path / "plan.json"
    assert main(["--mode", "plan", "--output", str(output)]) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["schemaVersion"] == "qps-probe-plan-v1"
    assert document["estimatedRequests"] == 51
    assert document["estimatedRequests"] <= document["budget"] == 60
    assert_no_secrets(document)
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "plan", "--output", str(output)])
    assert "output_already_exists" in str(exc.value)


def test_run_probe_reports_highest_passing_level():
    result = asyncio.run(run_probe(settings(), levels=(1.0, 2.0), budget=10,
                                   transport=httpx.MockTransport(handler())))
    assert result["observedCeilingQps"] == 2.0
    assert result["stopReason"] is None
    assert result["realNetworkRequests"] == 3
    assert [row["passed"] for row in result["levels"]] == [True, True]
    assert result["levels"][0]["durationMeanSeconds"] == 600
    assert_no_secrets(result)


def test_run_probe_stops_on_rate_limit_without_extra_requests():
    result = asyncio.run(run_probe(settings(), levels=(1.0, 2.0, 3.0), budget=10,
                                   transport=httpx.MockTransport(handler(walking_status=401))))
    assert result["observedCeilingQps"] is None
    assert result["stopReason"] == "rate_limit"
    assert result["realNetworkRequests"] == 1
    assert result["levels"][0]["outcomes"] == {"rate_limit": 1}
    assert result["levels"][0]["passed"] is False


def test_run_probe_stops_on_quota():
    result = asyncio.run(run_probe(settings(), levels=(1.0, 3.0), budget=10,
                                   transport=httpx.MockTransport(handler(walking_status=302))))
    assert result["stopReason"] == "quota"
    assert result["levels"][0]["outcomes"] == {"quota": 1}


def test_run_probe_budget_caps_levels():
    result = asyncio.run(run_probe(settings(), levels=(1.0, 2.0, 3.0), budget=3,
                                   transport=httpx.MockTransport(handler())))
    assert result["realNetworkRequests"] == 3
    assert [(row["qps"], row["plannedRequests"]) for row in result["levels"]] == [(1.0, 1), (2.0, 2)]


def test_run_probe_concurrency_reaches_target_rate():
    result = asyncio.run(run_probe(settings(), levels=(3.0,), budget=3, concurrency=3,
                                   transport=httpx.MockTransport(handler())))
    row = result["levels"][0]
    assert row["concurrency"] == 3
    assert (row["sent"], row["ok"], row["passed"]) == (3, 3, True)
    assert row["maxInOneSecond"] == 3
    assert result["observedCeilingQps"] == 3.0


def test_run_probe_concurrency_stops_on_rate_limit():
    result = asyncio.run(run_probe(settings(), levels=(3.0, 5.0), budget=10, concurrency=3,
                                   transport=httpx.MockTransport(handler(walking_status=401))))
    assert result["stopReason"] == "rate_limit"
    assert result["levels"][0]["outcomes"] == {"rate_limit": 3}
    assert len(result["levels"]) == 1
    assert result["realNetworkRequests"] == 3


def test_cli_guards():
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "live", "--output", "unused.json"])
    assert "accept-quota" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "plan", "--output", "unused.json", "--concurrency", "4"])
    assert "concurrency" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "plan", "--output", "unused.json", "--budget", "61"])
    assert "budget" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "plan", "--output", "unused.json", "--max-qps", "21"])
    assert "max-qps" in str(exc.value)
    with pytest.raises(SystemExit) as exc:
        main(["--mode", "plan", "--output", "unused.json", "--levels", "1,30"])
    assert "level exceeds" in str(exc.value)


def test_plan_document_respects_budget():
    document = plan_document((1.0, 2.0, 3.0), budget=4, max_qps=20.0)
    assert document["estimatedRequests"] == 4
    assert [(row["qps"], row["plannedRequests"]) for row in document["levels"]] == [(1.0, 1), (2.0, 2), (3.0, 1)]
