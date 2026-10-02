# orientation-pz

Compile OpenStreetMap-derived GeoJSON by detecting local street grids,
validating feature geometry, annotating orientations, and optionally exporting
generation-ready buildings for Project Zomboid.

## Quick start

Use Python 3.10 or newer and install the geometry dependency:

```sh
python -m pip install -r requirements.txt
```

Prepare `converted-map.geojson` using the [OSM-to-GeoJSON guide](docs/osm-to-geojson.md),
then run the pipeline and validate the PZ export:

```sh
python orientation_detector.py converted-map.geojson oriented-map.geojson \
  --pz-output-dir generated-pz --pz-tile-size-meters 1
python -m pz_validate generated-pz
```

The PZW writer is regression-tested against a synthetic fixture from a pinned
KnoxMap writer, and generated files pass structural and semantic validation.
This confirms format-level behavior only; loading, rendering, and gameplay still
require verification in WorldEd and Project Zomboid. See the [compatibility and
verification notes](docs/pz-generation.md).

Run the test suite with `python -m unittest`.

Project Zomboid tiles and game assets belong to The Indie Stone; this project's
MIT license covers its code only.

## Documentation

- [Compiler behavior and CLI options](docs/compiler-guide.md)
- [PZ generation formats and verification limits](docs/pz-generation.md)
- [OSM extraction and conversion](docs/osm-to-geojson.md)
- [Structural test fixtures](tests/fixtures/README.md)
