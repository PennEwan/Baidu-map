import json

import pytest

from app.stage_ledger import assert_no_secrets
from tools.matrix_contract import main, review

ORIGIN = [121.513926, 31.313077]
DEST = [121.517526, 31.313077]


def case(cid="od-1", single=None, matrix=None, **extra):
    record = {
        "id": cid,
        "origin": ORIGIN,
        "destination": DEST,
        "single": {"distance": 950, "duration": 700, "endpointVerified": True} if single is None else single,
        "matrix": {"distance": 952, "duration": 705} if matrix is None else matrix,
    }
    record.update(extra)
    return record


def document(cases, tolerances=None):
    doc = {"schemaVersion": "matrix-contract-cases-v1", "cases": cases}
    if tolerances:
        doc["tolerances"] = tolerances
    return doc


def test_review_matches_within_tolerance():
    payload = review(document([case()]), label="paired-live")
    assert payload["verdict"] == "compatible"
    assert payload["gateEligible"] is True
    assert payload["totals"]["matches"] == 1
    assert payload["totals"]["mismatches"] == 0
    check = payload["checks"][0]
    assert check["verdict"] == "match"
    assert check["distanceWithin"] is True and check["durationWithin"] is True
    assert check["routeDivergence"] is False
    assert payload["networkRequests"] == 0
    assert_no_secrets(payload)


def test_review_flags_numeric_mismatch():
    payload = review(document([case(matrix={"distance": 1200, "duration": 900})]), label="paired-live")
    assert payload["verdict"] == "incompatible"
    assert payload["totals"]["mismatches"] == 1
    assert payload["checks"][0]["distanceWithin"] is False


def test_matrix_zero_is_unknown_not_coverage():
    payload = review(document([case(matrix={"distance": 0, "duration": 0})]), label="paired-live")
    assert payload["verdict"] == "not_run"
    assert payload["totals"]["matrixUnknown"] == 1
    check = payload["checks"][0]
    assert check["verdict"] == "matrix_unknown"
    assert check["matrix"]["distance"] == 0.0
    assert "unknown" in check["notes"][0]


def test_route_divergence_counterexample_is_flagged():
    payload = review(document([case(single={"distance": 950, "duration": 700, "endpointVerified": True},
                                    matrix={"distance": 1050, "duration": 600})]), label="paired-live")
    check = payload["checks"][0]
    assert check["routeDivergence"] is True
    assert check["verdict"] == "mismatch"
    assert payload["totals"]["routeDivergences"] == 1
    assert payload["verdict"] == "incompatible"


def test_matrix_cannot_claim_endpoint_evidence():
    with pytest.raises(ValueError):
        review(document([case(matrix={"distance": 952, "duration": 705, "endpointVerified": True})]))
    payload = review(document([case(single={"distance": 950, "duration": 700})]))
    assert payload["checks"][0]["single"]["endpointVerified"] is None


def test_uid_and_unverified_single_are_recorded():
    payload = review(document([case(destinationUid="poi-9",
                                    single={"distance": 950, "duration": 700, "endpointVerified": False})]))
    assert payload["totals"]["uidBoundCases"] == 1
    assert payload["totals"]["singleEndpointUnverified"] == 1
    assert payload["checks"][0]["destinationUid"] == "poi-9"
    assert any("端点证据" in note for note in payload["checks"][0]["notes"])


def test_gate_only_for_paired_live_label():
    payload = review(document([case()]), label="synthetic-example")
    assert payload["gateEligible"] is False
    assert any("不能过门禁" in note for note in payload["notes"])


def test_cli_roundtrip_and_overwrite_guard(tmp_path):
    source = tmp_path / "cases.json"
    source.write_text(json.dumps(document([case()])), encoding="utf-8")
    synthetic = tmp_path / "synthetic-review.json"
    assert main(["--input", str(source), "--output", str(synthetic), "--label", "synthetic-example"]) == 0
    payload = json.loads(synthetic.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == "matrix-contract-review-v1"
    assert payload["gateEligible"] is False
    paired = tmp_path / "paired-review.json"
    assert main(["--input", str(source), "--output", str(paired), "--label", "paired-live"]) == 0
    assert json.loads(paired.read_text(encoding="utf-8"))["gateEligible"] is True
    with pytest.raises(SystemExit) as exc:
        main(["--input", str(source), "--output", str(paired), "--label", "paired-live"])
    assert "output_already_exists" in str(exc.value)


def test_invalid_documents_are_rejected():
    with pytest.raises(ValueError):
        review({"schemaVersion": "matrix-contract-cases-v9", "cases": []})
    with pytest.raises(ValueError):
        review(document([]))
    with pytest.raises(ValueError):
        review(document([case()], tolerances={"distanceRel": -1}))
    with pytest.raises(ValueError):
        review(document([{**case(), "origin": [True, 31.0]}]))
