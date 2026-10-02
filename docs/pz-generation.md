# PZ building backend

## Boundary and modules

The backend runs after geometry validation and city blocks. It consumes the
compiler's generation-ready indices and exact in-memory LocalGrid instances,
including their ProjectionContext. It does not reconstruct rounded bearings
from output GeoJSON or change ingestion, projection, discovery, topology,
rectification, road relationships, or blocks.

| Module | Responsibility |
| --- | --- |
| `tile_footprint.py` | Existing grid transform and tile-center rasterization, with bounded sampling; stores tile origin separately from the cropped mask. |
| `pz_plan.py` | XML-independent room grid, reserved stair core, inter-storey flights, openings, furniture, occupied floors, and exact flat-roof cover. |
| `pz_tbx.py` | TBX v4, fixed material palette, building-wide room table, object and tile reference tables. |
| `pz_world.py` | PZW cell/lot coordinates, project bounds, conversion settings, and resource paths. |
| `pz_generation.py` | Validation eligibility, existing frontage selection, diagnostic reporting, per-grid projects, and file output. |
| `pz_validate.py` | Independent semantic checks of serialized TBX/PZW, manifest consistency, and disk references. |
| `pz_debug.py` | Coordinate-linked JSON trace from validated source geometry to placement. |

## Reference research

Inspected KnoxMap main at `79ebdc25b0321e188a29d5d43ed7b22e73063bbe` (2026-09-30);
the remote still points to that revision on 2026-10-02. The relevant stages are
[footprint placement](https://github.com/spytheeuclidean-a11y/knoxmap/blob/79ebdc25b0321e188a29d5d43ed7b22e73063bbe/knoxbuild/footprint.py),
[Plan/Building layout](https://github.com/spytheeuclidean-a11y/knoxmap/blob/79ebdc25b0321e188a29d5d43ed7b22e73063bbe/knoxbuild/layout.py),
[TBX serialization](https://github.com/spytheeuclidean-a11y/knoxmap/blob/79ebdc25b0321e188a29d5d43ed7b22e73063bbe/knoxbuild/tbx.py), and
[WorldEd placement](https://github.com/spytheeuclidean-a11y/knoxmap/blob/79ebdc25b0321e188a29d5d43ed7b22e73063bbe/knoxbuild/world.py).

KnoxMap separates placement origins from local masks and keeps layout separate
from XML. Its global room table is indexed by floor grids; ordinary walls arise
from adjacency. Explicit wall objects implement exceptional boundaries such as
railings and glass walls. Its orchestration also performs upstream snapping and
road avoidance, which this backend leaves to the existing compiler.

This implementation was independently written from those behaviors. No KnoxMap
code or catalog was vendored. Minimal tile identifiers and schema conventions
were checked against PZ Mapping Tools at
`4e86b80c505b3d77a2fb5f5675b752da966306f6`:
[BuildingTemplates](https://github.com/Unjammer/PZ_Mapping_Tools/blob/4e86b80c505b3d77a2fb5f5675b752da966306f6/TileZed/src/tiled/BuildingEditor/BuildingTemplates.txt),
[BuildingReader](https://github.com/Unjammer/PZ_Mapping_Tools/blob/4e86b80c505b3d77a2fb5f5675b752da966306f6/WorldEd/src/editor/BuildingEditor/buildingreader.cpp), and
[WorldReader](https://github.com/Unjammer/PZ_Mapping_Tools/blob/4e86b80c505b3d77a2fb5f5675b752da966306f6/WorldEd/src/editor/worldreader.cpp).

Stair bounds and generated openings were also checked against
[Stairs](https://github.com/Unjammer/PZ_Mapping_Tools/blob/4e86b80c505b3d77a2fb5f5675b752da966306f6/WorldEd/src/editor/BuildingEditor/buildingobjects.cpp) and
[BuildingFloor](https://github.com/Unjammer/PZ_Mapping_Tools/blob/4e86b80c505b3d77a2fb5f5675b752da966306f6/WorldEd/src/editor/BuildingEditor/buildingfloor.cpp).

## Coordinates and format invariants

- LocalGrid axes are rotated into tile axes; tile Y increases southward. A
  tile is occupied only when its center lies strictly inside the polygon.
  Rasterization can therefore disconnect thin geometry; this is reported,
  not repaired by changing validated input.
- Plans use local width × height room grids. Zero is outside. Each floor has
  its own room IDs; serialization offsets nonzero IDs into one building table.
- TBX version is 4. Tile entries are 1-based, zero is no tile; furniture
  definitions use 0-based references. Fixed materials and sorted definitions
  keep output stable across processes. KnoxMap's process-dependent string hash
  seeding is not reproduced.
- Doors/windows refer to north or west tile edges. An east or south facade
  uses the next tile's west/north edge, potentially at x=width or y=height.
  Flood-filling the padded background prevents entrances into enclosed holes.
- A multistorey building reserves a wholly occupied 3×6 or 6×3 core before
  generating rooms, openings, and furniture. Its five-cell N/W flight has three
  actual step tiles and two landings, with a parallel lane for circulation.
  All occupied floors share the core; each floor below the highest one gets a
  stair object at the same local coordinates. Core cells remain in one room
  per floor so adjacency does not introduce walls across the flight.
- Stair coordinates are strictly inside the floor, including the full run.
  The serializer validates directions, alignment, floor continuity, core
  occupancy, and clearance from furniture/openings. The `stairs` tile entry
  contains North1–3 and West1–3 and is referenced with a nonzero 1-based index.
  Room grids stay occupied above a flight: BuildingEd itself removes the three
  step cells from the floor above. No stairs are placed on the top occupied
  floor or on the additional empty roof floor.
- Flat roof rectangles exactly partition the top footprint. They use
  `FlatTop`, `Depth="Three"`, caps false, and matching exterior gap materials.
  They live on the highest occupied storey. One empty room-grid floor above
  receives the roof surface. Thus a one-storey TBX has two floor elements.
- Lot origins use `cell = tile // 300`, `offset = tile % 300`. A lot crossing
  a cell boundary is placed once, in its origin cell. Project dimensions include
  the full lot extent. The backend rejects overlapping lot bounding boxes.
- Each LocalGrid is exported independently because PZW has no arbitrary lot
  rotation. `project_origin_in_frame_tiles` translates any negative frame
  coordinates; `world_origin_cells` is the separate WorldEd export origin.
  Grid-frame origins and bearings remain in the manifest, never in TBX.

## Project settings and world allocation

The exporter is **structurally complete but editor-unverified**. The settings
and path conventions below follow the pinned KnoxMap writer linked above;
they are source-format behavior, not evidence of a successful editor load.

- `BMPToTMX` contains the export directory, blank rules/blends/mapbase overrides,
  assignment to world, unknown-color warnings, compression, and pixel copying.
  `update-existing` is false, as in KnoxMap's initial conversion configuration.
- `TMXToBMP` enables main/vegetation images and disables the optional buildings
  image. `GenerateLots` contains an export directory, world origin, four worker
  threads, and empty zombie-map/tile-definition overrides. `LuaSettings` uses
  `spawnpoints.lua` and `objects.lua`. These declarations do not create those
  resources or run conversion. No zone groups are needed because this backend
  emits no zone objects.
- `result.files` holds destination-independent documents. `result.write(path)`
  binds `tmxexportdir` and `exportdir` to absolute native paths under each
  `grid_<id>` directory and creates the `tmx`/`lots` directories, matching
  KnoxMap's native export convention. TBX paths remain relative to the PZW.
  Copying an export to another location or OS requires rebinding those settings
  (re-export, or edit in WorldEd). No Wine-path translation is provided.
- Optional `generate_buildings(..., terrain_bmps={7: "terrain.bmp"})` declares
  a supplied terrain bitmap. Relative paths resolve from that grid's PZW
  directory; absolute paths are retained. The bitmap is not generated, copied,
  or validated by this backend. Supply terrain aligned to the project's local
  axes and translated origin, covering `width_cells * 300` by
  `height_cells * 300` tiles, together with the conversion resources required
  by your WorldEd installation. Without an input, no dangling `<bmp>` is emitted.
  Cell map references remain empty until terrain maps exist or are assigned.
- At write time, existing `tmx/<bitmap stem>_<originX + cellX>_<originY + cellY>.tmx`
  files are attached using absolute paths, following KnoxMap's re-export naming
  convention. Missing files remain unassigned. File existence is not a TMX
  validity or terrain-alignment check.

World origins use **300-tile source cells**, independently of the referenced
mapping tools' 256-tile compiled cells. `world_origin=(70, 0)` is the default
base used by KnoxMap. LocalGrids are sorted by ID and allocated eastward:
the next origin is the previous origin plus its full rounded-up project width
and two source cells. Full extents include empty cells, shifted negative frame
positions, and buildings crossing cell boundaries. The gap prevents the
allocated rectangles from sharing compiled cells as well. The manifest records
the policy, base, gap, per-project origin, and cell dimensions. A lot stays in
one origin cell with its full width/height; it is not split or repeated.

Allocation guarantees disjoint regions **within one export**. It does not scan
installed game maps or reserve space across independent exports. Choose a free
base with `world_origin` before installing alongside other maps. Changing grid
membership or preceding project extents can move later origins on regeneration;
do not apply a changed allocation to an existing save without managing that
migration. This packing is not a geographic join: each rotated grid still uses
its own local axes and requires matching terrain. PZW stores no arbitrary lot
rotation. Source geometry and TBX coordinates are unchanged by world allocation.

## First implementation limits

One room per storey, fixed materials, one exterior ground-floor entrance,
one window per storey when possible, optional simple furniture, and flat roofs
are implemented, including stairs between every occupied storey. A multistorey
footprint without space for a core is reported instead of reducing its height.
Room subdivision is not yet implemented. Separate multipolygon components and disconnected
raster masks require separate buildings and currently produce diagnostics.

WorldEd projects provide building placement and conversion settings. Assign or
convert terrain TMX maps before generating game lots. No terrain, game assets,
spawn maps, Lua content, or editor runtime is bundled.

## Exact remaining verification limitation

**Actual verification still requires running the generated project in a real
WorldEd/PZ environment. No WorldEd project load has succeeded in this sandbox.**
The attempted KnoxMap-pinned Linux `PZWorldEd_cli` release
`worlded-cli-linux-20260909f` requires `GLIBC_2.35`; the sandbox has glibc 2.34.
The dynamic loader failed before application startup, so the attempt did not
reach project parsing. Game tiles/assets and a working PZ environment are also
not configured here. Structural tests cannot establish editor or game behavior.

The outstanding verification is:

1. Load every generated PZW with actual tiles/resources and confirm TBX files
   resolve and the buildings appear.
2. Check displayed cell/lot positions and dimensions against the manifest and
   terrain, including the building that crosses a source-cell boundary.
3. Verify each rotated LocalGrid against its own aligned terrain and confirm
   the separate export origins give the intended installed-world placement.
4. Inspect 2-, 3-, and 4-storey buildings, floor/roof materials, stair objects,
   floor openings and visible connections. Test actual stair traversal in PZ.
5. Run terrain conversion and Generate Lots with valid settings/assets, install
   the outputs in a controlled PZ map setup, and verify loading and placement
   together with any other installed maps.

Until those checks succeed, status remains **structurally complete,
editor-unverified**; no claim of WorldEd compatibility or playability is made.

## Structural fixture verification

`tests/fixtures/pz_multistorey.json` is a hand-authored metric input and expected
structure, independent of XML formatting and serializer table ordering. It
covers 2/3/4 storeys, both stair directions, a 300-tile boundary crossing, and a
separate LocalGrid rotated 30 degrees. The test writes the actual project files
to a directory containing spaces and an ampersand, then resolves every PZW
reference on disk. It compares project membership, paths, cell dimensions,
cell/lot coordinates, TBX dimensions, room grids, core/flight positions,
doors/windows, furniture counts, and roof placement to the fixture.
Separate tests simulate the editor's stair openings to check that landings
remain reachable, reject malformed stair plans, and verify fresh-process
determinism for 2/3/4-storey compiler output.
Export tests inject staging, project-publication, and manifest-publication
failures. Writes are staged first and publishing uses backups to roll back
previous replacements on error, retaining unrelated and unreferenced files.

`tests/fixtures/knoxmap_world.pzw` is output from the unmodified pinned KnoxMap
writer using synthetic placements, not an editor-saved known-good project.
`test_pz_world.py` compares settings, bitmap declarations, cells and lots against
it, and tests native absolute output paths, relative/absolute bitmap references,
existing-TMX naming, absent terrain, and disjoint source/compiled-cell regions.
No reference implementation is needed to run the suite. Destination binding
changes PZW bytes across destinations; determinism tests use the same destination
and resource state across fresh processes.

## Manifest and debug trace

Manifest **version 3** retains `projects`, `buildings_generated`, and the compact
legacy `skipped` list. It adds a flat `buildings` list, ordered by source feature
index, covering every building-tagged candidate, including rejected ones.
Project `buildings` lists contain the same generated records. Non-building
features are not candidates. The validator remains compatible with version 2.

Each record includes:

- `source_feature_id`, its `source_id_origin`, and `source_feature_index`.
  ID precedence is GeoJSON `feature.id`, then properties `id`, `@id`, `osm_id`;
  otherwise the zero-based source index is used and explicitly labeled.
- `local_grid_id` and `local_grid_angle`; `levels` (the parsed or fallback count)
  and `requested_levels` (text preserving the supplied level value). Fractional
  values and semicolon-separated lists that are normalized, as well as invalid
  values that default to one, are described in each record's `warnings` list.
- `footprint_dimensions.meters`: continuous geometry bounds measured along the
  LocalGrid axes and reported to six decimal places, before tile sampling;
  `.tiles`: cropped integer mask dimensions. Dimensions are null when that
  stage cannot be reached. The full mask and building-local polygon remain in
  `footprint`; `source_geometry` is the compiler's validated geographic footprint.
- `pz_position.project_tiles` and `.world_tiles`, both `[x,y,level]`; `placement`
  with owning cell, offset, rectangular dimensions, and project-relative TBX path;
  `tbx_path` relative to the export root. Rejected records have no placement.
- `status`, `stage`, stable reason code, and structured `warnings`/`errors`.
  Stages are `pz_eligibility`, `pz_raster`, `pz_layout`, `pz_tbx`, `pz_placement`,
  and `complete`. Project warnings identify missing terrain. Building warnings
  identify a window or requested furniture that cannot be safely placed.
The top-level `rejection_counts` maps each rejection reason code to its count.

For example, a four-storey 2.1m × 3.4m footprint produces:

```json
{
  "source_feature_id": 1834,
  "status": "rejected",
  "stage": "pz_layout",
  "reason": "stair_core_does_not_fit",
  "footprint_dimensions": {"meters": [2.1, 3.4], "tiles": [2, 3]},
  "levels": 4,
  "local_grid_id": 17
}
```

`debug.json` is written even when all candidates are rejected. It contains
the geographic footprint, grid angle, local-frame raster polygon and binary
mask, project bounding box, entrance edges in building/project tiles, aligned
stair core, and WorldEd project/cell/lot. The document explicitly labels every
coordinate system. It is JSON rather than geographic GeoJSON because local tile
coordinates are not longitude/latitude. Rejected records preserve intermediate
geometry/masks when available and their exact failure stage. Debug contents are
cross-checked against the manifest before publication.

## Semantic validation and regression coverage

Run `python -m pz_validate <export-directory>` to validate a saved export.
The command emits JSON and exits nonzero on failure. Programmatic entry points
are `validate_tbx(xml)`, `validate_pzw(xml, read_tbx)`, and
`validate_export(report, documents)`. The last runs automatically after generation
and again before any publication. A per-building TBX semantic failure produces
a rejected diagnostic; project-wide inconsistency stops export before live files
are replaced. Unexpected programming/runtime failures are not declared valid.
Export paths are checked both lexically and against the resolved output root
before backups or writes. Existing symlinks in document paths or conversion/export
directories are rejected, including aliases into another LocalGrid project.

These are **exporter-profile validators**, not universal readers for arbitrary
hand-authored PZ projects. They independently parse serialized documents and check:

- Room-grid dimensions/IDs, referenced material/furniture indexes, connected and
  aligned occupied floors, an empty roof-support floor, and no orphan room/object.
- Doors on room boundaries joining occupied rooms or reachable exterior space;
  at least one exterior ground-floor entrance; windows on reachable exterior
  boundaries. Courtyard voids are not exterior entrance/window locations here.
- One N/W flight between every occupied storey, valid five-cell bounds, identical
  vertical positions, and a wholly occupied same-room 3×6/6×3 core on every floor.
- Furniture tile footprints entirely within one room, no overlapping furniture,
  no doorway or core obstruction; exact flat-roof coverage without overlaps.
  Walls are tile edges: furniture can adjoin a wall but cannot span a room wall.
  This is grid occupancy validation, not a claim about sprite collision meshes
  or actual stair traversal in the game.
- Unique in-bounds cells, lot offsets in `[0,299]`, full project containment,
  no rectangular overlaps (including across owning cells), resolvable TBXs,
  matching dimensions, and terrain TMX names consistent with cell/world origin.
  **A crossing lot is owned once by its origin cell and may extend into adjacent
  cells.** Requiring its whole rectangle to stay inside that origin cell would
  contradict the established KnoxMap/WorldEd source convention.
- Manifest/file agreement for masks, levels, core, paths, cell/lot, dimensions,
  positions, counts and deterministic origin allocation. Per-grid references
  cannot escape their owning project. Disk validation checks nonempty terrain
  references exist; it does not parse TMX or verify assets. Retained files outside
  the current manifest are intentionally ignored by disk validation.

`tests/fixtures/pz_golden.json` holds hand-authored semantic oracles for a rectangle,
L-shape, rotated footprint, four-storey building, and boundary-crossing building.
Tests compare mask shape, dimensions, storeys, room counts, stairs/cores, roof
rectangles and selected placements without fixing XML whitespace or table order.
Corruption tests independently modify room IDs, openings, stair continuity,
furniture references/occupancy, roofs, orphan objects, cell/lot references,
manifest origins and debug traces and require rejection.

The full suite also generates 1,000 synthetic candidates across ten grid angles
and 1–10 floors: tiny/oversized/narrow footprints, concavity, holes, rotations
different from the LocalGrid, and source-cell boundaries. Every candidate must
either have a valid semantic export or an explicit rejection, and the complete
saved export is validated again. A whole-compiler test launches fresh processes
with different hash seeds, comparing manifests, debug JSON, TBXs, PZWs, complete
directory structure, and annotated compiler output. It uses the same destination
and terrain state because native export directory settings are absolute.

All these checks remain **structural/semantic only and editor-unverified**.
