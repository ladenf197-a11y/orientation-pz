"""The PZ backend consumes validated compiler features and exact LocalGrid objects."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

from pz_debug import debug_document
from pz_plan import build_plan
from pz_tbx import render_tbx
from pz_validate import SemanticValidationError, validate_export, validate_tbx
from pz_world import (CELL_SIZE, DEFAULT_WORLD_ORIGIN, PROJECT_GAP_CELLS,
                      LotPlacement, bind_project_paths, render_pzw, validate_world_origin)
from tile_footprint import GridTileFrame, rasterize_footprint


@dataclass
class GenerationResult:
    report: dict
    files: dict[str, str]

    def write(self, directory):
        """Stage all writes, then publish with rollback; retain unrelated output."""
        directory = Path(directory).resolve()
        documents = dict(sorted(self.files.items()))
        documents["manifest.json"] = json.dumps(self.report, indent=2, allow_nan=False) + "\n"
        for name in documents:
            relative = PurePosixPath(name)
            if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                    or ":" in name or not relative.name):
                raise ValueError("export paths must be relative to the destination")
        destinations = [directory / name for name in documents]
        for project in self.report.get("projects", []):
            name = project["pzw_path"]
            if name not in documents:
                raise ValueError("project must reference an export document")
            destinations.extend((directory / name).parent / folder for folder in ("tmx", "lots"))
        # Lexically relative paths can still traverse an existing symlink.
        # Check before reading backups or creating any output, and reject
        # aliases within the tree as well to preserve LocalGrid separation.
        for destination in destinations:
            if not destination.resolve().is_relative_to(directory):
                raise ValueError("export destination escapes the output directory through a symlink")
            components = (destination, *destination.parents)
            if any(path != directory and path.is_relative_to(directory) and path.is_symlink()
                   for path in components):
                raise ValueError("export destinations must not traverse symlinks")
        for project in self.report.get("projects", []):
            name = project["pzw_path"]
            documents[name] = bind_project_paths(documents[name], (directory / name).parent)
        if "world_placement" in self.report:
            validate_export(self.report, documents)
        directory.mkdir(parents=True, exist_ok=True)
        for project in self.report.get("projects", []):
            base = (directory / project["pzw_path"]).parent
            for folder in ("tmx", "lots"):
                (base / folder).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".pz-export-", dir=directory) as staging:
            staged = Path(staging) / "new"
            backup = Path(staging) / "backup"
            # Complete serialization, disk writes, and backups before replacing
            # any live file. The manifest is published last.
            for name, content in documents.items():
                path = staged / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                destination = directory / name
                if destination.exists():
                    saved = backup / name
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, saved)
            published = []
            try:
                for name in documents:
                    destination = directory / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    # Track the path before mutation so an interruption just
                    # after the rename cannot escape the rollback journal.
                    published.append(name)
                    os.replace(staged / name, destination)
            except BaseException:
                for name in reversed(published):
                    saved = backup / name
                    if saved.exists():
                        os.replace(saved, directory / name)
                    else:
                        (directory / name).unlink(missing_ok=True)
                raise


def parse_building_levels(value):
    """Return a supported storey count and an optional input warning."""
    parts = value.split(";") if isinstance(value, str) else [value]
    try:
        numbers = []
        fractional = False
        for part in parts:
            if isinstance(part, bool):
                raise ValueError
            number = float(part)
            if not math.isfinite(number) or number <= 0:
                raise ValueError
            rounded = math.floor(number + 0.5)
            if not 1 <= rounded <= 30:
                raise ValueError
            numbers.append(rounded)
            fractional = fractional or not number.is_integer()
    except (TypeError, ValueError):
        return 1, {"code": "invalid_building_levels",
                   "message": f"Invalid building levels value {value!r}; defaulted to 1."}
    levels = max(numbers)
    messages = []
    if len(numbers) > 1:
        messages.append(f"Multiple values found; using maximum {levels}.")
    if fractional:
        messages.append(f"Fractional values rounded to the nearest storey (halves up); using {levels}.")
    warning = {"code": "building_levels_normalized", "message": " ".join(messages)} if messages else None
    return levels, warning


def _frontage(properties):
    relationship = properties.get("road_relationship") or {}
    if not isinstance(relationship, dict):
        return None
    approach = relationship.get("approach_direction") or {}
    if not isinstance(approach, dict):
        return None
    return {"grid_axis_positive": "east", "grid_axis_negative": "west",
            "cross_axis_positive": "north", "cross_axis_negative": "south"}.get(
                approach.get("local_grid_side"))


def _reason_code(error):
    if isinstance(error, SemanticValidationError):
        return error.code
    message = str(error)
    for fragment, code in (
        ("stair core", "stair_core_does_not_fit"), ("disconnected", "disconnected_tile_mask"),
        ("rasterization budget", "rasterization_budget_exceeded"),
        ("dimensions", "building_dimensions_out_of_range"), ("levels", "invalid_building_levels"),
        ("invalid footprint", "invalid_footprint"),
        ("no tile centers", "no_tile_centers"),
        ("entrance edge", "no_exterior_entrance"), ("empty", "empty_or_invalid_footprint"),
    ):
        if fragment in message:
            return code
    return "invalid_generation_input"


def _identity(feature, properties, index):
    for label, value in [("feature.id", feature.get("id")), *(
            (f"properties.{key}", properties.get(key)) for key in ("id", "@id", "osm_id"))]:
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return value, label
    return index, "source_feature_index"


def generate_buildings(features, generation_ready_indices, local_grids,
                       tile_size_meters=1.0, world_origin=DEFAULT_WORLD_ORIGIN, furnish=False,
                       terrain_bmps=None):
    """Produce one project per LocalGrid without rediscovering or rectifying grids.

    PZW has no arbitrary lot rotation. Projects therefore use each grid's own
    tile frame; geographic anchoring and bearing are recorded in the manifest.
    Missing grids, ineligible geometry, and unrepresentable masks are reported.
    """
    if not math.isfinite(tile_size_meters) or tile_size_meters <= 0:
        raise ValueError("PZ tile size must be finite and greater than zero")
    validate_world_origin(world_origin)
    terrain_bmps = terrain_bmps or {}
    ready = set(generation_ready_indices)
    grids = {grid.grid_id: grid for grid in local_grids}
    frames = {}
    buildings_by_grid = {}
    skipped = []
    records = []

    def reject(record, stage, code, message):
        record.update(status="rejected", stage=stage, reason=code,
                      errors=[{"stage": stage, "code": code, "message": message}])
        # Retain the original compact diagnostic view for existing consumers.
        skipped.append({"source_feature_index": record["source_feature_index"], "reason": message})

    for index, feature in enumerate(features):
        properties = feature.get("properties") or {}
        if properties.get("building") in (None, "no", "false", False):
            continue
        reason = None
        geometry = feature.get("geometry") or {}
        grid_id = properties.get("local_grid_id")
        source_id, id_origin = _identity(feature, properties, index)
        raw_levels = properties.get("building:levels", properties.get("num_floors", 1))
        record = {"source_feature_index": index, "source_feature_id": source_id, "source_id_origin": id_origin,
                  "local_grid_id": grid_id, "local_grid_angle": grids[grid_id].bearing_degrees if grid_id in grids else None,
                  "levels": None, "requested_levels": str(raw_levels), "source_geometry": geometry,
                  "footprint_dimensions": {"meters": None, "tiles": None},
                  "pz_position": None, "tbx_path": None, "warnings": [], "errors": []}
        records.append(record)
        record["levels"], level_warning = parse_building_levels(raw_levels)
        if level_warning:
            record["warnings"].append(level_warning)
        if index not in ready or properties.get("generation_eligible") is not True:
            reason = "not_generation_ready"
        elif geometry.get("type") not in ("Polygon", "MultiPolygon"):
            reason = "non_polygon_building"
        elif grid_id not in grids:
            reason = "missing_local_grid"
        if reason:
            reject(record, "pz_eligibility", reason, reason)
            continue
        stage = "pz_raster"
        try:
            if grid_id not in frames:
                frames[grid_id] = GridTileFrame.from_local_grid(grids[grid_id], tile_size_meters)
            tile_geometry = frames[grid_id].transform_geometry(geometry)
            bounds = tile_geometry.bounds
            if len(bounds) == 4 and all(math.isfinite(v) for v in bounds):
                record["footprint_dimensions"]["meters"] = [round((bounds[2] - bounds[0]) * tile_size_meters, 6),
                                                                round((bounds[3] - bounds[1]) * tile_size_meters, 6)]
            footprint = rasterize_footprint(geometry, frames[grid_id])
            if footprint is None:
                if tile_geometry.is_empty or not tile_geometry.is_valid or tile_geometry.area <= 0:
                    raise ValueError("invalid footprint geometry")
                raise ValueError("footprint contains no tile centers; it may be too small")
            record["footprint"] = footprint.to_dict()
            record["footprint_dimensions"]["tiles"] = [footprint.width, footprint.height]
            stage = "pz_layout"
            plan = build_plan(footprint, record["levels"], _frontage(properties), furnish=furnish)
            stage = "pz_tbx"
            tbx = render_tbx(plan)
            validate_tbx(tbx)
        except (ValueError, TypeError, KeyError, IndexError) as error:
            reject(record, stage, _reason_code(error), str(error))
            continue
        record["plan"] = plan.to_dict()
        if not any(o.kind == "window" for f in plan.floors for o in f.openings):
            record["warnings"].append({"code": "window_not_placed", "message": "No exterior window edge remains clear of the entrance and core."})
        if furnish and not any(f.furniture for f in plan.floors):
            record["warnings"].append({"code": "furniture_not_placed", "message": "No safe furniture tile preserves circulation."})
        buildings_by_grid.setdefault(grid_id, []).append((index, footprint, plan, record, tbx))

    files = {}
    projects = []
    next_origin_x, origin_y = world_origin
    for grid_id, buildings in sorted(buildings_by_grid.items()):
        frame = frames[grid_id]
        # A footprint can extend beyond its grid support. Translate the project
        # frame as a whole, retaining every relative position and reporting it.
        shift_x = min(0, *(b[1].position[0] for b in buildings))
        shift_y = min(0, *(b[1].position[1] for b in buildings))
        width = max(frame.width, *(b[1].position[0] + b[1].width for b in buildings)) - shift_x
        height = max(frame.height, *(b[1].position[1] + b[1].height for b in buildings)) - shift_y
        project_path = f"grid_{grid_id}/world.pzw"
        lots = []
        reports = []
        for index, footprint, plan, record, tbx in buildings:
            x, y = footprint.position[0] - shift_x, footprint.position[1] - shift_y
            lot = LotPlacement(f"buildings/building_{index}.tbx", x, y, plan.width, plan.height)
            # WorldEd places rectangular lots, including each mask's empty cells.
            # Do not silently erase a neighbor when their lot rectangles overlap.
            if any(x < p.x + p.width and x + lot.width > p.x
                   and y < p.y + p.height and y + lot.height > p.y for p in lots):
                reject(record, "pz_placement", "overlapping_lot", "overlapping_lot")
                continue
            lots.append(lot)
            tbx_path = f"grid_{grid_id}/{lot.tbx_path}"
            files[tbx_path] = tbx
            record.update(status="generated", stage="complete", reason=None, placement=lot.to_dict(), tbx_path=tbx_path)
            reports.append(record)
        # Allocate complete source-cell rectangles, including empty cells and
        # crossing lots. Two source cells also separate 256-tile compiled cells.
        columns = (width + CELL_SIZE - 1) // CELL_SIZE
        rows = (height + CELL_SIZE - 1) // CELL_SIZE
        project_origin = (next_origin_x, origin_y)
        for record in reports:
            lot = record["placement"]
            record["pz_position"] = {"project_tiles": [lot["x"], lot["y"], 0],
                                     "world_tiles": [project_origin[0] * CELL_SIZE + lot["x"],
                                                     project_origin[1] * CELL_SIZE + lot["y"], 0]}
        next_origin_x += columns + PROJECT_GAP_CELLS
        files[project_path] = render_pzw(lots, width, height, project_origin,
                                         terrain_bmp=terrain_bmps.get(grid_id, ""))
        projects.append({
            "local_grid_id": grid_id, "pzw_path": project_path,
            "tile_size_meters": tile_size_meters, "bearing_degrees": frame.angle_degrees,
            "frame_origin_lon_lat": list(frame.world_tile_origin()),
            "frame_origin_uv_meters": [frame.origin_u_meters, frame.origin_v_meters],
            "projection_reference_latitude": frame.projection.reference_latitude,
            "project_origin_in_frame_tiles": [shift_x, shift_y],
            "world_origin_cells": list(project_origin), "width_cells": columns, "height_cells": rows,
            "width_tiles": width, "height_tiles": height,
            "warnings": ([] if terrain_bmps.get(grid_id) else [{"code": "terrain_not_supplied", "message": "Assign terrain before conversion/Generate Lots."}]),
            "buildings": reports,
        })
    rejection_counts = {}
    for record in records:
        if record.get("status") == "rejected":
            rejection_counts[record["reason"]] = rejection_counts.get(record["reason"], 0) + 1
    report = {"version": 3, "projects": projects, "buildings": records,
                             "world_placement": {"base_origin_cells": list(world_origin),
                                                 "gap_cells": PROJECT_GAP_CELLS,
                                                 "policy": "local_grids_sorted_eastward"},
                             "buildings_generated": sum(len(p["buildings"]) for p in projects),
              "buildings_rejected": len(skipped),
              "rejection_counts": dict(sorted(rejection_counts.items())),
              "skipped": sorted(skipped, key=lambda item: item["source_feature_index"])}
    files["debug.json"] = json.dumps(debug_document(records, projects), indent=2, allow_nan=False) + "\n"
    report["validation"] = validate_export(report, files)
    return GenerationResult(report, files)
