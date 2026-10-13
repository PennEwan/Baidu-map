#!/usr/bin/env python3
"""把 data/osm/ 的运行时文件打成预构建路网包，供 Release 发布与内网分发。

    python scripts/pack_osm_bundle.py [--output PATH]

产物结构固定为 zip 根目录下的五个文件加一份 BUNDLE_README.txt。`dev.py osm` 按资产名
`shanghai-osm-bundle.zip` 去仓库 Release 里找它，并核对 GitHub 给出的 sha256 摘要；自建镜像
分发时把脚本打印的 sha256 交给使用方，用 `OSM_BUNDLE_URL` + `OSM_BUNDLE_SHA256` 安装。

只依赖标准库，用系统 Python 运行即可；构图依赖（Pyrosm 等）不需要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OSM_DIR = ROOT / "data" / "osm"
ASSET_NAME = "shanghai-osm-bundle.zip"

#: 运行时要用的文件；cache 与 metadata 必须有，图层缺一个就少一份证据。
REQUIRED = ("shanghai.osm-cache", "graph_metadata.json")
OPTIONAL = ("shanghai.obstacles.geojson", "shanghai.risks.geojson", "shanghai.poly")


def manifest(meta: dict) -> bytes:
    lines = [
        "15 分钟生活圈 · 上海 OSM 路网数据包（预构建）",
        "",
        f"数据版本（backend/.env 的 OSM_DATA_VERSION）：{meta.get('osm_data_version', '未知')}",
        f"构图时间：{meta.get('built_at', '未知')}",
        f"来源：Geofabrik {meta.get('pbf_filename', '未知')}（下载于 {meta.get('downloaded_at', '未知')}）",
        f"PBF SHA256：{meta.get('pbf_sha256', '未知')}",
        f"节点/有向边：{meta.get('node_count', '未知')} / {meta.get('edge_count', '未知')}",
        "版权：Map data © OpenStreetMap contributors, ODbL 1.0 — https://www.openstreetmap.org/copyright",
        "",
        "包含文件：",
        "  shanghai.osm-cache          图缓存（gzip JSON v1，几何 WKT）",
        "  graph_metadata.json         构图元数据（dev.py doctor 读它核对版本）",
        "  shanghai.obstacles.geojson  硬障碍层（水体、围墙等）",
        "  shanghai.risks.geojson      风险层（水体、铁路）",
        "  shanghai.poly               覆盖边界（Geofabrik extract）",
        "",
        "安装：把本包解压到仓库的 data/osm/ 目录，或在仓库根目录运行 python dev.py osm 自动下载。",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def pack(source: Path, output: Path) -> dict:
    missing = [name for name in REQUIRED if not (source / name).is_file()]
    if missing:
        raise SystemExit(f"缺少 {', '.join(missing)}；先在 data/osm/ 准备好路网数据再打包")

    meta = json.loads((source / "graph_metadata.json").read_text(encoding="utf-8"))
    # 发布件不带上构建机的绝对路径（含用户名），只留可核对的版本信息。
    meta["cache_path"] = "data/osm/shanghai.osm-cache"

    names = [*REQUIRED, *(name for name in OPTIONAL if (source / name).is_file())]
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr("BUNDLE_README.txt", manifest(meta))
        archive.writestr("graph_metadata.json", json.dumps(meta, ensure_ascii=False, indent=1) + "\n")
        for name in names:
            if name != "graph_metadata.json":
                archive.write(source / name, name)
    return meta


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 data/osm 下的预构建路网包")
    parser.add_argument("--source", type=Path, default=OSM_DIR, help="数据目录，默认 data/osm/")
    parser.add_argument("--output", type=Path, default=OSM_DIR / ASSET_NAME, help="输出 zip，默认 data/osm/ 下")
    args = parser.parse_args()

    meta = pack(args.source, args.output)
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    size = args.output.stat().st_size / 1024 / 1024
    print(f"已生成 {args.output}")
    print(f"数据版本: {meta.get('osm_data_version', '未知')}")
    print(f"大小: {size:.1f} MB")
    print(f"sha256: {digest}")
    print()
    print("发布（需要仓库写权限）：")
    print(f"  gh release create osm-data-{meta.get('osm_data_version', 'x')} \"{args.output}\" \\")
    print("    --repo PennEwan/Baidu-map --title '上海 OSM 路网（预构建）' \\")
    print(f"    --notes 'sha256: {digest}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
