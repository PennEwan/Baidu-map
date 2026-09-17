import json

import pytest

from app.stage_ledger import assert_no_secrets
from tools.od_cache_review import load_observations, main, normalize_observations, review

ORIGIN = (121.513926, 31.313077)
NEAR = (121.517526, 31.313077)
FAR = (121.510326, 31.313077)


def obs(destination, *, destination_uid="", task_id="t1", metric="duration", reason=None,
        endpoint_verified=True, provider="baidu", api_version="directionlite/v1/walking",
        coord_system="bd09ll", origin=ORIGIN):
    return {
        "provider": provider, "api_version": api_version, "metric": metric,
        "coord_system": coord_system, "origin": origin, "destination": destination,
        "destination_uid": destination_uid, "task_id": task_id,
        "reason": reason, "endpoint_verified": endpoint_verified,
    }


def test_review_counts_duplicates_and_keeps_uid_variants_apart():
    payload = review([
        obs(NEAR, task_id="t1"),
        obs(NEAR, task_id="t2"),
        obs(NEAR, task_id="t2"),
        obs(NEAR, destination_uid="poi-2", task_id="t1"),
    ], label="synthetic-example")
    totals = payload["totals"]
    assert totals["observations"] == 4
    assert totals["uniqueKeys"] == 2
    assert totals["duplicateObservations"] == 2
    assert totals["potentialHitRate"] == 0.5
    assert totals["maxMultiplicity"] == 3
    assert payload["cacheable"]["potentialHitRate"] == 0.5
    assert payload["uidVariantGroups"]["groups"] == 1
    sample = payload["uidVariantGroups"]["samples"][0]
    assert sample["destination"] == list(NEAR)
    assert ["", "poi-2"] in [list(uids) for uids in sample["uids"]]
    assert payload["byMetric"] == {"duration": 4}
    assert payload["networkRequests"] == 0
    assert_no_secrets(payload)


def test_cross_task_hits_need_distinct_task_ids():
    payload = review([
        obs(NEAR, task_id="t1"),
        obs(NEAR, task_id="t1"),
        obs(NEAR, task_id="t2"),
        obs(FAR, task_id="t1"),
    ])
    totals = payload["totals"]
    assert totals["crossTaskKeys"] == 1
    assert totals["crossTaskDuplicateObservations"] == 1
    assert totals["crossTaskPotentialHitRate"] == 0.25


def test_uncacheable_reasons_and_unverified_endpoints_are_excluded():
    payload = review([
        obs(NEAR),
        obs(NEAR, reason="temporary"),
        obs(NEAR, endpoint_verified=False),
        obs(NEAR, endpoint_verified=None),
    ])
    assert payload["totals"]["potentialHitRate"] == 0.75
    cacheable = payload["cacheable"]
    assert cacheable["observations"] == 1
    assert cacheable["potentialHitRate"] == 0.0
    assert cacheable["excludedReasons"] == {"endpoint_unverified": 2, "temporary": 1}


def test_normalize_accepts_mapping_points_and_rejects_bad_records():
    normalized = normalize_observations([
        {**obs(NEAR), "origin": {"lng": ORIGIN[0], "lat": ORIGIN[1]}},
    ])
    assert normalized[0]["origin"] == ORIGIN
    with pytest.raises(ValueError):
        normalize_observations([{"provider": "baidu", "origin": ORIGIN, "destination": NEAR}])
    with pytest.raises(ValueError):
        normalize_observations([{**obs(NEAR), "destination": [True, 31.0]}])
    with pytest.raises(ValueError):
        normalize_observations({"schemaVersion": "od-observations-v9", "observations": []})


def test_cli_roundtrip_refuses_overwrite(tmp_path):
    source = tmp_path / "observations.json"
    source.write_text(json.dumps({
        "schemaVersion": "od-observations-v1",
        "observations": [obs(NEAR, task_id="t1"), obs(NEAR, task_id="t2")],
    }), encoding="utf-8")
    assert load_observations(source)[0]["destination"] == NEAR
    output = tmp_path / "review.json"
    assert main(["--input", str(source), "--output", str(output), "--label", "synthetic-example"]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == "od-cache-review-v1"
    assert payload["label"] == "synthetic-example"
    assert payload["totals"]["potentialHitRate"] == 0.5
    assert payload["networkRequests"] == 0
    with pytest.raises(SystemExit) as exc:
        main(["--input", str(source), "--output", str(output)])
    assert "output_already_exists" in str(exc.value)


def test_jsonl_input(tmp_path):
    source = tmp_path / "observations.jsonl"
    lines = [json.dumps(obs(NEAR, task_id="t1")), json.dumps(obs(FAR, task_id="t1"))]
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = review(load_observations(source))
    assert payload["totals"]["observations"] == 2
    assert payload["totals"]["uniqueKeys"] == 2
    assert payload["totals"]["crossTaskPotentialHitRate"] == 0.0
