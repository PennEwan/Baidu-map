# OSM Region Setup

`dev.py` installs the application and starts the services. It does not select,
download, or build an OSM road-network package. Use the single setup entry
point for package lifecycle operations:

```bash
python backend/scripts/setup_osm_region.py list
```

To download, verify, extract, and activate a runtime ZIP supplied by the
deployment team (the ZIP must contain `data/osm/manifest.json`):

```bash
python backend/scripts/setup_osm_region.py download \
  --url <runtime-package-url> --region <region-id> --sha256 <archive-sha256>
```

If the package is already under `data/osm/`, activate it directly:

```bash
python backend/scripts/setup_osm_region.py select --region <region-id>
```

The selector verifies the graph cache, coverage boundary, data version, and
metric CRS before writing the selected region and paths to `backend/.env`. To
disable OSM and return to Baidu-only mode:

```bash
python backend/scripts/setup_osm_region.py clear
```

For a custom region, download the matching Geofabrik `.osm.pbf` and `.poly`,
install the build dependencies once, and build the complete runtime package in
one command:

```bash
python -m pip install -r backend/requirements-osm-build.txt
python backend/scripts/setup_osm_region.py build \
  --region <region-id> --pbf <path-to-pbf> --coverage <path-to-poly> \
  --version <snapshot-id> --metric-crs <metric-crs> \
  --source <pbf-url> --downloaded-at <yyyy-mm-dd>
```

The build command creates the graph cache, risk layer, obstacle layer, and
manifest, validates the package, and activates it. Add `--no-simplify` when
diagnosing graph preparation and `--force` when replacing an existing region.
The generated package lives under `data/osm/regions/<region-id>/` and can be
archived for deployment to another machine.

In the unconfigured state, Baidu E8.2 remains available. OSM and Hybrid
requests report that the regional graph is unconfigured. A location outside
the selected package boundary remains unknown; the application does not reuse
another region's graph.
