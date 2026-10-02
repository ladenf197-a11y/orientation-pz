"""Run orientation-pz on a Knoxify selection and hand it back to KnoxMap."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from knoxmap_export import export_knoxmap_buildings
from orientation_detector import annotate_collection


def _source_buildings(path, directory):
    collection = json.loads(path.read_text(encoding="utf-8"))
    marker = collection.get("orientation_pz") if isinstance(collection, dict) else None
    backup_name = marker.get("source_backup") if isinstance(marker, dict) else None
    if isinstance(backup_name, str) and Path(backup_name).name == backup_name:
        backup = directory / backup_name
        if backup.is_file():
            return backup, json.loads(backup.read_text(encoding="utf-8"))
    return path, collection


def _run_knoxmap_build(directory, knoxmap_root, should_stop=None):
    root = Path(knoxmap_root).resolve()
    if not (root / "knoxbuild" / "build.py").is_file():
        raise ValueError("KnoxMap root must contain knoxbuild/build.py")
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    from knoxbuild.build import build
    from knoxbuild.settings import Settings

    settings = {}
    settings_path = directory / "settings.json"
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"could not read KnoxMap settings.json: {error}") from None
    result = build(str(directory), settings=Settings.from_dict(settings),
                   should_stop=should_stop)
    if result not in (None, 0):
        raise RuntimeError(f"KnoxMap building step failed with status {result}")


def run_knoxmap_pipeline(knoxify_directory, *, knoxmap_root=None, build_map=True,
                         should_stop=None):
    """Analyze the selected Knoxify area, update its footprints, and build it."""
    directory = Path(knoxify_directory).resolve()
    if build_map:
        if knoxmap_root is None:
            raise ValueError("--knoxmap-root is required unless --no-build is used")
        knoxmap_root = Path(knoxmap_root).resolve()
        if not (knoxmap_root / "knoxbuild" / "build.py").is_file():
            raise ValueError("KnoxMap root must contain knoxbuild/build.py")
    info_files = sorted(directory.glob("*_info.json"))
    if len(info_files) != 1:
        raise ValueError("selected Knoxify directory must contain exactly one *_info.json")
    info = json.loads(info_files[0].read_text(encoding="utf-8"))
    map_name = info.get("map_name")
    if not isinstance(map_name, str) or info_files[0].name != f"{map_name}_info.json":
        raise ValueError("Knoxify info map_name does not match its filename")
    source_path = directory / f"{map_name}_buildings.geojson"
    if not source_path.is_file():
        raise ValueError(f"Knoxify selected-area buildings are missing: {source_path.name}")

    raw_path, source = _source_buildings(source_path, directory)
    if not isinstance(source, dict) or source.get("type") != "FeatureCollection":
        raise ValueError("Knoxify building input must be a GeoJSON FeatureCollection")
    annotated = annotate_collection(source)
    ready_count = len(annotated["generation_ready_feature_indices"])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=f".{map_name}-orientation-", suffix=".geojson",
                                         delete=False) as output:
            temporary = Path(output.name)
            json.dump(annotated, output, ensure_ascii=False, allow_nan=False)
            output.write("\n")
        export = export_knoxmap_buildings(temporary, directory, replace_existing=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    if build_map:
        _run_knoxmap_build(directory, knoxmap_root, should_stop=should_stop)

    return {
        "map_name": map_name,
        "source_geojson": str(raw_path),
        "selected_features": len(source.get("features", [])),
        "generation_ready_features": ready_count,
        "local_grids_detected": annotated["orthogonalization_summary"]["domains_detected"],
        "knoxmap_buildings_geojson": export["output"],
        "knoxmap_backup": export["backup"],
        "knoxmap_build_run": build_map,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("knoxify_directory", type=Path,
                        help="the selected map folder under KnoxMap/output")
    parser.add_argument("--knoxmap-root", type=Path,
                        help="KnoxMap source/install root containing knoxbuild/")
    parser.add_argument("--no-build", action="store_true",
                        help="write analyzed footprints back without running KnoxMap Build")
    args = parser.parse_args()
    try:
        result = run_knoxmap_pipeline(
            args.knoxify_directory,
            knoxmap_root=args.knoxmap_root,
            build_map=not args.no_build,
        )
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()