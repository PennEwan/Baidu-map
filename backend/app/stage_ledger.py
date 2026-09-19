"""Credential-safe stage timing and quota inventory. Never store URLs or AK."""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import time

_LEDGER = ContextVar("stage_ledger", default=None)
NETWORK_STAGES = frozenset({"walking", "poi_search", "request"})
_BLOCKED = ("ak", "token", "secret", "authorization", "password", "url", "params", "headers", "query")
UNCACHEABLE_REASONS = frozenset({
    "no_result", "temporary", "timeout", "rate_limit", "quota", "auth", "configuration",
    "permission", "invalid_parameter", "interrupted", "deadline", "cancelled", "upstream_status",
    "http_error", "invalid_response", "endpoint_offset", "invalid_duration", "invalid_distance",
})
INDEPENDENT_LIMITERS = (
    {"name": "AnalysisManager.gate", "qpsField": "ANALYSIS_QPS", "process": "backend-api"},
    {"name": "PoiRuntime.gate", "qpsField": "CollectionConfig.runtime.qps", "process": "poi_collect-cli"},
)


def current():
    return _LEDGER.get()


def _safe_extra(extra):
    out = {}
    for key, value in extra.items():
        lowered = key.lower()
        if any(part in lowered for part in _BLOCKED):
            continue
        if isinstance(value, str) and any(part in value.lower() for part in ("ak=", "baidu_map_ak")):
            continue
        out[key] = value
    return out


def od_meta(provider_identity):
    """Map a route provider identity tuple to od-observations fields.

    Only real Baidu walking adapters qualify; synthetic/unknown identities are
    skipped so mock or analytic runs cannot be mistaken for production ODs.
    """
    if not isinstance(provider_identity, tuple) or len(provider_identity) != 7:
        return None
    provider, api_version, coord_system, _, origin_uid, destination_uid, metric = provider_identity
    if provider != "baidu" or not api_version or not coord_system or metric not in ("duration", "distance"):
        return None
    return {
        "provider": provider,
        "api_version": api_version,
        "coord_system": coord_system,
        "origin_uid": origin_uid or "",
        "destination_uid": destination_uid or "",
        "metric": metric,
    }


class StageLedger:
    def __init__(self, *, real_network=False, qps=None, center=None, task_id=None):
        self.real_network = real_network
        self.qps = qps
        self.center = center
        self.task_id = task_id
        self.events = []
        self.observations = []
        self.started = time.perf_counter()
        self.max_in_flight = 0
        self._in_flight = 0

    @contextmanager
    def attach(self):
        token = _LEDGER.set(self)
        try:
            yield self
        finally:
            _LEDGER.reset(token)

    def record(self, stage, *, seconds=0, reason=None, attempt=1, **extra):
        self.events.append({
            "stage": stage,
            "seconds": round(max(0.0, float(seconds)), 6),
            "reason": reason,
            "attempt": attempt,
            "network": bool(self.real_network and stage in NETWORK_STAGES),
            **_safe_extra(extra),
        })

    def enter_inflight(self):
        self._in_flight += 1
        if self._in_flight > self.max_in_flight:
            self.max_in_flight = self._in_flight

    def exit_inflight(self):
        self._in_flight = max(0, self._in_flight - 1)

    def record_od(self, *, stage, provider_identity, origin, destination, reason=None,
                  endpoint_verified=None, duration=None, distance_m=None):
        meta = od_meta(provider_identity)
        if meta is None:
            return None
        record = {
            **meta,
            "origin": [round(float(origin[0]), 6), round(float(origin[1]), 6)],
            "destination": [round(float(destination[0]), 6), round(float(destination[1]), 6)],
            "task_id": self.task_id or "",
            "reason": reason,
            "endpoint_verified": endpoint_verified if endpoint_verified is None else bool(endpoint_verified),
            "duration_s": duration,
            "distance_m": distance_m,
            "stage": stage,
            "network": bool(self.real_network),
        }
        self.observations.append(record)
        return record

    def od_observations(self):
        return [dict(record) for record in self.observations]

    def od_payload(self, label=None):
        return {
            "schemaVersion": "od-observations-v1",
            "label": label,
            "observations": self.od_observations(),
        }

    def summary(self):
        seconds = {}
        for event in self.events:
            seconds[event["stage"]] = seconds.get(event["stage"], 0.0) + event["seconds"]
        network_events = [event for event in self.events if event["stage"] in NETWORK_STAGES]
        failures = sum(1 for event in network_events if event["reason"] not in (None,))
        failure_reasons = Counter(event["reason"] for event in network_events if event["reason"] not in (None,))
        retries = sum(1 for event in self.events if event["attempt"] > 1)
        dominant = max(seconds, key=seconds.get) if seconds else None
        return {
            "schemaVersion": "stage-ledger-v1",
            "realNetworkRequests": 0 if not self.real_network else len(network_events),
            "qps": self.qps,
            "center": self.center,
            "wallSeconds": round(time.perf_counter() - self.started, 6),
            "secondsByStage": {key: round(value, 6) for key, value in seconds.items()},
            "countsByStage": dict(Counter(event["stage"] for event in self.events)),
            "networkAttempts": len(network_events),
            "successes": len(network_events) - failures,
            "retries": retries,
            "failures": failures,
            "failureReasons": dict(sorted(failure_reasons.items())),
            "maxInFlight": self.max_in_flight,
            "dominantStage": dominant,
        }


CONSOLE_INVENTORY_SCHEMA = "console-inventory-v1"
DEFAULT_CONSOLE_STATE = {
    "status": "not-read-from-console",
    "checkedAt": None,
    "walkingPermission": None,
    "walkingPerSecond": None,
    "walkingDaily": None,
    "matrixPermission": None,
    "matrixOdPerSecond": None,
    "matrixDaily": None,
}


def _quota_number(value, field):
    if value is None:
        return None
    if type(value) not in (int, float) or value < 0:
        raise ValueError(f"{field} must be a non-negative number or null")
    return value


def load_console_inventory(path):
    """Read a human-verified console inventory; never contains credentials."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schemaVersion") != CONSOLE_INVENTORY_SCHEMA:
        raise ValueError("unsupported console inventory")
    walking = data.get("walkingDirection") or {}
    matrix = data.get("walkingMatrix") or {}
    permission = walking.get("permission")
    if permission is not None and not isinstance(permission, bool):
        raise ValueError("walking permission must be boolean or null")
    return {
        "status": "verified-console" if data.get("verified") is True else "not-read-from-console",
        "checkedAt": data.get("checkedAt"),
        "walkingPermission": permission,
        "walkingPerSecond": _quota_number(walking.get("perSecond"), "walking perSecond"),
        "walkingDaily": _quota_number(walking.get("daily"), "walking daily"),
        "matrixPermission": matrix.get("permission"),
        "matrixOdPerSecond": _quota_number(matrix.get("odPerSecond"), "matrix odPerSecond"),
        "matrixDaily": _quota_number(matrix.get("daily"), "matrix daily"),
    }


def quota_inventory(settings=None, console=None):
    qps = getattr(settings, "analysis_qps", None) if settings is not None else None
    console_state = dict(DEFAULT_CONSOLE_STATE if console is None else console)
    walking_verified = all(console_state[field] is not None for field in
                           ("walkingPermission", "walkingPerSecond", "walkingDaily"))
    matrix_verified = all(console_state[field] is not None for field in
                          ("matrixPermission", "matrixOdPerSecond", "matrixDaily"))
    return {
        "schemaVersion": "quota-inventory-v1",
        "akConfigured": bool(getattr(settings, "ak_configured", False)) if settings is not None else False,
        "analysisProvider": getattr(settings, "analysis_provider", None) if settings is not None else None,
        "analysisQps": qps,
        "analysisQpsConfigured": qps is not None,
        "analysisQpsIsLocalPacing": True,
        "mockQpsThreeNotProduction": True,
        "assumedMatrixFiftyOdPerSecondNotProduction": True,
        "sharedCredentialSource": "same-backend-env-file",
        "sameAccountIfSameEnvFile": True,
        "independentLimiters": [dict(row) for row in INDEPENDENT_LIMITERS],
        "sharedAccountLimiter": False,
        "crossProcessLimiter": False,
        "b1JoinedFacilities": False,
        "matrixNotInProduction": True,
        "consoleCheckRequired": not (walking_verified and matrix_verified),
        "consoleWalkingVerified": walking_verified,
        "consoleMatrixVerified": matrix_verified,
        "analysisQpsWithinConsoleWalking": (
            None if qps is None or console_state["walkingPerSecond"] is None
            else qps <= console_state["walkingPerSecond"]),
        "officialWalkingDirection": {
            "path": "/directionlite/v1/walking",
            "quotaUnit": "route-request",
        },
        "officialWalkingMatrix": {
            "path": "/routematrix/v2/walking",
            "maxOdProduct": 50,
            "quotaUnit": "od-route",
        },
        "console": console_state,
        "liveBaseline": {
            "center": {"lng": 121.513926, "lat": 31.313077},
            "maxOd": 10,
            "minRounds": 3,
            "requiresAcceptQuota": True,
        },
    }


def od_cache_key(*, provider, api_version, metric, coord_system, origin, destination, origin_uid="", destination_uid=""):
    if not all((provider, api_version, metric, coord_system, origin, destination)):
        raise ValueError("incomplete cache key")
    def packed(point):
        return (round(float(point[0]), 6), round(float(point[1]), 6))
    return (
        str(provider), str(api_version), str(metric), str(coord_system),
        packed(origin), packed(destination), origin_uid or "", destination_uid or "",
    )


def observation_cacheable(reason, endpoint_verified):
    if reason in UNCACHEABLE_REASONS or reason is not None:
        return False
    return endpoint_verified is True


def assert_no_secrets(payload):
    text = str(payload).lower()
    if "ak=" in text or "baidu_map_ak" in text:
        raise AssertionError("ledger payload must not contain credentials")
