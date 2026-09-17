"""Stage-2 gate: offline duplicate-OD review. Zero network, never writes cache.

Reads authorized local OD observations and reports the potential cross-task
cache hit rate under the Stage-2 key and cacheability rules. It does not send
requests, does not store credentials and refuses to overwrite existing output.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

from app.persistence import atomic_dump
from app.stage_ledger import assert_no_secrets, od_cache_key, observation_cacheable

OBSERVATION_SCHEMA = "od-observations-v1"
REVIEW_SCHEMA = "od-cache-review-v1"
REQUIRED = ("provider", "api_version", "metric", "coord_system", "origin", "destination")
MAX_UID_SAMPLES = 5


def _point(value, field):
    if isinstance(value, dict):
        value = (value.get("lng"), value.get("lat"))
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field} must be [lng, lat]")
    out = []
    for component in value:
        if type(component) not in (int, float):
            raise ValueError(f"{field} components must be numbers")
        out.append(float(component))
    return tuple(out)


def _text(record, field, *, required=True):
    value = record.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if not required:
            return ""
        raise ValueError(f"{field} must be a non-empty string")
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value.strip()


def normalize_observations(raw):
    if isinstance(raw, dict) and "observations" in raw:
        schema = raw.get("schemaVersion")
        if schema not in (None, OBSERVATION_SCHEMA):
            raise ValueError(f"unsupported schemaVersion {schema!r}")
        raw = raw["observations"]
    if not isinstance(raw, list):
        raise ValueError("input must be a JSON array or an object with observations")
    observations = []
    for index, record in enumerate(raw):
        if not isinstance(record, dict):
            raise ValueError(f"observation {index} must be an object")
        missing = [field for field in REQUIRED if not record.get(field)]
        if missing:
            raise ValueError(f"observation {index} missing {', '.join(missing)}")
        observations.append({
            "provider": _text(record, "provider"),
            "api_version": _text(record, "api_version"),
            "metric": _text(record, "metric"),
            "coord_system": _text(record, "coord_system"),
            "origin": _point(record["origin"], f"observation {index} origin"),
            "destination": _point(record["destination"], f"observation {index} destination"),
            "origin_uid": _text(record, "origin_uid", required=False),
            "destination_uid": _text(record, "destination_uid", required=False),
            "task_id": _text(record, "task_id", required=False),
            "reason": record.get("reason"),
            "endpoint_verified": record.get("endpoint_verified"),
        })
    return observations


def load_observations(path):
    text = Path(path).read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return normalize_observations([json.loads(line) for line in text.splitlines() if line.strip()])
    return normalize_observations(json.loads(text))


def _key(record):
    return od_cache_key(
        provider=record["provider"], api_version=record["api_version"], metric=record["metric"],
        coord_system=record["coord_system"], origin=record["origin"], destination=record["destination"],
        origin_uid=record["origin_uid"], destination_uid=record["destination_uid"],
    )


def _rate(redundant, total):
    return round(redundant / total, 6) if total else None


def _summary(keys, task_ids=None):
    counts = Counter(keys)
    redundant = sum(count - 1 for count in counts.values())
    summary = {
        "observations": len(keys),
        "uniqueKeys": len(counts),
        "duplicateObservations": redundant,
        "potentialHitRate": _rate(redundant, len(keys)),
        "maxMultiplicity": max(counts.values()) if counts else 0,
    }
    if task_ids is not None:
        tasks_by_key = defaultdict(set)
        for key, task_id in zip(keys, task_ids):
            if task_id:
                tasks_by_key[key].add(task_id)
        reused = [key for key, tasks in tasks_by_key.items() if len(tasks) > 1]
        cross = sum(counts[key] - len(tasks_by_key[key]) for key in reused)
        summary["crossTaskKeys"] = len(reused)
        summary["crossTaskDuplicateObservations"] = cross
        summary["crossTaskPotentialHitRate"] = _rate(cross, len(keys))
    return summary


def review(observations, *, label=None):
    observations = normalize_observations(observations)
    keys = [_key(record) for record in observations]
    cacheable = [observation_cacheable(record["reason"], record["endpoint_verified"]) for record in observations]
    cacheable_keys = [key for key, ok in zip(keys, cacheable) if ok]
    excluded = Counter(record["reason"] for record in observations
                       if not observation_cacheable(record["reason"], record["endpoint_verified"])
                       and record["reason"] is not None)
    endpoint_unverified = sum(1 for record, ok in zip(observations, cacheable)
                              if not ok and record["reason"] is None)
    if endpoint_unverified:
        excluded["endpoint_unverified"] = endpoint_unverified

    coordinate_groups = defaultdict(set)
    for record, key in zip(observations, keys):
        identity = (record["provider"], record["api_version"], record["metric"], record["coord_system"],
                    record["origin"], record["destination"])
        coordinate_groups[identity].add(key)
    variants = [identity for identity, group in coordinate_groups.items() if len(group) > 1]
    samples = []
    for identity in variants[:MAX_UID_SAMPLES]:
        provider, api_version, metric, coord_system, origin, destination = identity
        group = [record for record, key in zip(observations, keys) if key in coordinate_groups[identity]]
        samples.append({
            "provider": provider, "apiVersion": api_version, "metric": metric, "coordSystem": coord_system,
            "origin": list(origin), "destination": list(destination),
            "uids": sorted({(record["origin_uid"], record["destination_uid"]) for record in group}),
        })

    return {
        "schemaVersion": REVIEW_SCHEMA,
        "label": label,
        "networkRequests": 0,
        "totals": _summary(keys, [record["task_id"] for record in observations]),
        "cacheable": {
            **_summary(cacheable_keys, [record["task_id"] for record, ok in zip(observations, cacheable) if ok]),
            "excludedReasons": dict(sorted(excluded.items())),
        },
        "uidVariantGroups": {"groups": len(variants), "samples": samples},
        "byMetric": dict(sorted(Counter(record["metric"] for record in observations).items())),
    }


def write_output(path, payload):
    path = Path(path)
    if path.exists():
        raise SystemExit("output_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_secrets(payload)
    atomic_dump(path, payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="od-observations-v1 JSON/JSONL keeping coordinates but no credentials")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", help="provenance label, e.g. production-ledger / synthetic-example")
    args = parser.parse_args(argv)
    payload = review(load_observations(args.input), label=args.label)
    assert_no_secrets(payload)
    if args.output:
        write_output(args.output, payload)
    print(json.dumps({
        "schemaVersion": payload["schemaVersion"],
        "label": payload["label"],
        "networkRequests": payload["networkRequests"],
        "totals": payload["totals"],
        "cacheable": {
            "observations": payload["cacheable"]["observations"],
            "potentialHitRate": payload["cacheable"]["potentialHitRate"],
            "excludedReasons": payload["cacheable"]["excludedReasons"],
        },
        "uidVariantGroups": payload["uidVariantGroups"]["groups"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
