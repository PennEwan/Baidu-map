"""Download, build, validate, and activate a region-specific OSM package.

The runtime application only consumes generated files.  This script keeps the
deployment workflow in one place while leaving the existing selector available
for deployments that already have a package on disk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.request import Request, urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.osm_package import (  # noqa: E402
    discover_packages,
    package_env,
    read_manifest,
    unconfigured_env,
    update_env,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = ROOT / "data" / "osm"
DEFAULT_ENV = ROOT / "backend" / ".env"
REGION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_env_path(path: Path, env_path: Path) -> str:
    return Path(os.path.relpath(path, env_path.resolve().parent)).as_posix()


def _print_packages(packages) -> None:
    if not packages:
        print("No OSM packages found.")
        return
    for package in packages:
        missing = package.missing_runtime_files()
        state = "ready" if not missing else "missing:" + ",".join(missing)
        print(f"{package.region_id}\t{package.region_name}\t{package.version}\t{state}")


def _safe_extract(archive: Path, target: Path) -> None:
    target = target.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = info.filename.replace("\\", "/")
            candidate = Path(name)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError(f"unsafe archive path: {info.filename}")
            # Reject symbolic links in ZIP metadata; they could escape the target
            # directory after extraction on platforms that preserve symlinks.
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (mode & 0o170000) == 0o120000:
                raise ValueError(f"symbolic links are not allowed: {info.filename}")
            destination = (target / candidate).resolve()
            if destination != target and target not in destination.parents:
                raise ValueError(f"unsafe archive path: {info.filename}")
        bundle.extractall(target)


def _manifest_entry_path(manifest: Path, raw: str) -> Path:
    candidate = Path(raw)
    roots = [manifest.parent, *manifest.parents]
    for root in roots:
        resolved = (root / candidate).resolve()
        if resolved.is_file():
            return resolved
    return (manifest.parent / candidate.name).resolve()


def _verify_manifest_files(manifest: Path) -> list[str]:
    """Return missing or mismatched manifest entries; empty means valid."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ["manifest.json"]
    errors: list[str] = []
    for entry in data.get("files", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path = _manifest_entry_path(manifest, entry["path"])
        if not path.is_file():
            errors.append(f"missing:{entry['path']}")
            continue
        expected = entry.get("sha256")
        if expected and _sha256(path).lower() != str(expected).lower():
            errors.append(f"sha256:{entry['path']}")
    return errors


def _archive_source_root(extracted: Path, manifest: Path) -> Path:
    relative = manifest.resolve().relative_to(extracted.resolve())
    parts = relative.parts
    for index in range(len(parts) - 1):
        if parts[index:index + 2] == ("data", "osm"):
            return extracted.joinpath(*parts[:index]) if index else extracted
    return manifest.parent


def _download(url: str, target: Path) -> None:
    request = Request(url, headers={"User-Agent": "Baidu-map OSM package setup"})
    with urlopen(request, timeout=120) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def _select_region(data_root: Path, env_path: Path, region: str) -> int:
    packages = discover_packages(data_root)
    selected = next((item for item in packages if item.region_id == region.lower()), None)
    if selected is None:
        raise ValueError(f"OSM region not found: {region}")
    missing = selected.missing_runtime_files()
    if missing:
        raise ValueError(f"OSM package is incomplete ({', '.join(missing)}): {selected.manifest_path}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    update_env(env_path, package_env(selected, env_path))
    print(f"OSM region selected: {selected.region_id} ({selected.region_name}), version {selected.version}")
    return 0


def download_package(args) -> int:
    region = args.region.lower() if args.region else None
    with tempfile.TemporaryDirectory(prefix="osm-package-") as temp:
        archive = Path(temp) / "package.zip"
        print(f"Downloading OSM runtime package: {args.url}")
        _download(args.url, archive)
        if args.sha256 and _sha256(archive).lower() != args.sha256.lower():
            raise ValueError("runtime package SHA256 does not match --sha256")
        extracted = Path(temp) / "extracted"
        extracted.mkdir()
        _safe_extract(archive, extracted)
        manifests = []
        for path in extracted.rglob("manifest.json"):
            try:
                package = read_manifest(path)
            except (OSError, ValueError, TypeError):
                continue
            if region is None or package.region_id == region:
                manifests.append((path, package))
        if not manifests:
            wanted = f" for region {region}" if region else ""
            raise ValueError(f"no usable manifest found{wanted}")
        manifest, package = manifests[0]
        errors = _verify_manifest_files(manifest)
        if errors:
            raise ValueError("package manifest verification failed: " + ", ".join(errors))
        source_root = _archive_source_root(extracted, manifest)
        repo_root = args.data_root.parent.parent
        destination = repo_root if (source_root / "data" / "osm").is_dir() else args.data_root / "regions" / package.region_id
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, destination, dirs_exist_ok=True)
    chosen = region or package.region_id
    _select_region(args.data_root, args.env, chosen)
    print(f"OSM runtime package installed under {args.data_root}")
    return 0


def _build_manifest(output_root: Path, region: str, name: str, version: str,
                    metric_crs: str, source: str, downloaded_at: str,
                    metadata: dict, coverage: Path, risk: Path, obstacle: Path,
                    graph_cache: Path) -> None:
    files = [output_root / "graph_metadata.json", graph_cache, coverage, risk, obstacle]
    entries = [{"path": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)} for path in files]
    manifest = {
        "schema_version": 2,
        "region": {"id": region, "name": name},
        "snapshot": version,
        "source": source,
        "downloaded_at": downloaded_at,
        "metric_crs": metric_crs,
        "pbf_sha256": metadata.get("pbf_sha256"),
        "attribution": "Copyright OpenStreetMap contributors",
        "license": "https://opendatacommons.org/licenses/odbl/1-0/",
        "runtime": {
            "graph_cache": graph_cache.name,
            "coverage": coverage.name,
            "risk": risk.name,
            "obstacle": obstacle.name,
        },
        "files": entries,
    }
    (output_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_package(args) -> int:
    region = args.region.lower()
    if not REGION_RE.fullmatch(region):
        raise ValueError("region must contain only lowercase letters, digits, '_' or '-'")
    pbf = args.pbf.resolve()
    coverage_source = args.coverage.resolve()
    if not pbf.is_file():
        raise ValueError(f"PBF file not found: {pbf}")
    if not coverage_source.is_file():
        raise ValueError(f"coverage boundary not found: {coverage_source}")
    output_root = (args.data_root / "regions" / region).resolve()
    if output_root.exists() and any(output_root.iterdir()) and not args.force:
        raise ValueError(f"output already exists: {output_root} (use --force to rebuild)")
    output_root.mkdir(parents=True, exist_ok=True)
    graph_cache = output_root / f"{region}.osm-cache"
    coverage = output_root / f"{region}.poly"
    risk = output_root / f"{region}.risks.geojson"
    obstacle = output_root / f"{region}.obstacles.geojson"
    shutil.copy2(coverage_source, coverage)
    env_values = {
        "OSM_REGION_ID": region,
        "OSM_PBF_PATH": _relative_env_path(pbf, args.env),
        "OSM_GRAPH_CACHE_PATH": _relative_env_path(graph_cache, args.env),
        "OSM_DATA_VERSION": args.version,
        "OSM_METRIC_CRS": args.metric_crs,
        "OSM_COVERAGE_BOUNDARY_PATH": _relative_env_path(coverage, args.env),
        "HYBRID_RISK_PATH": _relative_env_path(risk, args.env),
        "HYBRID_OBSTACLE_PATH": _relative_env_path(obstacle, args.env),
        "WATER_REVIEW_DIR": "",
    }
    args.env.parent.mkdir(parents=True, exist_ok=True)
    update_env(args.env, env_values)
    from app.algorithms.osm_offline.graph_store import OsmDataError
    from app.algorithms.osm_offline.prepare import prepare_graph
    from app.config import load_settings
    from backend.tools.export_hybrid_obstacles import export as export_obstacles
    from backend.tools.export_hybrid_risks import export as export_risks

    settings = load_settings()
    try:
        metadata = prepare_graph(settings, source=args.source, downloaded_at=args.downloaded_at,
                                 simplify=not args.no_simplify)
        (output_root / "graph_metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        export_risks(pbf, risk, args.version)
        export_obstacles(pbf, obstacle, args.version)
    except OsmDataError as exc:
        raise ValueError(f"OSM preparation failed: {exc}") from exc
    _build_manifest(output_root, region, args.name or region, args.version, args.metric_crs,
                    args.source, args.downloaded_at, metadata, coverage, risk, obstacle, graph_cache)
    _select_region(args.data_root, args.env, region)
    print(f"OSM runtime package built at {output_root}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Download or build and activate a region-specific OSM runtime package.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="list locally available packages")
    list_parser.set_defaults(handler=lambda args: (_print_packages(discover_packages(args.data_root)) or 0))
    clear_parser = subparsers.add_parser("clear", help="disable OSM and clear the selected region")
    clear_parser.set_defaults(handler=lambda args: (update_env(args.env, unconfigured_env()) or print("OSM region cleared; Baidu-only mode remains available.") or 0))
    select_parser = subparsers.add_parser("select", help="activate a package that is already on disk")
    select_parser.add_argument("--region", required=True)
    select_parser.set_defaults(handler=lambda args: _select_region(args.data_root, args.env, args.region))
    download_parser = subparsers.add_parser("download", help="download, verify, install, and activate a runtime ZIP")
    download_parser.add_argument("--url", required=True)
    download_parser.add_argument("--region")
    download_parser.add_argument("--sha256")
    download_parser.set_defaults(handler=download_package)
    build_parser = subparsers.add_parser("build", help="build a package from a local PBF and coverage boundary")
    build_parser.add_argument("--region", required=True)
    build_parser.add_argument("--name")
    build_parser.add_argument("--pbf", required=True, type=Path)
    build_parser.add_argument("--coverage", required=True, type=Path)
    build_parser.add_argument("--version", required=True)
    build_parser.add_argument("--metric-crs", required=True)
    build_parser.add_argument("--source", required=True)
    build_parser.add_argument("--downloaded-at", required=True)
    build_parser.add_argument("--no-simplify", action="store_true")
    build_parser.add_argument("--force", action="store_true")
    build_parser.set_defaults(handler=build_package)

    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except (OSError, ValueError, ImportError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"OSM setup failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
