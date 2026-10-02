# orientation-pz

Run orientation and geometry checks on KnoxMap's selected-area buildings, then
hand them back to KnoxMap's existing builder.

## KnoxMap Setup

Use Python 3.10 or newer. Install this project's dependencies into KnoxMap's
Python environment and install the Build-button hook:

```sh
/path/to/KnoxMap/.venv/bin/python -m pip install \
  -r /path/to/orientation-pz/requirements.txt
/path/to/KnoxMap/.venv/bin/python /path/to/orientation-pz/install_knoxmap_hook.py \
  /path/to/KnoxMap --orientation-pz-root /path/to/orientation-pz
```

Restart KnoxMap. Choose an area, click **Generate map**, then **Build** as
usual. The hook processes that area's footprints and lets KnoxMap generate and
place the TBX buildings in its PZW project. The original footprints and
`app.py` are backed up; rerun the installer after a KnoxMap update.
To undo the hook, run the installer with `--uninstall` while KnoxMap is closed.

## Checks

```sh
python -m unittest
```

The KnoxMap Build route is integration-tested against the pinned source with a
synthetic selected area. This verifies the data handoff and generated project
structure, not in-game rendering or playability. Project Zomboid assets belong
to The Indie Stone; this project's MIT license covers its code only.

## Documentation

- [KnoxMap setup, recovery, and manual workflow](docs/knoxmap-integration.md)
- [Compiler behavior and standalone CLI](docs/compiler-guide.md)
- [PZ formats and verification limits](docs/pz-generation.md)
- [OSM input guide](docs/osm-to-geojson.md)
