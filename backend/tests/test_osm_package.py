import json
from pathlib import Path
from types import SimpleNamespace
import zipfile
import pytest

from app.osm_package import discover_packages, package_env, unconfigured_env, update_env
from scripts.setup_osm_region import _safe_extract, download_package


def make_package(tmp_path):
    root = tmp_path / "osm"
    root.mkdir()
    (root / "city.osm-cache").write_bytes(b"cache")
    (root / "city.poly").write_text("city\n0 0\n1 0\n1 1\n0 1\nEND\nEND\n", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 2,
        "region": {"id": "city", "name": "Test City"},
        "snapshot": "osm-city-v1",
        "metric_crs": "EPSG:32650",
        "runtime": {"graph_cache": "city.osm-cache", "coverage": "city.poly"},
    }), encoding="utf-8")
    return root


def test_manifest_selection_writes_explicit_region_config(tmp_path):
    root = make_package(tmp_path)
    package = discover_packages(root)[0]
    env = tmp_path / "backend.env"
    values = package_env(package, env)
    update_env(env, values)
    body = env.read_text(encoding="utf-8")
    assert "OSM_REGION_ID=city" in body
    assert "OSM_DATA_VERSION=osm-city-v1" in body
    assert values["OSM_GRAPH_CACHE_PATH"].endswith("osm/city.osm-cache")


def test_unconfigured_selection_clears_region_paths(tmp_path):
    env = tmp_path / "backend.env"
    update_env(env, unconfigured_env())
    body = env.read_text(encoding="utf-8")
    assert "OSM_REGION_ID=" in body
    assert "OSM_GRAPH_CACHE_PATH=" in body
    assert "OSM_DATA_VERSION=unconfigured" in body


def test_package_name_and_graph_metadata_fill_legacy_manifest_fields(tmp_path):
    root = make_package(tmp_path)
    (root / "graph_metadata.json").write_text(
        json.dumps({"osm_data_version": "geofabrik-shanghai-260913", "crs": "EPSG:32651"}),
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(json.dumps({
        "package": "osm-shanghai-geofabrik-260913-runtime",
        "files": [{"path": "city.osm-cache"}, {"path": "city.poly"}],
    }), encoding="utf-8")
    package = discover_packages(root)[0]
    assert package.region_id == "shanghai"
    assert package.version == "geofabrik-shanghai-260913"
    assert package.metric_crs == "EPSG:32651"


def test_download_installs_verified_runtime_archive_and_selects_region(tmp_path):
    archive = tmp_path / "city-runtime.zip"
    cache = b"cache"
    coverage = b"city\n0 0\n1 0\n1 1\n0 1\nEND\nEND\n"
    manifest = {
        "schema_version": 2,
        "region": {"id": "city", "name": "Test City"},
        "snapshot": "osm-city-v2",
        "metric_crs": "EPSG:32650",
        "runtime": {"graph_cache": "city.osm-cache", "coverage": "city.poly"},
        "files": [
            {"path": "data/osm/city.osm-cache", "sha256": __import__("hashlib").sha256(cache).hexdigest()},
            {"path": "data/osm/city.poly", "sha256": __import__("hashlib").sha256(coverage).hexdigest()},
        ],
    }
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("data/osm/city.osm-cache", cache)
        bundle.writestr("data/osm/city.poly", coverage)
        bundle.writestr("data/osm/manifest.json", json.dumps(manifest))
    repo = tmp_path / "repo"
    data_root = repo / "data" / "osm"
    env = repo / "backend" / ".env"
    download_package(SimpleNamespace(url=archive.as_uri(), sha256=None, region="city",
                                     data_root=data_root, env=env))
    assert (data_root / "city.osm-cache").is_file()
    assert "OSM_REGION_ID=city" in env.read_text(encoding="utf-8")


def test_runtime_archive_rejects_path_traversal(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "must not extract")
    with pytest.raises(ValueError, match="unsafe archive path"):
        _safe_extract(archive, tmp_path / "extract")
