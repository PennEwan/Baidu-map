import json

import pytest

from app.stage_ledger import assert_no_secrets
from tools.od_cache_review import review
from tools.od_export import export, main

ORIGIN = [121.513926, 31.313077]


def source(pairs=None):
    return {"origin": ORIGIN, "pairs": pairs if pairs is not None else [{
        "id": 1, "cells": [[3, 8], [4, 8]],
        "points": [[121.503633, 31.314037], [121.507061, 31.314315]],
        "durations": [1038, 763],
    }]}


def test_export_maps_real_pairs_to_observations():
    payload = export(source(), fmt="boundary-reference-v2")
    observations = payload["observations"]
    assert payload["schemaVersion"] == "od-observations-v1"
    assert payload["sourceFormat"] == "boundary-reference-v2"
    assert len(observations) == 2
    first = observations[0]
    assert first["provider"] == "baidu"
    assert first["api_version"] == "directionlite/v1/walking"
    assert first["metric"] == "duration"
    assert first["coord_system"] == "bd09ll"
    assert first["origin"] == ORIGIN
    assert first["destination"] == [121.503633, 31.314037]
    assert first["duration_s"] == 1038.0
    assert first["reason"] is None
    assert first["endpoint_verified"] is None
    assert first["task_id"] == "boundary-reference-v2"
    assert_no_secrets(payload)
    summary = review(observations)["cacheable"]
    assert summary["observations"] == 0
    assert summary["excludedReasons"] == {"endpoint_unverified": 2}


def test_export_rejects_bad_sources():
    with pytest.raises(ValueError):
        export({"origin": ORIGIN, "pairs": []}, fmt="boundary-reference-v2")
    with pytest.raises(ValueError):
        export(source([{"id": 1, "points": [[1, 2]], "durations": [1, 2]}]), fmt="boundary-reference-v2")
    with pytest.raises(ValueError):
        export(source([{"id": 1, "points": [[1, 2]], "durations": [-3]}]), fmt="boundary-reference-v2")
    with pytest.raises(ValueError):
        export(source([{"id": 1, "points": [["a", 2]], "durations": [3]}]), fmt="boundary-reference-v2")


def test_cli_roundtrip_refuses_overwrite(tmp_path):
    from tools.od_cache_review import load_observations

    src = tmp_path / "reference-v2.json"
    src.write_text(json.dumps(source()), encoding="utf-8")
    out = tmp_path / "observations.json"
    assert main(["--format", "boundary-reference-v2", "--input", str(src), "--output", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["observations"]) == 2
    assert payload["observations"][1]["duration_s"] == 763.0
    loaded = load_observations(out)
    assert loaded[0]["destination"] == (121.503633, 31.314037)
    assert review(loaded)["totals"]["observations"] == 2
    with pytest.raises(SystemExit) as exc:
        main(["--format", "boundary-reference-v2", "--input", str(src), "--output", str(out)])
    assert "output_already_exists" in str(exc.value)
