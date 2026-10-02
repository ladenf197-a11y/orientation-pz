"""Install building GeoJSON into an existing Knoxify output directory."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from pz_generation import parse_building_levels


def _building_properties(properties):
    if not isinstance(properties, dict):
        return None
    building = properties.get("building")
    if building is None or str(building).strip().lower() in ("", "no", "false"):
        return None
    result = dict(properties)
    result["building"] = str(building)
    raw_levels = next((result[key] for key in ("building:levels", "levels", "num_floors")
                       if key in result and result[key] is not None), None)
    if raw_levels is not None:
        levels, warning = parse_building_levels(raw_levels)
        if warning:
            result["orientation_pz_original_levels"] = str(raw_levels)
            result["orientation_pz_levels_warning"] = warning["message"]
        result["building:levels"] = str(levels)
    return result


def _outer_rings(geometry):
    if not isinstance(geometry, dict):
        return []
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        polygons = [coordinates]
    elif geometry_type == "MultiPolygon":
        polygons = coordinates
    else:
        return []
    if not isinstance(polygons, list):
        return []
    rings = []
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon or not isinstance(polygon[0], list):
            continue
        ring = polygon[0]
        if len(ring) < 4 or ring[0] != ring[-1]:
            continue
        if any(not isinstance(point, (list, tuple)) or len(point) < 2
               or not isinstance(point[0], (int, float))
               or not isinstance(point[1], (int, float))
               or not math.isfinite(point[0]) or not math.isfinite(point[1])
               or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90
               for point in ring):
            continue
        rings.append(ring)
    return rings


def export_knoxmap_buildings(geojson_path, knoxify_directory, replace_existing=False):
    """Write KnoxMap's expected ``<map>_buildings.geojson`` beside its info/BMP."""
    source_path = Path(geojson_path)
    output_dir = Path(knoxify_directory)
    info_files = sorted(output_dir.glob("*_info.json"))
    if len(info_files) != 1:
        raise ValueError("Knoxify directory must contain exactly one *_info.json file")
    info_path = info_files[0]
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read Knoxify metadata or GeoJSON: {error}") from None

    map_name = info.get("map_name")
    if (not isinstance(map_name, str) or not map_name or Path(map_name).name != map_name
            or info_path.name != f"{map_name}_info.json"):
        raise ValueError("Knoxify info map_name must match its *_info.json filename")
    bbox = info.get("bbox")
    if (not isinstance(bbox, dict)
            or any(key not in bbox for key in ("south", "west", "north", "east"))
            or not all(isinstance(bbox[key], (int, float)) and math.isfinite(bbox[key])
                       for key in ("south", "west", "north", "east"))
            or not isinstance(info.get("meters_per_tile"), (int, float))
            or not math.isfinite(info["meters_per_tile"])
            or info["meters_per_tile"] <= 0):
        raise ValueError("Knoxify info is missing valid bbox or meters_per_tile metadata")
    if not (output_dir / f"{map_name}.bmp").is_file():
        raise ValueError(f"Knoxify terrain bitmap is missing: {map_name}.bmp")
    if not isinstance(source, dict) or source.get("type") != "FeatureCollection" \
            or not isinstance(source.get("features"), list):
        raise ValueError("input must be a GeoJSON FeatureCollection")

    ready_indices = source.get("generation_ready_feature_indices")
    ready_indices = set(ready_indices) if isinstance(ready_indices, list) else None
    features = []
    skipped = 0
    split_components = 0
    for index, feature in enumerate(source["features"]):
        if not isinstance(feature, dict):
            skipped += 1
            continue
        properties = _building_properties(feature.get("properties"))
        if (properties is None or feature.get("properties", {}).get("generation_eligible") is False
                or (ready_indices is not None and index not in ready_indices)):
            skipped += 1
            continue
        rings = _outer_rings(feature.get("geometry"))
        if not rings:
            skipped += 1
            continue
        if len(rings) > 1:
            split_components += len(rings) - 1
        for component, ring in enumerate(rings):
            item = {
                "type": "Feature",
                "properties": dict(properties),
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
            if feature.get("id") is not None:
                item["id"] = (f"{feature['id']}:{component}" if len(rings) > 1
                              else feature["id"])
            features.append(item)
    if not features:
        raise ValueError("input contains no KnoxMap-compatible building footprints")

    target = output_dir / f"{map_name}_buildings.geojson"
    if target.exists() and not replace_existing:
        raise FileExistsError(f"{target} exists; pass --replace-existing to preserve a backup and replace it")
    backup = target.with_suffix(target.suffix + ".orientation-pz.bak")
    previous_backup = None
    if target.exists():
        try:
            previous = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        marker = previous.get("orientation_pz") if isinstance(previous, dict) else None
        if isinstance(marker, dict):
            name = marker.get("source_backup")
            if isinstance(name, str) and Path(name).name == name:
                candidate = output_dir / name
                if candidate.is_file():
                    previous_backup = candidate
                    backup = candidate
        if previous_backup is None and backup.exists():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()[:12]
            backup = target.with_suffix(
                target.suffix + f".orientation-pz.{digest}.bak")
            if backup.exists() and backup.read_bytes() != target.read_bytes():
                raise FileExistsError(f"refusing to overwrite existing backup: {backup}")

    document = {"type": "FeatureCollection", "features": features,
                "orientation_pz": {"version": 1,
                                   "source_backup": backup.name if target.exists() else None}}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_dir,
                                         prefix=f".{map_name}-", suffix=".geojson",
                                         delete=False) as output:
            temporary = Path(output.name)
            json.dump(document, output, ensure_ascii=False, allow_nan=False)
            output.write("\n")
        if target.exists() and previous_backup is None:
            if backup.exists() and backup.read_bytes() == target.read_bytes():
                previous_backup = backup
            else:
                os.replace(target, backup)
        if target.exists() and previous_backup is not None:
            target.unlink()
        if target.exists():
            os.replace(target, backup)
        os.replace(temporary, target)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if backup.exists() and not target.exists():
            os.replace(backup, target)
        raise

    return {"output": str(target), "buildings_written": len(features),
            "skipped_features": skipped, "split_multipolygon_components": split_components,
            "replaced_existing": backup.exists(), "backup": str(backup) if backup.exists() else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("geojson", type=Path, help="annotated GeoJSON FeatureCollection")
    parser.add_argument("knoxify_directory", type=Path,
                        help="existing Knoxify output directory containing *_info.json and <map>.bmp")
    parser.add_argument("--replace-existing", action="store_true",
                        help="back up and replace the directory's current building GeoJSON")
    args = parser.parse_args()
    try:
        print(json.dumps(export_knoxmap_buildings(
            args.geojson, args.knoxify_directory, args.replace_existing), indent=2))
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()