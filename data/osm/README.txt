OSM Shanghai runtime data package

Package: osm-shanghai-geofabrik-260913-runtime
Purpose: runtime data for the 15 Minute Life Circle application.

Install
=======

Extract this archive at the application repository root. The archive keeps
the required data under data/osm/. The application reads the prepared cache;
ordinary users do not need Pyrosm, OSMnx, MSVC, conda, or the source PBF.

Included data
=============

- data/osm/shanghai.osm-cache: prepared walking graph cache
- data/osm/shanghai.poly: extract coverage boundary
- data/osm/shanghai.risks.geojson: OSM-derived risk layer for Hybrid
- data/osm/shanghai.obstacles.geojson: OSM-derived obstacle layer for Hybrid
- data/osm/graph_metadata.json: snapshot and compatibility metadata
- SHA256SUMS.txt: file integrity checks

Data source and license
=======================

The data is derived from OpenStreetMap contributors and the Geofabrik
Shanghai extract. See LICENSE-OSM.txt for attribution and ODbL terms.
The snapshot is fixed; it is not a live road-status feed.

The cache can omit roads, entrances, gates, campus paths, or private access
information. Results are a reference for analysis and are not a guarantee of
real-world walkability.
