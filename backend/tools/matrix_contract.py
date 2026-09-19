"""Stage-3: offline matrix-vs-single contract review. Zero network.

Compares paired directionlite (single) and routematrix (matrix) raw numbers and
records what a matrix response cannot prove: endpoint evidence, route choice,
zero/missing parity. A matrix input carrying endpointVerified is rejected at
parse time instead of being silently trusted. Only a paired-live label can pass
the gate.
"""
import argparse
import json
import math
from pathlib import Path
import sys

from tools.od_cache_review import write_output

CASES_SCHEMA = "matrix-contract-cases-v1"
REVIEW_SCHEMA = "matrix-contract-review-v1"
GATE_LABEL = "paired-live"
DEFAULT_TOLERANCES = {
    "distanceAbsM": 50.0,
    "distanceRel": 0.05,
    "durationAbsS": 30.0,
    "durationRel": 0.10,
}
NOTES = [
    "矩阵响应没有 steps/path/实际端点；任何 matrix endpointVerified 输入都会在解析阶段被拒绝。",
    "矩阵数值为 0 或缺失一律记 unknown，不得当可达或覆盖证据。",
    "只有 label=paired-live 的成对真实运行可以过门禁；合成/历史标签仅供链路验证。",
]


def _point(value, label):
    if isinstance(value, dict):
        value = (value.get("lng"), value.get("lat"))
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must be [lng, lat]")
    out = []
    for component in value:
        if type(component) not in (int, float) or not math.isfinite(component):
            raise ValueError(f"{label} components must be finite numbers")
        out.append(float(component))
    return out


def _raw(value, label):
    """Return (raw, usable). 0 or missing means no matrix route, not coverage."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None, False
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a non-negative finite number")
    return float(value), value > 0


def normalize_side(record, label, *, allow_endpoint):
    if record is None:
        return None
    if not isinstance(record, dict):
        raise ValueError(f"{label} must be an object or null")
    if "endpointVerified" in record and not allow_endpoint:
        raise ValueError(f"{label} must not carry endpoint evidence")
    distance, distance_usable = _raw(record.get("distance"), f"{label} distance")
    duration, duration_usable = _raw(record.get("duration"), f"{label} duration")
    side = {
        "distance": distance, "duration": duration,
        "distanceUsable": distance_usable, "durationUsable": duration_usable,
    }
    if allow_endpoint:
        verified = record.get("endpointVerified")
        side["endpointVerified"] = verified if verified is None else bool(verified)
    return side


def normalize_case(case, index):
    if not isinstance(case, dict):
        raise ValueError(f"case {index} must be an object")
    if not case.get("id"):
        raise ValueError(f"case {index} missing id")
    return {
        "id": str(case["id"]),
        "origin": _point(case.get("origin"), f"case {index} origin"),
        "destination": _point(case.get("destination"), f"case {index} destination"),
        "destinationUid": (case.get("destinationUid") or "").strip(),
        "single": normalize_side(case.get("single"), f"case {index} single", allow_endpoint=True),
        "matrix": normalize_side(case.get("matrix"), f"case {index} matrix", allow_endpoint=False),
    }


def _usable(side):
    return bool(side) and side["distanceUsable"] and side["durationUsable"]


def check_case(case, tolerances):
    single, matrix = case["single"], case["matrix"]
    check = {
        "id": case["id"],
        "origin": case["origin"],
        "destination": case["destination"],
        "destinationUid": case["destinationUid"],
        "single": single,
        "matrix": matrix,
        "notes": [],
    }
    if matrix is None:
        check["verdict"] = "matrix_missing"
        return check
    if not _usable(matrix):
        check["verdict"] = "matrix_unknown"
        check["notes"].append("矩阵 0/缺失：unknown，不是覆盖。")
        return check
    if not _usable(single):
        check["verdict"] = "single_missing"
        return check
    distance_delta = round(matrix["distance"] - single["distance"], 6)
    duration_delta = round(matrix["duration"] - single["duration"], 6)
    distance_within = abs(distance_delta) <= max(tolerances["distanceAbsM"],
                                                 tolerances["distanceRel"] * single["distance"])
    duration_within = abs(duration_delta) <= max(tolerances["durationAbsS"],
                                                 tolerances["durationRel"] * single["duration"])
    route_divergence = (distance_delta > tolerances["distanceAbsM"]
                        and -duration_delta > tolerances["durationAbsS"])
    check["deltas"] = {"distanceM": distance_delta, "durationS": duration_delta}
    check["distanceWithin"] = distance_within
    check["durationWithin"] = duration_within
    check["routeDivergence"] = route_divergence
    if single.get("endpointVerified") is not True:
        check["notes"].append("单点缺端点证据，数值相同也不能证明业务等价。")
    check["verdict"] = "match" if (distance_within and duration_within
                                   and not route_divergence) else "mismatch"
    return check


def review(document, *, label=None):
    if not isinstance(document, dict):
        raise ValueError("input must be an object")
    if document.get("schemaVersion") not in (None, CASES_SCHEMA):
        raise ValueError(f"unsupported schemaVersion {document.get('schemaVersion')!r}")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases must be a non-empty list")
    tolerances = {**DEFAULT_TOLERANCES, **document.get("tolerances", {})}
    for key in tolerances:
        if key not in DEFAULT_TOLERANCES or not isinstance(tolerances[key], (int, float)) or tolerances[key] < 0:
            raise ValueError(f"invalid tolerance {key!r}")
    cases = [normalize_case(case, index) for index, case in enumerate(raw_cases)]
    checks = [check_case(case, tolerances) for case in cases]
    verdicts = [check["verdict"] for check in checks]
    matches = verdicts.count("match")
    mismatches = verdicts.count("mismatch")
    divergences = sum(1 for check in checks if check.get("routeDivergence"))
    single_unverified = sum(1 for check in checks
                            if check["single"] and check["single"].get("endpointVerified") is not True)
    if not matches and not mismatches:
        verdict = "not_run"
    elif mismatches or divergences:
        verdict = "incompatible"
    else:
        verdict = "compatible"
    return {
        "schemaVersion": REVIEW_SCHEMA,
        "label": label,
        "networkRequests": 0,
        "gateEligible": label == GATE_LABEL,
        "verdict": verdict,
        "tolerances": tolerances,
        "totals": {
            "cases": len(checks),
            "matches": matches,
            "mismatches": mismatches,
            "matrixUnknown": verdicts.count("matrix_unknown") + verdicts.count("matrix_missing"),
            "singleUnknown": verdicts.count("single_missing"),
            "routeDivergences": divergences,
            "uidBoundCases": sum(1 for case in cases if case["destinationUid"]),
            "singleEndpointUnverified": single_unverified,
        },
        "checks": checks,
        "notes": NOTES + (["label 不是 paired-live，本次结果不能过门禁。"] if label != GATE_LABEL else []),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", help=f"provenance label; only {GATE_LABEL} can pass the gate")
    args = parser.parse_args(argv)
    document = json.loads(args.input.read_text(encoding="utf-8"))
    payload = review(document, label=args.label)
    if args.output:
        write_output(args.output, payload)
    print(json.dumps({
        "schemaVersion": payload["schemaVersion"],
        "label": payload["label"],
        "networkRequests": payload["networkRequests"],
        "gateEligible": payload["gateEligible"],
        "totals": payload["totals"],
        "notes": payload["notes"] if not payload["gateEligible"] else [],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
