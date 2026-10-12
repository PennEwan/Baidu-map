"""Select an optional OSM runtime package without downloading or building data."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.osm_package import discover_packages, package_env, unconfigured_env, update_env


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = ROOT / "data" / "osm"
DEFAULT_ENV = ROOT / "backend" / ".env"


def _print_packages(packages) -> None:
    if not packages:
        print("No OSM packages found.")
        return
    for package in packages:
        missing = package.missing_runtime_files()
        state = "ready" if not missing else "missing:" + ",".join(missing)
        print(f"{package.region_id}\t{package.region_name}\t{package.version}\t{state}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Select an optional region-specific OSM package; this command never downloads data."
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--list", action="store_true", help="list discovered packages")
    parser.add_argument("--region", help="activate a package by region id")
    parser.add_argument("--clear", action="store_true", help="disable OSM and use no region package")
    args = parser.parse_args()
    if args.list and (args.region or args.clear):
        parser.error("--list cannot be combined with --region or --clear")
    if args.region and args.clear:
        parser.error("--region cannot be combined with --clear")

    packages = discover_packages(args.data_root)
    if args.list:
        _print_packages(packages)
        return 0
    if args.clear:
        update_env(args.env, unconfigured_env())
        print("OSM region cleared; Baidu-only mode remains available.")
        return 0

    region = args.region
    if region is None:
        if not sys.stdin.isatty():
            parser.error("choose --region REGION, use --clear, or run --list")
        _print_packages(packages)
        region = input("Region id (blank disables OSM): ").strip()
        if not region:
            update_env(args.env, unconfigured_env())
            print("OSM region cleared; Baidu-only mode remains available.")
            return 0
    selected = next((item for item in packages if item.region_id == region.lower()), None)
    if selected is None:
        parser.error(f"OSM region not found: {region}")
    missing = selected.missing_runtime_files()
    if missing:
        parser.error(f"OSM package is incomplete ({', '.join(missing)}): {selected.manifest_path}")
    update_env(args.env, package_env(selected, args.env))
    print(f"OSM region selected: {selected.region_id} ({selected.region_name}), version {selected.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
