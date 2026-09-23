"""Bounded live QPS probe for the walking direction endpoint.

`plan` is zero-request. `live` requires --accept-quota and a frozen ladder of
QPS levels, each sending a short single-flight burst at 1/level spacing. It
stops at the first rate_limit/quota/permission and reports the highest fully
successful level. Output contains only counts, reasons and timings; never AK,
URLs or headers.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

import httpx

from app.analyses import LimitedProvider, RateGate
from app.baidu import silence_transport_logs
from app.config import load_settings
from app.stage_ledger import assert_no_secrets
from life_circle.providers import BaiduProvider
from tools.od_cache_review import write_output

PROBE_SCHEMA = "qps-probe-v1"
PLAN_SCHEMA = "qps-probe-plan-v1"
PROVIDER_LABEL = "baidu/directionlite/v1/walking"
ORIGIN = (121.513926, 31.313077)
RADIUS_DEG = 0.0036
DEFAULT_LEVELS = (1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0)
ABORT_REASONS = frozenset({"rate_limit", "quota", "permission", "auth",
                           "configuration", "invalid_parameter"})
MAX_AUTHORIZED_REQUESTS = 60
MAX_AUTHORIZED_QPS = 20.0
MAX_AUTHORIZED_CONCURRENCY = 3
PER_REQUEST_DEADLINE_S = 20.0


def parse_levels(text):
    levels = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            raise SystemExit("levels must be comma-separated numbers") from None
        if value <= 0:
            raise SystemExit("levels must be positive")
        levels.append(value)
    if not levels:
        raise SystemExit("at least one level required")
    return tuple(levels)


def destinations(count):
    points = []
    for index in range(count):
        angle = 2 * math.pi * index / count
        points.append((round(ORIGIN[0] + RADIUS_DEG * math.cos(angle), 6),
                       round(ORIGIN[1] + RADIUS_DEG * math.sin(angle), 6)))
    return points


def ladder(levels, budget):
    rows, used = [], 0
    for level in levels:
        burst = min(int(math.ceil(level)), max(budget - used, 0))
        if burst <= 0:
            break
        rows.append({"qps": level, "plannedRequests": burst})
        used += burst
    return rows


def plan_document(levels, budget, max_qps):
    rows = ladder(levels, budget)
    return {
        "schemaVersion": PLAN_SCHEMA,
        "note": "零请求计划；live 需 --accept-quota，遇 rate_limit/quota/permission 立即停止。",
        "provider": PROVIDER_LABEL,
        "origin": list(ORIGIN),
        "levels": rows,
        "estimatedRequests": sum(row["plannedRequests"] for row in rows),
        "budget": budget,
        "maxQps": max_qps,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }


def max_in_one_second(times):
    return max((sum(start <= value < start + 1 for value in times) for start in times), default=0)


async def run_level(client, ak, level, planned, points, offset, concurrency):
    """One burst at 1/level dispatch spacing. concurrency=1 stays single-flight;
    higher values allow that many requests in flight so the target rate is not
    capped by response latency."""
    targets = [points[(offset + index) % len(points)] for index in range(planned)]
    if concurrency <= 1:
        gate = RateGate(level)
        provider = LimitedProvider(BaiduProvider(ak, client=client), gate)
        observations, dispatch_times = [], []
        for target in targets:
            dispatch_times.append(time.perf_counter())
            observed = await provider.query_walking_time(
                tuple(ORIGIN), target, time.monotonic() + PER_REQUEST_DEADLINE_S)
            observations.append(observed)
            if observed.reason in ABORT_REASONS:
                break
        return observations, dispatch_times
    semaphore = asyncio.Semaphore(concurrency)
    dispatch_times = []

    async def one(target):
        async with semaphore:
            dispatch_times.append(time.perf_counter())
            return await BaiduProvider(ak, client=client).query_walking_time(
                tuple(ORIGIN), target, time.monotonic() + PER_REQUEST_DEADLINE_S)

    interval = 1.0 / level
    next_start = 0.0
    tasks = []
    for target in targets:
        now = time.perf_counter()
        if now < next_start:
            await asyncio.sleep(next_start - now)
        next_start = max(time.perf_counter(), next_start) + interval
        tasks.append(asyncio.create_task(one(target)))
    return await asyncio.gather(*tasks), dispatch_times


async def run_probe(settings, *, levels, budget, transport=None, concurrency=1):
    ak = settings.baidu_map_ak.get_secret_value()
    points = destinations(int(math.ceil(max(levels))) + 1)
    rows, total, ceiling, stop_reason = [], 0, None, None
    started = time.perf_counter()
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=transport) as client:
        for level in levels:
            planned = min(int(math.ceil(level)), budget - total)
            if planned <= 0:
                stop_reason = "budget"
                break
            level_started = time.perf_counter()
            observations, dispatch_times = await run_level(
                client, ak, level, planned, points, total, concurrency)
            sent = len(observations)
            total += sent
            wall = time.perf_counter() - level_started
            outcomes = Counter(str(observed.reason) for observed in observations)
            ok = sum(1 for observed in observations if observed.reason is None)
            durations = [observed.duration for observed in observations
                         if observed.reason is None and observed.duration is not None]
            abort = next((observed.reason for observed in observations
                          if observed.reason in ABORT_REASONS), None)
            row = {
                "qps": level,
                "concurrency": concurrency,
                "plannedRequests": planned,
                "sent": sent,
                "ok": ok,
                "outcomes": dict(sorted(outcomes.items())),
                "durationMeanSeconds": round(sum(durations) / len(durations), 6) if durations else None,
                "wallSeconds": round(wall, 6),
                "achievedQps": round(sent / wall, 3) if wall > 0 else None,
                "maxInOneSecond": max_in_one_second(dispatch_times),
                "passed": abort is None and sent == planned and ok == sent,
            }
            rows.append(row)
            if abort is not None:
                stop_reason = abort
                break
            if not row["passed"]:
                stop_reason = "incomplete"
                break
            ceiling = level
    payload = {
        "schemaVersion": PROBE_SCHEMA,
        "label": "qps-probe-live",
        "provider": PROVIDER_LABEL,
        "qpsIsLocalOnly": True,
        "concurrency": concurrency,
        "note": "pacing 上限探测；observedCeilingQps 是最后整档通过的值，不是控制台额度。",
        "levels": rows,
        "observedCeilingQps": ceiling,
        "stopReason": stop_reason,
        "realNetworkRequests": total,
        "wallSeconds": round(time.perf_counter() - started, 6),
        "finishedUtc": datetime.now(timezone.utc).isoformat(),
    }
    assert_no_secrets(payload)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("plan", "live"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--levels", default=",".join(str(int(v)) if v.is_integer() else str(v)
                                                      for v in DEFAULT_LEVELS))
    parser.add_argument("--budget", type=int, default=MAX_AUTHORIZED_REQUESTS)
    parser.add_argument("--max-qps", type=float, default=MAX_AUTHORIZED_QPS)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--accept-quota", action="store_true")
    args = parser.parse_args(argv)
    levels = parse_levels(args.levels)
    if not 0 < args.budget <= MAX_AUTHORIZED_REQUESTS:
        raise SystemExit(f"--budget must be in (0, {MAX_AUTHORIZED_REQUESTS}]")
    if not 0 < args.max_qps <= MAX_AUTHORIZED_QPS:
        raise SystemExit(f"--max-qps must be in (0, {MAX_AUTHORIZED_QPS}]")
    if not 0 < args.concurrency <= MAX_AUTHORIZED_CONCURRENCY:
        raise SystemExit(f"--concurrency must be in (0, {MAX_AUTHORIZED_CONCURRENCY}]")
    if any(level > args.max_qps for level in levels):
        raise SystemExit(f"level exceeds --max-qps {args.max_qps}")
    if args.mode == "plan":
        document = plan_document(levels, args.budget, args.max_qps)
        write_output(args.output, document)
        print(json.dumps({"mode": "plan", "levels": len(document["levels"]),
                          "estimatedRequests": document["estimatedRequests"],
                          "output": args.output.name}, ensure_ascii=False))
        return 0
    if not args.accept_quota:
        raise SystemExit("live mode requires --accept-quota")
    settings = load_settings()
    if not settings.ak_configured:
        raise SystemExit("live mode requires BAIDU_MAP_AK")
    silence_transport_logs()
    payload = asyncio.run(run_probe(settings, levels=levels, budget=args.budget,
                                    concurrency=args.concurrency))
    write_output(args.output, payload)
    print(json.dumps({"mode": "live", "observedCeilingQps": payload["observedCeilingQps"],
                      "stopReason": payload["stopReason"],
                      "realNetworkRequests": payload["realNetworkRequests"],
                      "output": args.output.name}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
