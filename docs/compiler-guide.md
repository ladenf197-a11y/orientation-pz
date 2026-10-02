# orientation-pz

What Can You Do?


Compile a GeoJSON `FeatureCollection` by detecting local street-grid domains,
annotating feature orientations, and orthogonalizing geometry that follows
each domain's grid. Near-square or otherwise balanced features are marked
`undetermined` rather than assigned a cardinal orientation.

Use Python 3.10 or newer. Install the geometry-validation dependency with
`python -m pip install -r requirements.txt`.

Coordinate conversions share a `ProjectionContext` anchored at the map's mean
latitude. It currently uses a local equirectangular approximation and provides
one boundary for moving to a projected CRS later.

Domains are detected with a spatial-angular feature graph and density-based
core components. Grid bearings are fitted from each component, and the support
polygon is constructed from its member geometries rather than feature centers.
Defaults are a 250-meter search radius, at least three aligned features per
domain, and separate 15-degree tolerances for domain-angle agreement and edge
rectification. An optional property can prevent features with different values
from joining the same domain:

```sh
python orientation_detector.py converted-map.geojson oriented-map.geojson \
  --domain-radius-meters 250 --minimum-domain-features 3 \
  --domain-angle-tolerance-degrees 15 \
  --rectification-angle-tolerance-degrees 15 \
  --topology-tolerance-meters 0.05 \
  --building-road-adjacency-distance-meters 40 \
  --minimum-block-area-meters2 100 \
  --domain-property neighborhood_id
```

Omit the output path to write the result to standard output. Each feature gets
`dominant_orientation` (`horizontal`, `vertical`, or `undetermined`),
`dominant_orientation_degrees` (0, 90, or null), and
`orientation_confidence`, `local_grid_id`, `grid_bearing_degrees`,
`grid_confidence`, and `grid_evidence`. The top-level `local_grids` array
contains each grid's polygonal support domain, bearing, confidence, and fit
evidence. Segments within the rectification-angle tolerance of either local
grid axis are made exactly parallel to that axis; more diagonal segments remain
unchanged. The domain-angle tolerance is used only to discover compatible
features and does not control snapping.
Features outside an unambiguous support domain are not modified. Rectification
first builds a shared topology graph, inserts source vertices that fall on
another member's edge within the topology tolerance, and nodes proper segment
intersections. It then solves shared coordinate constraints across the graph.
This preserves repeated vertices and shared boundaries even when an input edge
has different vertex segmentation. Node identity is clustered in projected
meters using the topology tolerance rather than rounded longitude/latitude.
Rectification is semantic-aware: roads and railways use the configured
tolerance; building snapping is limited to closed polygon footprints with at
least 75% of perimeter near either grid axis and edges along both axes, and is
capped at 5 degrees. Water/natural features are not snapped, and unassigned
buildings stay unchanged. Each feature reports
`geometry_orthogonalized`, `orthogonalized_segments`, and
`topology_nodes_inserted`; the top-level `orthogonalization_summary` reports
detected domains, snapped segments, and inserted nodes. `--snap-tolerance`
remains as an alias for `--rectification-angle-tolerance-degrees`.

Before output, a geometry-validation gate checks zero-length edges, ring
closure, self-intersections, collapsed area, and hole preservation, then checks
that source inter-feature connections survive rectification. Invalid
rectifications are rolled back to source geometry; invalid source features are
marked `generation_eligible: false`. Procedural generation should consume only
`generation_ready_feature_indices`; `geometry_validation_summary` reports the
gate outcome. The top-level `validation_report` is deterministic: `features`
is ordered by zero-based `source_feature_index`, each record has `valid`,
`generation_ready`, sorted `reasons`, `repaired`, and `issue_details`; its
`grid_rectifications` records are ordered by `grid_id` and include
`rectification`, sorted `affected_features`, failure indices, and reason codes.
Grid rollback uses the reason `validation_failure` while preserving specific
feature-level causes such as `self_intersection`, `collapsed_polygon`, or
`topology_inconsistency`.

The top-level `road_network` contains normalized `highway`/`railway`
centerlines as `nodes` and `edges`. Crossings and T-junctions are noded in the
shared metric projection; node tolerance joins only nearby endpoints, while
parallel roads remain separate. `bridge`, `tunnel`, and `layer` tags define
grade separation, so disconnected crossings remain distinct components. Edges
retain source feature indices and properties for downstream attribution.
Each generation-ready building also gets a `road_relationship` record, collected
in the top-level `building_road_relationships` array. It reports the nearest
road and edge, closest building/road points, metric distance, approach bearing
and local-grid side, plus unique adjacent source roads within the configurable
40-meter default. Buildings between roads can therefore report both facades;
buildings with no available road have a null nearest-road record.

The `city_blocks` array is polygonized from closed, at-grade road-network faces;
buildings do not define block boundaries. Each `CityBlock` carries its polygonal
boundary, member road edge/node IDs, contained generation-ready building
indices, a majority local-grid summary, and area/perimeter/road-class metadata.
Bridge, tunnel, and non-zero-layer roads do not close an at-grade block.
`--minimum-block-area-meters2` filters tiny faces, while
`--road-boundary-tolerance-meters` controls road-edge attribution to a face.
Polygon edge lengths are capped at the median length for their snapped axis
when estimating orientation, reducing the influence of unusually long edges
such as sharp protrusions. Point-only features have an `undetermined`
orientation.

## PZ building generation

Export buildings after the existing validation, roads, relationships, and block
stages by adding `--pz-output-dir`:

```sh
python orientation_detector.py converted-map.geojson oriented-map.geojson \
  --pz-output-dir generated-pz --pz-tile-size-meters 1 --pz-furnish
```

The export produces `manifest.json`, `debug.json`, and, for each LocalGrid,
`grid_<id>/world.pzw` with `grid_<id>/buildings/building_<source_index>.tbx`.
The exporter is **structurally complete but editor-unverified**. Actual
verification requires loading the generated project in a real WorldEd/PZ
environment with the standard tiles and terrain. Its lots reference the
adjacent building files. Terrain TMX maps must be assigned before Generate Lots.

Building-tagged, generation-ready polygons assigned to a LocalGrid become
center-sampled tile masks. Representable masks become separate one-room-per-storey
plans and TBX v4 buildings. Features without a building property are ignored;
skip diagnostics describe buildings the backend cannot generate. Masks preserve
concavities and holes. Entrances use existing road-frontage information when
available and always face reachable exterior space. A basic window is added
when space permits; `--pz-furnish` adds a chair clear of the entry. Ordinary walls
come from room boundaries. Flat roofs cover the occupied tiles, including
nonrectangular footprints. TBX contains no world coordinates.

`building:levels` (or `num_floors`) requests 1–30 occupied storeys, defaulting to
one. Fractional values round to the nearest storey, with halves rounded up;
semicolon-separated values use the maximum. Invalid values fall back to one
storey and add an `invalid_building_levels` warning instead of rejecting the
building. The serializer adds one empty floor for the roof surface. Multistorey
buildings reserve an aligned 3×6 or 6×3 stair core before room and furniture
generation, with a stair flight connecting every pair of occupied storeys.
The core includes clear landings and side circulation on every floor; no
stairs lead onto the roof. Buildings that cannot fit a valid core are reported
instead of silently losing requested storeys. Invalid level counts, empty/disconnected/oversized
masks, missing grids, ineligible features, and overlapping rectangular lots
are reported in `pz_generation.skipped` and `manifest.json` instead of silently
being reshaped or discarded. Tiny footprints with no covered tile centers have
their own rejection reason, and the manifest counts rejections by reason. For
overlapping lots, the earlier source feature
keeps its placement. A valid isolated building without a discovered LocalGrid
is reported as `missing_local_grid`.

PZW does not encode arbitrary building rotation. Each LocalGrid therefore gets
an independent project with its own tile coordinates, preserving the positions
of buildings within that grid. The manifest records the geographic origin,
exact grid bearing, projection reference latitude, tile scale, and any project
origin shift. Projects receive separate world regions, starting at source-cell
origin `(70, 0)` and proceeding east in grid-ID order with a two-cell gap after
each full project extent. This separates this export's projects; it does not
reassemble their geographic positions or check other installed maps.
Output is deterministic for identical input, settings, destination, and terrain
TMX files. Re-export stages
every generated file before publishing, with rollback if publication fails.
Generated filenames are replaced; old unreferenced files and unrelated files
are retained.

The Python API is `annotate_collection(..., pz_output_directory="generated-pz")`.
For an in-memory backend result, call
`pz_generation.generate_buildings(features, generation_ready_indices, local_grids)`
with validated features and the existing `LocalGrid` objects; its `report`
contains masks, plans, and placement metadata, and its `files` contains the
serialized documents. `result.write(directory)` exports them and binds native
absolute conversion/export paths to that destination. The backend also accepts
`world_origin=(x, y)` for the first project's source-cell origin and
`terrain_bmps={grid_id: "terrain.bmp"}` for existing, aligned terrain inputs.
See the notes below for resource paths, regeneration, and remaining editor checks.

Manifest version 3 lists every building candidate with its source ID, LocalGrid
ID/angle, dimensions, levels, PZ coordinates, cell/lot, TBX path, warnings, and
staged rejection details. It adds rejection counts by reason; the validator
continues to accept version 2 manifests. Fractional or semicolon-normalized
levels and invalid-value fallbacks are identified in each building's `warnings`
list. `debug.json` traces the validated footprint through
the raster mask, bounding box, entrance and stair core to WorldEd placement.
Independent semantic validators check serialized TBX/PZW content before export
and again before publishing files. Validate an existing export with:

```sh
python -m pz_validate generated-pz
```

Validation covers the exporter's supported building model, including valid
boundary-crossing lots. It does not run WorldEd or validate game assets.

See [the architecture and reference notes](pz-generation.md) for format
details and verification limits.

The hand-authored [structural fixture](../tests/fixtures/pz_multistorey.json) covers
2-, 3-, and 4-storey buildings, both stair directions, a lot crossing a cell
boundary, and a separate rotated LocalGrid. Tests compare exported PZW paths,
cell/lot coordinates, TBX references and dimensions, room grids, stairs, and
roofs against its expected structure.

Run the tests with:

```sh
python -m unittest
```
