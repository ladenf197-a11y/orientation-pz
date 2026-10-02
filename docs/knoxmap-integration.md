# KnoxMap Integration

This project can hand annotated building footprints to KnoxMap's own building
generator. KnoxMap does not read this project's `manifest.json` or generated
PZ `.tbx`/`.pzw` files as building input. Its `knoxbuild` step reads a
Knoxify output directory containing `<map>_info.json`, `<map>.bmp`, and
`<map>_buildings.geojson`.

## Install the Hook

Install this project's dependencies into KnoxMap's Python environment, then
install the Build-button hook:

```sh
/path/to/KnoxMap/.venv/bin/python -m pip install \
  -r /path/to/orientation-pz/requirements.txt
/path/to/KnoxMap/.venv/bin/python /path/to/orientation-pz/install_knoxmap_hook.py \
  /path/to/KnoxMap --orientation-pz-root /path/to/orientation-pz
```

The installer backs up `KnoxMap/app.py` before changing only its existing
`/api/buildings` handler, and records the orientation-pz path in
`knoxmap_config.json`. It refuses to patch an unsupported handler and is safe to
run again. If a KnoxMap update replaces `app.py`, rerun the installer; each
source version gets its own backup. Restart KnoxMap after installation.
To remove the hook, close KnoxMap and run the installer command with
`--uninstall`; it restores the matching backup and preserves other config keys.

## Build a Selected Area

Choose the real-world area in KnoxMap and run **Generate map**. Then click the
existing **Build** button. The hook reads that selected area's own
`<map>_buildings.geojson`, `<map>_info.json`, terrain bitmap, and saved settings.
It validates and annotates those exact features, writes the processed buildings
back in KnoxMap's expected format, and runs KnoxMap's builder in-process.
KnoxMap creates its TBX buildings and PZW lot placements as usual. Source
footprints are kept in an `.orientation-pz*.bak` file; reruns use the preserved
original, and regenerated input gets its own backup. KnoxMap's normal
cancellation behavior and saved building settings are preserved.

For a manual run outside the UI, use KnoxMap's Python environment:

```sh
/path/to/KnoxMap/.venv/bin/python /path/to/orientation-pz/knoxmap_pipeline.py \
  /path/to/KnoxMap/output/my-map --knoxmap-root /path/to/KnoxMap
```

Add `--no-build` to analyze and write back without running KnoxMap's builder.
The command needs no separate GeoJSON input; the selected map folder is the input.

For manual or staged workflows, annotate a GeoJSON FeatureCollection first:

```sh
python orientation_detector.py source.geojson oriented.geojson
python knoxmap_export.py oriented.geojson /path/to/KnoxMap/output/my-map \
  --replace-existing
```

The adapter writes `my-map_buildings.geojson`, the exact filename KnoxMap
expects, and leaves `<map>_info.json`, terrain, and other files untouched. Do
not pass this project's optional PZ `.tbx`/`.pzw` export directory to KnoxMap
as input.

The adapter keeps the source feature properties and WGS84 coordinates, filters
out non-buildings and geometries marked ineligible, and writes only Polygon
features. Multipolygons are split into one KnoxMap building per outer
component; interior rings are omitted, matching KnoxMap's own building export
behavior. If a level value is fractional or semicolon-separated, it is
normalized into `building:levels` using this project's parser. The original
value and a warning are retained as `orientation_pz_original_levels` and
`orientation_pz_levels_warning` when normalization changes its interpretation.
KnoxMap's selected preset can still cap the final storey count.

The selected features come directly from the Knoxify output for the chosen
area. KnoxMap uses its own `bbox`, rotation, and `meters_per_tile` to project
them onto the existing terrain; the adapter does not create terrain or
reproject geometries itself. The handoff was exercised through `knoxbuild` at
commit `79ebdc25b0321e188a29d5d43ed7b22e73063bbe`, which read the exported file,
generated a TBX building, and wrote a PZW lot reference. That smoke test used a
synthetic terrain bitmap and does not verify loading or rendering in WorldEd
or gameplay in Project Zomboid.