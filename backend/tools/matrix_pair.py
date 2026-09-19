"""Stage-3 paired collector: one directionlite call and one routematrix call per
frozen OD set. `plan` is zero-request; `live` requires --accept-quota, a frozen
plan file and a pacing cap. Writes only raw values plus credential-free metadata;
never stores AK, URLs or headers.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import httpx

from app.analyses import LimitedProvider, RateGate
from app.config import load_settings
from app.persistence import atomic_dump
from app.stage_ledger import StageLedger, assert_no_secrets
from life_circle.providers import BaiduProvider
from tools.od_cache_review import write_output

PLAN_SCHEMA = "matrix-pair-plan-v1"
CASES_SCHEMA = "matrix-contract-cases-v1"
COLLECTION_SCHEMA = "matrix-pair-collection-v1"
MATRIX_ENDPOINT = "https://api.map.baidu.com/routematrix/v2/walking"
ORIGIN = (121.513926, 31.313077)
RADIUS_DEG = 0.0036
ROUTE_METRIC = "distance"
COORD_SYSTEM = "bd09ll"
ABORT_REASONS = frozenset({"rate_limit", "quota", "permission", "invalid_parameter",
                           "configuration", "auth"})
RETRY_REASONS = frozenset({"temporary", "timeout"})
MAX_SINGLE_ATTEMPTS = 2


def fixed_points(n=5):
    points = []
    for index in range(n):
        angle = 2 * math.pi * index / n
        points.append((round(ORIGIN[0] + RADIUS_DEG * math.cos(angle), 6),
                       round(ORIGIN[1] + RADIUS_DEG * math.sin(angle), 6)))
    return points


def plan_document(n=5):
    cases = [{
        "id": f"od-{index + 1}",
        "origin": list(ORIGIN),
        "destination": list(point),
        "destinationUid": "",
    } for index, point in enumerate(fixed_points(n))]
    document = {
        "schemaVersion": PLAN_SCHEMA,
        "note": "冻结的成对 OD 计划；live 前不得改点。可在单个用例补 destinationUid 后重新冻结。",
        "center": {"lng": ORIGIN[0], "lat": ORIGIN[1]},
        "routeMetric": ROUTE_METRIC,
        "coordSystem": COORD_SYSTEM,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "cases": cases,
    }
    document["planHash"] = plan_hash(cases)
    return document


def plan_hash(cases):
    canonical = json.dumps(cases, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_plan(document):
    if not isinstance(document, dict) or document.get("schemaVersion") != PLAN_SCHEMA:
        raise ValueError("unsupported plan document")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("plan cases must be a non-empty list")
    normalized = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or not case.get("id"):
            raise ValueError(f"case {index} missing id")
        normalized.append({
            "id": str(case["id"]),
            "origin": _point(case.get("origin"), f"case {index} origin"),
            "destination": _point(case.get("destination"), f"case {index} destination"),
            "destinationUid": str(case.get("destinationUid") or "").strip(),
        })
    expected = plan_hash(normalized)
    if document.get("planHash") not in (None, expected):
        raise ValueError("plan hash mismatch")
    if len({tuple(case["origin"]) for case in normalized}) != 1:
        raise ValueError("plan must share one origin for the matrix batch")
    return {**document, "cases": normalized, "planHash": expected}


def _point(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must be [lng, lat]")
    out = []
    for component in value:
        if type(component) not in (int, float) or not math.isfinite(component):
            raise ValueError(f"{label} components must be finite numbers")
        out.append(float(component))
    return out


def matrix_point(point, uid=""):
    lat, lng = point[1], point[0]
    return f"{lat:.6f},{lng:.6f}" + (f";{uid}" if uid else "")


def matrix_value(cell, key):
    value = cell.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid {key}")
    return float(value)


def parse_matrix_payload(payload, count):
    if not isinstance(payload, dict) or type(payload.get("status")) is not int:
        return {"status": None, "cells": [None] * count, "error": "invalid_response"}
    status = payload["status"]
    if status != 0:
        return {"status": status, "cells": [None] * count, "error": "upstream_status"}
    result = payload.get("result")
    if not isinstance(result, list) or len(result) != count:
        return {"status": status, "cells": [None] * count, "error": "invalid_response"}
    cells = []
    for cell in result:
        if not isinstance(cell, dict):
            cells.append(None)
            continue
        try:
            cells.append({"distance": matrix_value(cell, "distance"),
                          "duration": matrix_value(cell, "duration")})
        except ValueError:
            cells.append(None)
    return {"status": status, "cells": cells, "error": None}


def build_cases(plan, singles, cells):
    cases = []
    for index, item in enumerate(plan["cases"]):
        single = singles[index] if index < len(singles) else None
        cell = cells[index] if index < len(cells) else None
        cases.append({
            "id": item["id"],
            "origin": list(item["origin"]),
            "destination": list(item["destination"]),
            "destinationUid": item["destinationUid"],
            "single": None if single is None else {
                "distance": single["distance"],
                "duration": single["duration"],
                "endpointVerified": single["endpointVerified"],
            },
            "matrix": None if cell is None else {
                "distance": cell["distance"],
                "duration": cell["duration"],
            },
        })
    return {
        "schemaVersion": CASES_SCHEMA,
        "label": "paired-live",
        "planHash": plan["planHash"],
        "note": "真实成对调用原始数值；不含 AK、URL 或请求头。",
        "cases": cases,
    }


async def fetch_matrix(client, plan, ak):
    origin = matrix_point(plan["cases"][0]["origin"])
    destinations = "|".join(matrix_point(item["destination"], item["destinationUid"])
                            for item in plan["cases"])
    params = {
        "ak": ak, "origins": origin, "destinations": destinations,
        "coord_type": COORD_SYSTEM, "output": "json", "ret_straight_dist": "0",
    }
    started = time.perf_counter()
    try:
        response = await client.get(MATRIX_ENDPOINT, params=params, timeout=8)
    except httpx.TimeoutException:
        return {"httpStatus": None, "status": None, "cells": [None] * len(plan["cases"]),
                "error": "timeout", "seconds": round(time.perf_counter() - started, 6)}
    except httpx.RequestError:
        return {"httpStatus": None, "status": None, "cells": [None] * len(plan["cases"]),
                "error": "temporary", "seconds": round(time.perf_counter() - started, 6)}
    elapsed = round(time.perf_counter() - started, 6)
    if response.status_code != 200:
        reason = {429: "rate_limit", 401: "permission", 403: "permission", 400: "invalid_parameter"}.get(
            response.status_code, "http_error")
        return {"httpStatus": response.status_code, "status": None,
                "cells": [None] * len(plan["cases"]), "error": reason, "seconds": elapsed}
    try:
        payload = response.json()
    except ValueError:
        return {"httpStatus": 200, "status": None, "cells": [None] * len(plan["cases"]),
                "error": "invalid_response", "seconds": elapsed}
    parsed = parse_matrix_payload(payload, len(plan["cases"]))
    return {"httpStatus": 200, "seconds": elapsed, **parsed}


async def run_paired(plan, settings, *, qps, transport=None, task_id="matrix-pair"):
    plan = validate_plan(plan)
    ledger = StageLedger(real_network=True, qps=qps, center=plan.get("center"), task_id=task_id)
    ak = settings.baidu_map_ak.get_secret_value()
    matrix_weight = len(plan["cases"])
    collection = {
        "schemaVersion": COLLECTION_SCHEMA,
        "label": "paired-live",
        "planHash": plan["planHash"],
        "routeMetric": plan.get("routeMetric", ROUTE_METRIC),
        "qps": qps,
        "matrixWeight": matrix_weight,
        "singles": [],
        "matrix": None,
        "stopReason": None,
        "ledger": None,
    }
    singles = []
    cells = [None] * matrix_weight
    with ledger.attach():
        gate = RateGate(qps)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=transport) as client:
            stop_reason = None
            for item in plan["cases"]:
                observed = None
                for attempt in range(MAX_SINGLE_ATTEMPTS):
                    provider = LimitedProvider(BaiduProvider(
                        ak, client=client,
                        destination_uid=item["destinationUid"] or None,
                        route_metric=ROUTE_METRIC), gate)
                    observed = await provider.query_walking_time(
                        tuple(item["origin"]), tuple(item["destination"]), time.monotonic() + 20)
                    if observed.reason not in RETRY_REASONS:
                        break
                reason = observed.reason if observed else "interrupted"
                singles.append({
                    "id": item["id"],
                    "reason": reason,
                    "endpointVerified": bool(observed.endpoint_verified) if observed else False,
                    "distance": observed.distance_m if observed else None,
                    "duration": observed.duration if observed else None,
                })
                if reason in ABORT_REASONS:
                    stop_reason = reason
                    break
            if stop_reason is None:
                deadline = time.monotonic() + 60
                for _ in range(matrix_weight):
                    if not await gate.wait(deadline):
                        stop_reason = "deadline"
                        break
            if stop_reason is None:
                matrix = await fetch_matrix(client, plan, ak)
                cells = matrix["cells"]
                collection["matrix"] = {key: matrix[key] for key in
                                        ("httpStatus", "status", "error", "seconds")}
                if matrix["error"] in ABORT_REASONS or matrix["error"] == "upstream_status":
                    stop_reason = matrix["error"]
            collection["stopReason"] = stop_reason
    collection["ledger"] = ledger.summary()
    collection["singleRequests"] = collection["ledger"]["realNetworkRequests"]
    collection["matrixHttpRequests"] = 1 if collection["matrix"] else 0
    collection["realNetworkRequests"] = collection["singleRequests"] + collection["matrixHttpRequests"]
    singles_for_cases = []
    for row in singles:
        if row["reason"] is None and (row["distance"] is not None or row["duration"] is not None):
            singles_for_cases.append(row)
        else:
            singles_for_cases.append(None)
    while len(singles_for_cases) < matrix_weight:
        singles_for_cases.append(None)
    cases = build_cases(plan, singles_for_cases, cells)
    assert_no_secrets(collection)
    assert_no_secrets(cases)
    return {"cases": cases, "collection": collection}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("plan", "live"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--collection-output", type=Path)
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--qps", type=float, default=3.0)
    parser.add_argument("--live-max-qps", type=float, default=3.0)
    parser.add_argument("--accept-quota", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "plan":
        document = plan_document(args.n)
        write_output(args.output, document)
        print(json.dumps({
            "mode": "plan", "cases": len(document["cases"]),
            "planHash": document["planHash"], "output": args.output.name,
        }, ensure_ascii=False))
        return 0
    if not args.accept_quota:
        raise SystemExit("live mode requires --accept-quota")
    if args.plan is None:
        raise SystemExit("live mode requires a frozen --plan file")
    if args.qps <= 0 or args.qps > args.live_max_qps:
        raise SystemExit(f"live pacing {args.qps} must be in (0, --live-max-qps {args.live_max_qps}]")
    plan = validate_plan(json.loads(args.plan.read_text(encoding="utf-8")))
    settings = load_settings()
    if not settings.ak_configured:
        raise SystemExit("live mode requires BAIDU_MAP_AK")
    from app.baidu import silence_transport_logs
    silence_transport_logs()
    result = asyncio.run(run_paired(plan, settings, qps=args.qps, task_id="matrix-pair-live"))
    write_output(args.output, result["cases"])
    if args.collection_output:
        write_output(args.collection_output, result["collection"])
    print(json.dumps({
        "mode": "live",
        "cases": len(result["cases"]["cases"]),
        "planHash": plan["planHash"],
        "realNetworkRequests": result["collection"]["realNetworkRequests"],
        "stopReason": result["collection"]["stopReason"],
        "matrixError": result["collection"]["matrix"]["error"] if result["collection"]["matrix"] else None,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
