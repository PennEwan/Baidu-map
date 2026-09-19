"""Stage-2 step 1: export authorized local OD details as od-observations-v1.

Zero network. Reads a completed real-run artifact that is already in the repo
or provided locally, keeps only origin/destination/reason/endpoint fields and
never writes credentials, URLs or headers. Output refuses to overwrite.
"""
import argparse
import json
import math
from pathlib import Path
import sys

from tools.od_cache_review import write_output

PROVIDER = "baidu"
API_VERSION = "directionlite/v1/walking"
METRIC = "duration"
COORD_SYSTEM = "bd09ll"


def _point(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must be [lng, lat]")
    out = []
    for component in value:
        if type(component) not in (int, float) or not math.isfinite(component):
            raise ValueError(f"{label} components must be finite numbers")
        out.append(float(component))
    return out


def _duration(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return float(value)


def export_boundary_reference_v2(payload):
    """Frozen boundary reference v2: pairs carry real walking durations.

    The frozen file does not record per-point endpoint verification, so the
    export sets endpoint_verified to null; the Stage-2 rule then treats every
    record as uncacheable until the original evidence is supplied.
    """
    if not isinstance(payload, dict):
        raise ValueError("boundary reference payload must be an object")
    origin = _point(payload.get("origin"), "origin")
    pairs = payload.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("pairs must be a non-empty list")
    observations = []
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("pair must be an object")
        points, durations = pair.get("points"), pair.get("durations")
        if not isinstance(points, list) or not isinstance(durations, list) or len(points) != len(durations):
            raise ValueError(f"pair {pair.get('id')} points/durations mismatch")
        for point, duration in zip(points, durations):
            observations.append({
                "provider": PROVIDER,
                "api_version": API_VERSION,
                "metric": METRIC,
                "coord_system": COORD_SYSTEM,
                "origin": origin,
                "destination": _point(point, f"pair {pair.get('id')} point"),
                "origin_uid": "",
                "destination_uid": "",
                "task_id": "boundary-reference-v2",
                "reason": None,
                "endpoint_verified": None,
                "duration_s": _duration(duration, f"pair {pair.get('id')} duration"),
            })
    return observations


FORMATS = {"boundary-reference-v2": export_boundary_reference_v2}


def export(source, *, fmt):
    if fmt not in FORMATS:
        raise ValueError(f"unsupported format {fmt!r}")
    observations = FORMATS[fmt](source)
    return {
        "schemaVersion": "od-observations-v1",
        "note": "已授权本地真实运行的坐标/耗时明细；不含 AK、URL 与请求头。",
        "sourceFormat": fmt,
        "observations": observations,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", required=True, choices=sorted(FORMATS))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source = json.loads(args.input.read_text(encoding="utf-8"))
    payload = export(source, fmt=args.format)
    write_output(args.output, payload)
    print(json.dumps({
        "schemaVersion": payload["schemaVersion"],
        "sourceFormat": payload["sourceFormat"],
        "observations": len(payload["observations"]),
        "output": args.output.name,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
