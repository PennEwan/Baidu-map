"""Stage-1 baseline: quota inventory and credential-safe timing. Mock by default."""
import argparse
import asyncio
from collections import Counter
import json
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import httpx
from shapely.geometry import box, mapping

from app.analyses import LimitedProvider, RateGate
from app.config import Settings, load_settings
from app.facilities import analyze_facilities
from app.persistence import atomic_dump
from app.stage_ledger import StageLedger, assert_no_secrets, load_console_inventory, quota_inventory
from life_circle.models import CancelToken, RouteObservation
from life_circle.providers import BaiduProvider

ORIGIN = (121.513926, 31.313077)
LIVE_MAX_OD = 10
ROUNDS = 3


def walking_payload(origin, destination, distance=800, duration=600):
    start = {"lng": str(origin[0]), "lat": str(origin[1])}
    end = {"lng": str(destination[0]), "lat": str(destination[1])}
    return {"status": 0, "result": {"routes": [{
        "distance": distance, "duration": duration,
        "steps": [{"start_location": start, "end_location": end,
                   "path": f"{origin[0]},{origin[1]};{destination[0]},{destination[1]}"}],
    }]}}


def circle_points(n):
    points = []
    for index in range(n):
        angle = 2 * math.pi * index / n
        points.append((round(ORIGIN[0] + 0.0036 * math.cos(angle), 6),
                       round(ORIGIN[1] + 0.0036 * math.sin(angle), 6)))
    return points


def mock_handler(delay=0.04):
    async def handle(request):
        await asyncio.sleep(delay)
        path = request.url.path
        if "place/v3" in path:
            query = request.url.params.get("query")
            rows = []
            if query == "药店":
                dest = circle_points(1)[0]
                rows = [{"uid": "poi-pharmacy-1", "name": "社区药店",
                         "location": {"lng": dest[0], "lat": dest[1]}}]
            return httpx.Response(200, json={"status": 0, "total": len(rows), "results": rows})
        origin = tuple(float(part) for part in reversed(request.url.params["origin"].split(",")))
        destination = tuple(float(part) for part in reversed(request.url.params["destination"].split(",")))
        return httpx.Response(200, json=walking_payload(origin, destination))
    return handle


MOCK_TASK_ID = "stage-baseline-mock"
LIVE_TASK_ID = "stage-baseline-live"


def observations_document(payload, mode):
    return {
        "schemaVersion": "od-observations-v1",
        "label": LIVE_TASK_ID if mode == "live" else MOCK_TASK_ID,
        "observations": payload["observations"],
    }


async def run_mock_walking(n=10, qps=3, delay=0.04):
    ledger = StageLedger(real_network=False, qps=qps, center={"lng": ORIGIN[0], "lat": ORIGIN[1]},
                         task_id=MOCK_TASK_ID)
    destinations = circle_points(n)
    with ledger.attach():
        gate = RateGate(qps)
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler(delay))) as client:
            provider = LimitedProvider(BaiduProvider("offline-fixture", client=client), gate)
            deadline = time.monotonic() + 120
            for destination in destinations:
                await provider.query_walking_time(ORIGIN, destination, deadline)
    summary = ledger.summary()
    assert_no_secrets(summary)
    assert_no_secrets(ledger.events)
    return {"n": n, "summary": summary, "events": ledger.events,
            "observations": ledger.od_observations()}


async def run_mock_facilities(qps=3, delay=0.04):
    ledger = StageLedger(real_network=False, qps=qps, center={"lng": ORIGIN[0], "lat": ORIGIN[1]},
                         task_id=MOCK_TASK_ID)
    geom = box(ORIGIN[0] - 0.01, ORIGIN[1] - 0.01, ORIGIN[0] + 0.01, ORIGIN[1] + 0.01)
    result = SimpleNamespace(
        config=SimpleNamespace(origin=ORIGIN, extent=1600),
        local_geometry=box(-1000, -1000, 1000, 1000),
        geometry=mapping(geom),
        sample_observations=[RouteObservation(ORIGIN, 0)],
    )
    with ledger.attach():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler(delay))) as client:
            facilities, categories, summary, report = await analyze_facilities(
                result, client, "offline-fixture", RateGate(qps), CancelToken())
    payload = ledger.summary()
    assert_no_secrets(payload)
    assert_no_secrets(ledger.events)
    return {
        "summary": payload,
        "events": ledger.events,
        "observations": ledger.od_observations(),
        "facilityCount": len(facilities),
        "categoryCount": len(categories),
        "networkRequests": summary.network_requests,
        "reportChars": len(report),
    }


def live_provenance(qps):
    # The recorded pacing is a local env value, not a console-verified quota.
    return {
        "liveProvider": "baidu/directionlite/v1/walking",
        "routeMetric": "duration",
        "pacingQps": qps,
        "pacingQpsIsLocalOnly": True,
        "quotaSource": "local-env-not-console",
        "consoleCheckRequired": True,
    }


def aggregate_failure_reasons(summaries):
    totals = Counter()
    for row in summaries:
        totals.update(row.get("failureReasons", {}))
    return dict(sorted(totals.items()))


def live_pacing_error(qps, cap):
    if qps is None or qps <= 0:
        return "live mode requires ANALYSIS_QPS or --qps"
    if qps > cap:
        return (f"live pacing {qps} exceeds --live-max-qps {cap}; "
                "confirm console quota before raising")
    return None


async def run_live_walking(n, settings, qps=None, task_id=LIVE_TASK_ID):
    if n > LIVE_MAX_OD:
        raise SystemExit(f"live walking cap is {LIVE_MAX_OD} OD")
    if not settings.ak_configured:
        raise SystemExit("live walking requires BAIDU_MAP_AK")
    qps = settings.analysis_qps if qps is None else qps
    if qps is None:
        raise SystemExit("live walking requires ANALYSIS_QPS or --qps")
    from app.baidu import silence_transport_logs
    silence_transport_logs()
    ledger = StageLedger(real_network=True, qps=qps,
                         center={"lng": ORIGIN[0], "lat": ORIGIN[1]}, task_id=task_id)
    destinations = circle_points(n)
    with ledger.attach():
        gate = RateGate(qps)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            provider = LimitedProvider(
                BaiduProvider(settings.baidu_map_ak.get_secret_value(), client=client), gate)
            deadline = time.monotonic() + 120
            for destination in destinations:
                await provider.query_walking_time(ORIGIN, destination, deadline)
    summary = ledger.summary()
    assert_no_secrets(summary)
    assert_no_secrets(ledger.events)
    return {"n": n, "summary": summary, "observations": ledger.od_observations()}


def pack_rounds(n, qps, summaries):
    walls = [row["wallSeconds"] for row in summaries]
    mean = sum(walls) / len(walls)
    peak = max(walls) - min(walls)
    return {
        "n": n,
        "qps": qps,
        "rounds": len(summaries),
        "wallMean": round(mean, 6),
        "wallMin": min(walls),
        "wallMax": max(walls),
        "wallPeakToPeak": round(peak, 6),
        "relativeSpread": round(peak / mean, 6) if mean else None,
        "retries": [row["retries"] for row in summaries],
        "failures": [row["failures"] for row in summaries],
        "networkAttempts": [row["networkAttempts"] for row in summaries],
        "maxInFlight": max(row["maxInFlight"] for row in summaries),
        "dominantStages": [row["dominantStage"] for row in summaries],
        "summaries": summaries,
    }


def qualified_improvement(walking, *, live):
    return {
        "status": "provisional-from-live" if live else "unset-until-live",
        "rule": ("Any later live optimization must exceed this run's wall peak-to-peak "
                 "on the same N and input; mock spread is noise only and is not a production threshold."),
        "source": "live" if live else "mock",
        "wallPeakToPeakSeconds": walking["wallPeakToPeak"],
        "wallMeanSeconds": walking["wallMean"],
        "relativeSpread": walking["relativeSpread"],
    }


def write_output(path, payload):
    path = Path(path)
    if path.exists():
        raise SystemExit("output_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_secrets(payload)
    atomic_dump(path, payload)


def mock_payload(n, qps, console=None):
    walking_runs = [asyncio.run(run_mock_walking(n=n, qps=qps)) for _ in range(ROUNDS)]
    facility_runs = [asyncio.run(run_mock_facilities(qps=qps)) for _ in range(ROUNDS)]
    walking_rounds = [run["summary"] for run in walking_runs]
    facility_rounds = [run["summary"] for run in facility_runs]
    walking = pack_rounds(n, qps, walking_rounds)
    facilities = pack_rounds(None, qps, facility_rounds)
    return {
        "schemaVersion": "stage-baseline-v2",
        "mode": "mock",
        "realNetworkRequests": 0,
        "inventory": quota_inventory(Settings(_env_file=None), console=console),
        "provenance": {"mode": "mock", "pacingQpsIsLocalOnly": True, "quotaSource": "mock-no-network"},
        "failureReasons": aggregate_failure_reasons(walking_rounds),
        "observations": [record for run in walking_runs + facility_runs for record in run["observations"]],
        "walking": walking,
        "facilities": facilities,
        "qualifiedImprovement": qualified_improvement(walking, live=False),
    }


def live_payload(n, settings, qps=None, task_id=LIVE_TASK_ID, console=None):
    effective = settings.analysis_qps if qps is None else qps
    runs = [asyncio.run(run_live_walking(n, settings, effective, task_id)) for _ in range(ROUNDS)]
    walking_rounds = [run["summary"] for run in runs]
    walking = pack_rounds(n, effective, walking_rounds)
    return {
        "schemaVersion": "stage-baseline-v2",
        "mode": "live",
        "realNetworkRequests": sum(row["realNetworkRequests"] for row in walking_rounds),
        "inventory": quota_inventory(settings, console=console),
        "provenance": live_provenance(effective),
        "failureReasons": aggregate_failure_reasons(walking_rounds),
        "observations": [record for run in runs for record in run["observations"]],
        "walking": walking,
        "qualifiedImprovement": qualified_improvement(walking, live=True),
        "center": {"lng": ORIGIN[0], "lat": ORIGIN[1]},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("inventory", "mock", "live"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--observations-output", type=Path,
                        help="write an od-observations-v1 sidecar for the Stage-2 gate")
    parser.add_argument("--task-id", help="task id recorded on each OD observation")
    parser.add_argument("--console-inventory", type=Path,
                        help="human-verified console-inventory-v1 JSON")
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--qps", type=float, default=None)
    parser.add_argument("--live-max-qps", type=float, default=3.0)
    parser.add_argument("--accept-quota", action="store_true")
    args = parser.parse_args(argv)
    console = load_console_inventory(args.console_inventory) if args.console_inventory else None
    if args.mode == "inventory":
        payload = quota_inventory(Settings(_env_file=None), console=console)
        if args.output:
            write_output(args.output, payload)
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    if args.mode == "mock":
        payload = mock_payload(args.n, 3.0 if args.qps is None else args.qps, console)
        if args.output:
            write_output(args.output, payload)
        if args.observations_output:
            write_output(args.observations_output, observations_document(payload, "mock"))
        print(json.dumps({
            "mode": "mock",
            "rounds": payload["walking"]["rounds"],
            "wallMean": payload["walking"]["wallMean"],
            "wallMin": payload["walking"]["wallMin"],
            "wallMax": payload["walking"]["wallMax"],
            "dominant": payload["walking"]["dominantStages"][0],
            "maxInFlight": payload["walking"]["maxInFlight"],
            "observations": len(payload["observations"]),
            "qualifiedImprovement": payload["qualifiedImprovement"]["status"],
        }, ensure_ascii=False))
        return 0
    if not args.accept_quota:
        raise SystemExit("live mode requires --accept-quota")
    if args.n > LIVE_MAX_OD:
        raise SystemExit(f"live walking cap is {LIVE_MAX_OD} OD")
    if args.qps is not None:
        error = live_pacing_error(args.qps, args.live_max_qps)
        if error:
            raise SystemExit(error)
    settings = load_settings()
    qps = args.qps if args.qps is not None else settings.analysis_qps
    error = live_pacing_error(qps, args.live_max_qps)
    if error:
        raise SystemExit(error)
    payload = live_payload(args.n, settings, qps, args.task_id or LIVE_TASK_ID, console)
    if args.output:
        write_output(args.output, payload)
    if args.observations_output:
        write_output(args.observations_output, observations_document(payload, "live"))
    print(json.dumps({
        "mode": "live",
        "n": payload["walking"]["n"],
        "rounds": payload["walking"]["rounds"],
        "realNetworkRequests": payload["realNetworkRequests"],
        "wallMean": payload["walking"]["wallMean"],
        "dominant": payload["walking"]["dominantStages"][0],
        "maxInFlight": payload["walking"]["maxInFlight"],
        "failureReasons": payload["failureReasons"],
        "observations": len(payload["observations"]),
        "pacingQps": payload["provenance"]["pacingQps"],
        "qualifiedImprovement": payload["qualifiedImprovement"]["status"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
