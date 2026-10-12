"""Discover and activate an optional, region-specific OSM runtime package."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


FILE_KEYS = {
    ".osm.pbf": "pbf",
    ".osm-cache": "graph_cache",
    ".poly": "coverage",
    ".risks.geojson": "risk",
    ".obstacles.geojson": "obstacle",
}


@dataclass(frozen=True)
class OsmPackage:
    manifest_path: Path
    region_id: str
    region_name: str
    version: str
    metric_crs: str | None
    files: dict[str, Path]
    source: str | None = None

    def missing_runtime_files(self) -> list[str]:
        missing = []
        for key in ("graph_cache", "coverage"):
            path = self.files.get(key)
            if path is None or not path.is_file():
                missing.append(key)
        if self.version == "unconfigured":
            missing.append("data_version")
        if not self.metric_crs:
            missing.append("metric_crs")
        return missing


def _repo_root(manifest: Path) -> Path:
    return manifest.resolve().parents[2]


def _resolve_file(manifest: Path, raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    for root in (manifest.parent, _repo_root(manifest)):
        resolved = (root / candidate).resolve()
        if resolved.is_file():
            return resolved
    return (manifest.parent / candidate.name).resolve()


def _region_id(data: dict, manifest: Path) -> str:
    region = data.get("region")
    if isinstance(region, dict):
        region = region.get("id") or region.get("name")
    value = data.get("region_id") or region
    if value:
        return str(value).strip().lower().replace(" ", "-")
    package = str(data.get("package", manifest.parent.name)).lower()
    match = re.search(r"(?:^|-)osm-([a-z0-9_]+?)(?:-geofabrik|-runtime|-\d|$)", package)
    return match.group(1) if match else manifest.parent.name.lower()


def _classify_file(path: Path) -> str | None:
    name = path.name.lower()
    for suffix, key in FILE_KEYS.items():
        if name.endswith(suffix):
            return key
    return None


def _manifest_files(data: dict, manifest: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    declared = data.get("runtime") or data.get("paths")
    if isinstance(declared, dict):
        for key, raw in declared.items():
            if key in {"pbf", "graph_cache", "coverage", "risk", "obstacle", "water_review"}:
                if isinstance(raw, str) and raw:
                    files[key] = _resolve_file(manifest, raw)
    entries = data.get("files", [])
    if isinstance(entries, list):
        for entry in entries:
            raw = entry.get("path") if isinstance(entry, dict) else entry
            if not isinstance(raw, str):
                continue
            path = _resolve_file(manifest, raw)
            key = _classify_file(path)
            if key and key not in files:
                files[key] = path
    return files


def read_manifest(path: Path) -> OsmPackage:
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("OSM manifest must be an object")
    region_id = _region_id(data, path)
    region = data.get("region") if isinstance(data.get("region"), dict) else {}
    region_name = str(data.get("region_name") or region.get("name") or region_id)
    version = str(data.get("osm_data_version") or data.get("snapshot") or "unconfigured")
    metric_crs = data.get("metric_crs") or data.get("crs")
    metadata_path = path.parent / "graph_metadata.json"
    if (not version or version == "unconfigured" or not metric_crs) and metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
            version = version if version != "unconfigured" else metadata.get("osm_data_version", version)
            metric_crs = metric_crs or metadata.get("crs")
        except (OSError, ValueError, TypeError):
            pass
    metric_crs = str(metric_crs) if metric_crs else None
    return OsmPackage(path, region_id, region_name, version, metric_crs,
                      _manifest_files(data, path), data.get("source"))


def discover_manifests(root: Path) -> list[Path]:
    root = Path(root)
    candidates = [root / "manifest.json", *root.rglob("manifest.json")] if root.exists() else []
    return sorted({path.resolve() for path in candidates if path.is_file()})


def discover_packages(root: Path) -> list[OsmPackage]:
    packages = []
    for manifest in discover_manifests(root):
        try:
            packages.append(read_manifest(manifest))
        except (OSError, ValueError, TypeError):
            continue
    return sorted(packages, key=lambda item: (item.region_id, item.version, str(item.manifest_path)))


def _relative_env_path(path: Path | None, env_path: Path) -> str:
    if path is None:
        return ""
    return Path(os.path.relpath(path, Path(env_path).resolve().parent)).as_posix()


def package_env(package: OsmPackage, env_path: Path) -> dict[str, str]:
    return {
        "OSM_REGION_ID": package.region_id,
        "OSM_PBF_PATH": _relative_env_path(package.files.get("pbf"), env_path),
        "OSM_GRAPH_CACHE_PATH": _relative_env_path(package.files.get("graph_cache"), env_path),
        "OSM_DATA_VERSION": package.version,
        "OSM_METRIC_CRS": package.metric_crs or "",
        "OSM_COVERAGE_BOUNDARY_PATH": _relative_env_path(package.files.get("coverage"), env_path),
        "HYBRID_RISK_PATH": _relative_env_path(package.files.get("risk"), env_path),
        "HYBRID_OBSTACLE_PATH": _relative_env_path(package.files.get("obstacle"), env_path),
        "WATER_REVIEW_DIR": _relative_env_path(package.files.get("water_review"), env_path),
    }


def unconfigured_env() -> dict[str, str]:
    return {
        "OSM_REGION_ID": "",
        "OSM_PBF_PATH": "",
        "OSM_GRAPH_CACHE_PATH": "",
        "OSM_DATA_VERSION": "unconfigured",
        "OSM_METRIC_CRS": "EPSG:3857",
        "OSM_COVERAGE_BOUNDARY_PATH": "",
        "HYBRID_RISK_PATH": "",
        "HYBRID_OBSTACLE_PATH": "",
        "WATER_REVIEW_DIR": "",
    }


def update_env(path: Path, values: dict[str, str]) -> None:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    positions = {}
    for index, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            positions[line.partition("=")[0].strip()] = index
    for key, value in values.items():
        line = f"{key}={value}"
        if key in positions:
            lines[positions[key]] = line
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
