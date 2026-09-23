import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from shapely.geometry import box, mapping

from app.analyses import LimitedProvider, RateGate
from app.config import Settings
from app.facilities import analyze_facilities
from app.stage_ledger import (
    StageLedger, assert_no_secrets, load_console_inventory, od_meta, observation_cacheable,
    od_cache_key, quota_inventory,
)
from life_circle.models import CancelToken, RouteObservation
from life_circle.providers import BaiduProvider
from tools.stage_baseline import (
    ORIGIN, aggregate_failure_reasons, live_pacing_error, live_provenance, main,
    observations_document, pack_rounds, qualified_improvement, run_mock_facilities,
    run_mock_walking, walking_payload,
)


def test_quota_inventory_never_claims_shared_account_limiter():
    inventory = quota_inventory(Settings(_env_file=None, analysis_provider="synthetic"))
    assert inventory["sharedAccountLimiter"] is False
    assert inventory["crossProcessLimiter"] is False
    assert inventory["b1JoinedFacilities"] is False
    assert inventory["analysisQpsConfigured"] is False
    assert inventory["analysisQpsIsLocalPacing"] is True
    assert inventory["mockQpsThreeNotProduction"] is True
    assert inventory["assumedMatrixFiftyOdPerSecondNotProduction"] is True
    assert inventory["sameAccountIfSameEnvFile"] is True
    assert inventory["sharedCredentialSource"] == "same-backend-env-file"
    assert inventory["console"]["status"] == "not-read-from-console"
    assert inventory["console"]["walkingPerSecond"] is None
    assert inventory["consoleWalkingVerified"] is False
    assert inventory["consoleMatrixVerified"] is False
    assert inventory["officialWalkingDirection"]["path"] == "/directionlite/v1/walking"
    assert {row["name"] for row in inventory["independentLimiters"]} == {
        "AnalysisManager.gate", "PoiRuntime.gate",
    }
    assert_no_secrets(inventory)


def test_console_inventory_merge_marks_walking_verified(tmp_path):
    path = tmp_path / "console.json"
    path.write_text(json.dumps({
        "schemaVersion": "console-inventory-v1",
        "verified": True,
        "checkedAt": "2026-09-16",
        "walkingDirection": {"permission": True, "perSecond": 3, "daily": 5000},
        "walkingMatrix": {"permission": None, "odPerSecond": None, "daily": None},
    }), encoding="utf-8")
    state = load_console_inventory(path)
    inventory = quota_inventory(Settings(_env_file=None, analysis_qps=3), console=state)
    assert inventory["console"]["status"] == "verified-console"
    assert inventory["consoleWalkingVerified"] is True
    assert inventory["consoleMatrixVerified"] is False
    assert inventory["consoleCheckRequired"] is True
    assert inventory["analysisQpsWithinConsoleWalking"] is True
    assert inventory["console"]["walkingPerSecond"] == 3
    assert inventory["console"]["walkingDaily"] == 5000
    assert_no_secrets(inventory)
    high = quota_inventory(Settings(_env_file=None, analysis_qps=400), console=state)
    assert high["analysisQpsWithinConsoleWalking"] is False
    with pytest.raises(FileNotFoundError):
        load_console_inventory(tmp_path / "missing.json")


def test_console_inventory_rejects_bad_values(tmp_path):
    path = tmp_path / "console.json"
    path.write_text(json.dumps({
        "schemaVersion": "console-inventory-v1",
        "walkingDirection": {"permission": "yes", "perSecond": 3, "daily": 5000},
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_console_inventory(path)


def test_od_cache_key_keeps_uid_and_metric_apart():
    origin, destination = ORIGIN, (ORIGIN[0] + 0.001, ORIGIN[1])
    base = dict(provider="baidu", api_version="directionlite/v1/walking", metric="distance",
                coord_system="bd09ll", origin=origin, destination=destination)
    left = od_cache_key(**base, destination_uid="a")
    right = od_cache_key(**base, destination_uid="b")
    duration = od_cache_key(**{**base, "metric": "duration"}, destination_uid="a")
    assert left != right
    assert left != duration
    assert observation_cacheable(None, True)
    assert not observation_cacheable(None, False)
    assert not observation_cacheable("no_result", True)
    assert not observation_cacheable("temporary", True)


def test_ledger_strips_credential_fields():
    ledger = StageLedger(real_network=False, qps=3, center={"lng": ORIGIN[0], "lat": ORIGIN[1]})
    ledger.record("walking", seconds=0.04, ak="should-drop", url="https://api.map.baidu.com/?ak=secret")
    assert "ak" not in ledger.events[0]
    assert "url" not in ledger.events[0]
    assert_no_secrets(ledger.summary())


def test_record_od_maps_baidu_identity_and_skips_synthetic():
    ledger = StageLedger(real_network=True, qps=3, task_id="task-a")
    baidu = ("baidu", "directionlite/v1/walking", "bd09ll", "steps=1", "", "poi-1", "distance")
    destination = (ORIGIN[0] + 0.001, ORIGIN[1])
    record = ledger.record_od(stage="walking", provider_identity=baidu, origin=ORIGIN,
                              destination=destination, reason=None, endpoint_verified=True,
                              duration=600.0, distance_m=900.0)
    assert record["provider"] == "baidu"
    assert record["api_version"] == "directionlite/v1/walking"
    assert record["metric"] == "distance"
    assert record["coord_system"] == "bd09ll"
    assert record["destination_uid"] == "poi-1"
    assert record["task_id"] == "task-a"
    assert record["network"] is True
    assert record["endpoint_verified"] is True
    assert record["origin"] == [ORIGIN[0], ORIGIN[1]]
    assert ledger.record_od(stage="walking", provider_identity=("synthetic", "v1"),
                            origin=ORIGIN, destination=destination) is None
    assert od_meta(("baidu",)) is None
    payload = ledger.od_payload("stage-baseline-live")
    assert payload["schemaVersion"] == "od-observations-v1"
    assert len(payload["observations"]) == 1
    assert_no_secrets(payload)


def test_mock_walking_ledger_is_pacing_bound_and_serial():
    payload = asyncio.run(run_mock_walking(n=10, qps=3, delay=0.04))
    summary = payload["summary"]
    assert summary["realNetworkRequests"] == 0
    assert summary["maxInFlight"] == 1
    assert summary["retries"] == 0
    assert summary["failures"] == 0
    assert payload["n"] == 10
    assert summary["countsByStage"]["walking"] == 10
    assert summary["secondsByStage"]["pacing"] > summary["secondsByStage"]["walking"]
    assert summary["dominantStage"] == "pacing"
    assert_no_secrets(payload)
    observations = payload["observations"]
    assert len(observations) == 10
    assert {row["provider"] for row in observations} == {"baidu"}
    assert {row["metric"] for row in observations} == {"duration"}
    assert all(row["endpoint_verified"] is True and row["reason"] is None for row in observations)
    assert all(row["task_id"] == "stage-baseline-mock" for row in observations)
    assert all(row["network"] is False for row in observations)
    assert_no_secrets(observations)


def test_mock_facilities_emits_uid_observations():
    payload = asyncio.run(run_mock_facilities(qps=3, delay=0.02))
    observations = payload["observations"]
    assert observations
    assert any(row["destination_uid"] == "poi-pharmacy-1" and row["metric"] == "distance"
               for row in observations)
    assert all(row["endpoint_verified"] is True for row in observations)
    assert_no_secrets(observations)


def test_observations_document_labels_mode():
    mock = observations_document({"observations": [{"provider": "baidu"}]}, "mock")
    assert mock["schemaVersion"] == "od-observations-v1"
    assert mock["label"] == "stage-baseline-mock"
    live = observations_document({"observations": []}, "live")
    assert live["label"] == "stage-baseline-live"
    assert live["observations"] == []


def test_mock_facilities_splits_search_walking_geometry_report():
    payload = asyncio.run(run_mock_facilities(qps=3, delay=0.02))
    stages = payload["summary"]["secondsByStage"]
    counts = payload["summary"]["countsByStage"]
    assert counts["poi_search"] >= 5
    assert counts["walking"] >= 1
    assert "geometry" in stages and "report" in stages
    assert payload["summary"]["maxInFlight"] == 1
    assert payload["summary"]["realNetworkRequests"] == 0
    assert payload["facilityCount"] == 1
    assert_no_secrets(payload)


def test_attached_ledger_does_not_change_facility_decisions():
    async def run():
        def handle(request):
            if "place/v3" in request.url.path:
                rows = [{"uid": "x", "name": "社区药店",
                         "location": {"lng": ORIGIN[0] + 0.001, "lat": ORIGIN[1]}}] if request.url.params["query"] == "药店" else []
                return httpx.Response(200, json={"status": 0, "total": len(rows), "results": rows})
            origin = tuple(float(part) for part in reversed(request.url.params["origin"].split(",")))
            destination = tuple(float(part) for part in reversed(request.url.params["destination"].split(",")))
            return httpx.Response(200, json=walking_payload(origin, destination, distance=899))
        geom = box(ORIGIN[0] - 0.01, ORIGIN[1] - 0.01, ORIGIN[0] + 0.01, ORIGIN[1] + 0.01)
        result = SimpleNamespace(
            config=SimpleNamespace(origin=ORIGIN, extent=1600),
            local_geometry=box(-1000, -1000, 1000, 1000),
            geometry=mapping(geom),
            sample_observations=[RouteObservation(ORIGIN, 0)],
        )
        ledger = StageLedger()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with ledger.attach():
                _, _, summary, report = await analyze_facilities(
                    result, client, "offline-fixture", RateGate(None), CancelToken())
        medical = next(item for item in summary.assessments[0].categories if item.category == "medical")
        assert medical.status == "covered"
        assert summary.network_requests == 6
        assert "不生成覆盖率" in report
        assert ledger.summary()["countsByStage"]["walking"] >= 1
    asyncio.run(run())


def test_limited_provider_records_deadline_without_inflight():
    async def run():
        ledger = StageLedger(qps=3)
        gate = RateGate(3)
        async def handle(request):
            return httpx.Response(200, json=walking_payload(ORIGIN, (ORIGIN[0] + 0.001, ORIGIN[1])))
        with ledger.attach():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                provider = LimitedProvider(BaiduProvider("offline-fixture", client=client), gate)
                observed = await provider.query_walking_time(ORIGIN, (ORIGIN[0] + 0.001, ORIGIN[1]), time.monotonic() - 1)
        assert observed.reason == "deadline"
        assert ledger.max_in_flight == 0
        assert any(event["stage"] == "pacing" and event["reason"] == "deadline" for event in ledger.events)
    asyncio.run(run())


def test_tool_inventory_json_roundtrip(tmp_path):
    output = tmp_path / "inventory.json"
    assert main(["--mode", "inventory", "--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == "quota-inventory-v1"
    assert payload["consoleCheckRequired"] is True
    assert payload["console"]["matrixDaily"] is None
    assert_no_secrets(payload)


def test_pack_rounds_publishes_mean_min_max_without_inventing_threshold():
    summaries = [
        {"wallSeconds": 3.50, "retries": 0, "failures": 0, "networkAttempts": 10,
         "maxInFlight": 1, "dominantStage": "pacing", "realNetworkRequests": 0},
        {"wallSeconds": 3.52, "retries": 0, "failures": 0, "networkAttempts": 10,
         "maxInFlight": 1, "dominantStage": "pacing", "realNetworkRequests": 0},
        {"wallSeconds": 3.51, "retries": 0, "failures": 0, "networkAttempts": 10,
         "maxInFlight": 1, "dominantStage": "pacing", "realNetworkRequests": 0},
    ]
    packed = pack_rounds(10, 3, summaries)
    assert packed["rounds"] == 3
    assert packed["wallMin"] == 3.50
    assert packed["wallMax"] == 3.52
    assert packed["maxInFlight"] == 1
    note = qualified_improvement(packed, live=False)
    assert note["status"] == "unset-until-live"
    assert note["wallPeakToPeakSeconds"] == packed["wallPeakToPeak"]


def test_summary_reports_successes_and_failure_reasons():
    ledger = StageLedger(real_network=True, qps=3)
    ledger.record("walking", seconds=0.1)
    ledger.record("walking", seconds=0.1, reason="no_result")
    ledger.record("poi_search", seconds=0.1, reason="temporary", attempt=2)
    summary = ledger.summary()
    assert summary["networkAttempts"] == 3
    assert summary["successes"] == 1
    assert summary["failures"] == 2
    assert summary["failureReasons"] == {"no_result": 1, "temporary": 1}
    assert summary["retries"] == 1
    assert_no_secrets(summary)


def test_live_provenance_marks_local_pacing():
    provenance = live_provenance(3)
    assert provenance["pacingQps"] == 3
    assert provenance["pacingQpsIsLocalOnly"] is True
    assert provenance["quotaSource"] == "local-env-not-console"
    assert provenance["liveProvider"] == "baidu/directionlite/v1/walking"
    assert provenance["consoleCheckRequired"] is True
    assert_no_secrets(provenance)


def test_aggregate_failure_reasons_sums_rounds():
    totals = aggregate_failure_reasons([
        {"failureReasons": {"no_result": 1}},
        {"failureReasons": {"no_result": 2, "endpoint_offset": 1}},
    ])
    assert totals == {"endpoint_offset": 1, "no_result": 3}
    assert aggregate_failure_reasons([{}]) == {}


def test_live_pacing_guard_requires_qps_and_caps_it():
    assert "requires" in live_pacing_error(None, 3.0)
    assert live_pacing_error(3.0, 3.0) is None
    assert "live-max-qps" in live_pacing_error(50.0, 3.0)


def test_live_mode_requires_accept_quota():
    try:
        main(["--mode", "live", "--n", "2"])
    except SystemExit as exc:
        assert "accept-quota" in str(exc)
    else:
        raise AssertionError("live mode must refuse without --accept-quota")


def test_live_mode_refuses_pacing_above_cap_before_env():
    try:
        main(["--mode", "live", "--n", "2", "--qps", "50", "--accept-quota"])
    except SystemExit as exc:
        assert "live-max-qps" in str(exc)
    else:
        raise AssertionError("live mode must refuse pacing above --live-max-qps")
