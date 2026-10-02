"""Independent semantic checks for this exporter's TBX/PZW subset.

These checks parse serialized files, not the generator's plan objects. They do
not validate game assets, sprite collision shapes, TMX contents, or editor load.
"""

import argparse
from collections import deque
import json
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET


class SemanticValidationError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def require(condition, code, message):
    if not condition:
        raise SemanticValidationError(code, message)


def integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SemanticValidationError("invalid_integer", repr(value)) from None


def parse(document, tag):
    try:
        root = ET.fromstring(document)
    except ET.ParseError as error:
        raise SemanticValidationError("invalid_xml", str(error)) from None
    require(root.tag == tag, "invalid_root", f"expected {tag}")
    return root


def flood(start, allowed):
    seen, queue = {start}, deque([start])
    while queue:
        x, y = queue.popleft()
        for p in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if p not in seen and allowed(p):
                seen.add(p)
                queue.append(p)
    return seen


def validate_tbx(document):
    """Return normalized semantics or raise a coded SemanticValidationError.

    Profile: constant footprint, N/W openings, aligned 3x6/6x3 stair core,
    tile furniture, flat roof, and one empty roof-support floor.
    """
    root = parse(document, "building")
    w, h = (integer(root.get(k)) for k in ("width", "height"))
    require(root.get("version") == "4" and 1 <= w <= 300 and 1 <= h <= 300,
            "building_dimensions", "expected TBX v4 with 1..300 tile dimensions")
    rooms, entries, definitions = root.findall("room"), root.findall("tile_entry"), root.findall("furniture")
    floors = root.findall("floor")
    require(2 <= len(floors) <= 31, "floor_count", "occupied floors plus roof support required")
    levels = len(floors) - 1
    require(all(child.tag in {"tile_entry", "furniture", "used_tiles", "used_furniture", "room", "floor"} for child in root),
            "orphan_object", "unsupported building child")
    require(all(child.tag in {"object", "rooms"} for floor in floors for child in floor),
            "orphan_object", "unsupported floor child")
    tile_attributes = {"ExteriorWall", "ExteriorWallTrim", "InteriorWall", "InteriorWallTrim",
                       "Floor", "Ceiling", "Door", "DoorFrame", "Window", "Curtains", "Shutters",
                       "Stairs", "RoofCap", "RoofSlope", "RoofTop", "GrimeFloor", "GrimeWall",
                       "Tile", "FrameTile", "CurtainsTile", "ShuttersTile", "CapTiles", "SlopeTiles", "TopTiles"}
    for node in root.iter():
        for key in tile_attributes & node.attrib.keys():
            index = integer(node.get(key))
            require(0 <= index <= len(entries), "tile_reference", f"{key}={index}")
    for entry in entries:
        tiles = entry.findall("tile")
        require(bool(tiles) and all(t.get("enum") and t.get("tile") for t in tiles),
                "tile_definition", "empty tile definition")
    for tag, minimum, maximum in (("used_tiles", 1, len(entries)), ("used_furniture", 0, len(definitions) - 1)):
        indexes = [integer(v) for v in (root.findtext(tag) or "").split()]
        require(all(minimum <= v <= maximum for v in indexes) and len(indexes) == len(set(indexes)),
                "tile_reference", f"invalid {tag} references")

    def material(node, key, category):
        index = integer(node.get(key))
        require(1 <= index <= len(entries) and entries[index - 1].get("category") == category,
                "tile_reference", f"{key} must reference {category}")

    material(root, "ExteriorWall", "exterior_walls")
    for room in rooms:
        material(room, "InteriorWall", "interior_walls")
        material(room, "Floor", "floors")
        material(room, "Ceiling", "ceiling")
    grids, occupied = [], []
    used_rooms = set()
    for level, floor in enumerate(floors):
        require(len(floor.findall("rooms")) == 1, "room_grid", f"floor {level} needs one grid")
        values = [integer(v.strip()) for v in (floor.findtext("rooms") or "").split(",")]
        require(len(values) == w * h and all(0 <= v <= len(rooms) for v in values),
                "room_grid", f"floor {level} has invalid room cells")
        grid = {(x, y): values[y * w + x] for y in range(h) for x in range(w)}
        cells = {p for p, value in grid.items() if value}
        grids.append(grid)
        occupied.append(cells)
        used_rooms.update(set(values) - {0})
        if level < levels:
            require(bool(cells) and flood(min(cells), lambda p: p in cells) == cells,
                    "disconnected_floor", f"floor {level} is empty/disconnected")
            require(cells == occupied[0], "floor_footprint", "occupied footprints must align")
        else:
            require(not cells and not floor.findall("object"), "roof_support", "roof floor must be empty")
    require(used_rooms == set(range(1, len(rooms) + 1)), "orphan_room", "unused or missing room definition")
    outside = flood((-1, -1), lambda p: -1 <= p[0] <= w and -1 <= p[1] <= h and p not in occupied[0])
    stairs_by_floor, openings, furnishings, roof_cells, roof_rects = [], [], [], set(), []
    furniture_used = set()
    core = set()
    core_bounds = None
    stair_signature = None
    for level, floor in enumerate(floors[:-1]):
        stairs, flight_cells, doors, floor_furniture, floor_edges = [], set(), set(), set(), set()
        for obj in floor.findall("object"):
            kind = obj.get("type")
            require(kind in {"door", "window", "stairs", "furniture", "roof"}, "orphan_object", f"unsupported object {kind}")
            x, y = integer(obj.get("x")), integer(obj.get("y"))
            if kind in {"door", "window"}:
                direction = obj.get("dir")
                require(direction in {"N", "W"}, "opening_direction", str(direction))
                a, b = (x, y), ((x - 1, y) if direction == "W" else (x, y - 1))
                ra, rb = grids[level].get(a, 0), grids[level].get(b, 0)
                require(ra != rb and (ra or rb), "opening_boundary", f"{kind} at {a} is not on a room boundary")
                exterior = (a in outside and rb != 0) or (b in outside and ra != 0)
                require(kind != "window" or exterior, "window_exterior", "window is not on reachable exterior")
                require((ra and rb) or exterior, "door_connection", "opening faces an enclosed void")
                edge = (x, y, direction)
                require(edge not in floor_edges, "opening_overlap", str(edge))
                floor_edges.add(edge)
                tile = integer(obj.get("Tile"))
                require(tile > 0 and entries[tile - 1].get("category") == ("doors" if kind == "door" else "windows"),
                        "tile_reference", "opening needs the correct tile category")
                openings.append([level, kind, x, y, direction])
                if kind == "door":
                    material(obj, "FrameTile", "door_frames")
                    doors.update({a, b} & occupied[level])
            elif kind == "stairs":
                direction = obj.get("dir")
                require(direction in {"N", "W"}, "stair_direction", str(direction))
                flight = {(x, y + i) if direction == "N" else (x + i, y) for i in range(5)}
                require(level < levels - 1 and flight <= occupied[level] & occupied[level + 1],
                        "stair_consecutive_floors", "flight must join two occupied floors")
                signature = (x, y, direction)
                if stair_signature is None:
                    stair_signature = signature
                    cw, ch = (3, 6) if direction == "N" else (6, 3)
                    core_bounds = {"x": x - 1, "y": y - 1, "width": cw, "height": ch}
                    core = {(cx, cy) for cy in range(y - 1, y - 1 + ch) for cx in range(x - 1, x - 1 + cw)}
                require(signature == stair_signature, "stair_alignment", "flights do not align vertically")
                index = integer(obj.get("Tile"))
                require(1 <= index <= len(entries) and entries[index - 1].get("category") == "stairs",
                        "stair_tiles", "stairs need a stairs tile entry")
                enums = {t.get("enum") for t in entries[index - 1].findall("tile")}
                require({f"{side}{i}" for side in ("North", "West") for i in (1, 2, 3)} <= enums,
                        "stair_tiles", "missing stair tiles")
                stairs.append([level, x, y, direction])
                flight_cells.update(flight)
            elif kind == "furniture":
                index = integer(obj.get("FurnitureTiles"))
                require(0 <= index < len(definitions), "furniture_reference", str(index))
                orientation = obj.get("orient")
                require(orientation in {"N", "W", "S", "E"}, "furniture_orientation", str(orientation))
                entries_for_orientation = [e for e in definitions[index].findall("entry") if e.get("orient") == orientation]
                require(len(entries_for_orientation) == 1, "furniture_reference", "missing/duplicate orientation")
                tiles = entries_for_orientation[0].findall("tile")
                cells = {(x + integer(t.get("x")), y + integer(t.get("y"))) for t in tiles}
                require(bool(cells) and len(cells) == len(tiles) and all(t.get("name") for t in tiles),
                        "furniture_tiles", "empty/duplicate furniture tiles")
                require(cells <= occupied[level] and len({grids[level][p] for p in cells}) == 1,
                        "furniture_wall_overlap", "furniture crosses a wall or occupies outside space")
                require(not cells & floor_furniture, "furniture_overlap", "overlapping furniture")
                floor_furniture.update(cells)
                furniture_used.add(index)
            else:
                material(obj, "TopTiles", "roof_tops")
                rw, rh = integer(obj.get("width")), integer(obj.get("height"))
                require(level == levels - 1 and obj.get("RoofType") == "FlatTop" and obj.get("Depth") == "Three",
                        "roof_floor", "flat roof must cover the top occupied floor")
                require(0 <= x < w and 0 <= y < h and 1 <= rw <= w - x and 1 <= rh <= h - y,
                        "roof_bounds", "roof outside building")
                cells = {(cx, cy) for cy in range(y, y + rh) for cx in range(x, x + rw)}
                require(not cells & roof_cells, "roof_overlap", "roof rectangles overlap")
                roof_cells.update(cells)
                roof_rects.append([x, y, rw, rh])
        require(len(stairs) == int(level < levels - 1), "stair_continuity", f"floor {level} needs one flight to next floor")
        require(not (doors & floor_furniture), "furniture_door_overlap", "furniture blocks a door")
        require(not (flight_cells & doors), "stair_door_overlap", "door opens into a stair flight")
        stairs_by_floor.extend(stairs)
        furnishings.append(floor_furniture)
    if core:
        for level in range(levels):
            require(core <= occupied[level] and len({grids[level][p] for p in core}) == 1,
                    "stair_core", f"floor {level} does not contain the aligned core in one room")
            require(not core & furnishings[level], "furniture_stair_overlap", "furniture blocks stair core")
        # Openings must avoid the flight even on the top storey, where no stair object exists.
        x, y, direction = stair_signature
        flight = {(x, y + i) if direction == "N" else (x + i, y) for i in range(5)}
        for _, _, ox, oy, direction in openings:
            adjacent = {(ox, oy), (ox - 1, oy) if direction == "W" else (ox, oy - 1)}
            require(not flight & adjacent, "stair_opening_overlap", "opening intersects aligned flight")
    require(any(level == 0 and kind == "door" and ((x, y) in outside or
                ((x - 1, y) if direction == "W" else (x, y - 1)) in outside)
                for level, kind, x, y, direction in openings), "missing_entrance", "no exterior ground-floor entrance")
    require(roof_cells == occupied[-2], "roof_coverage", "roof must cover occupied footprint exactly")
    require(furniture_used == set(range(len(definitions))), "orphan_furniture", "unused furniture definition")
    require(len(root.findall(".//object")) == sum(len(f.findall("object")) for f in floors),
            "orphan_object", "object is not attached to a floor")
    return {"width": w, "height": h, "levels": levels,
            "mask": ["".join("1" if (x, y) in occupied[0] else "0" for x in range(w)) for y in range(h)],
            "room_count": len(rooms), "occupied_tiles": len(occupied[0]), "core": core_bounds,
            "openings": sorted(openings), "stairs": stairs_by_floor,
            "furniture_tiles_per_floor": [len(cells) for cells in furnishings],
            "roof_rectangles": sorted(roof_rects)}


def relative_path(value):
    require(isinstance(value, str) and bool(value), "resource_path", "missing resource path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts and "\\" not in value and ":" not in value,
            "resource_path", f"reference escapes project: {value}")
    return path


def validate_pzw(document, read_tbx):
    """Validate cell ownership and complete lot rectangles, including crossings."""
    root = parse(document, "world")
    w, h = integer(root.get("width")), integer(root.get("height"))
    require(root.get("version") == "1.0" and root.get("cellFormat", "legacy-300") == "legacy-300"
            and w > 0 and h > 0, "world_dimensions", "expected positive legacy-300 world")
    origin = root.find("GenerateLots/worldOrigin")
    require(origin is not None, "world_origin", "missing origin")
    origin = [integer(v) for v in origin.get("origin", "").split(",")]
    require(len(origin) == 2, "world_origin", "expected two source-cell coordinates")
    cells, lots, paths = set(), [], set()
    for cell in root.findall("cell"):
        cx, cy = integer(cell.get("x")), integer(cell.get("y"))
        require(0 <= cx < w and 0 <= cy < h and (cx, cy) not in cells,
                "cell_reference", "duplicate or out-of-range cell")
        cells.add((cx, cy))
        terrain = root.find("bmp")
        if terrain is not None and cell.get("map"):
            stem = PurePosixPath(terrain.get("path", "")).stem
            expected_name = f"{stem}_{origin[0] + cx}_{origin[1] + cy}.tmx"
            require(PurePosixPath(cell.get("map")).name == expected_name, "terrain_cell_reference", "TMX name does not match cell/origin")
        for lot in cell.findall("lot"):
            x, y, lw, lh, level = (integer(lot.get(k)) for k in ("x", "y", "width", "height", "level"))
            require(0 <= x < 300 and 0 <= y < 300 and level == 0, "lot_cell", "invalid owning-cell offset/level")
            tx, ty = cx * 300 + x, cy * 300 + y
            require(1 <= lw <= 300 and 1 <= lh <= 300 and tx + lw <= w * 300 and ty + lh <= h * 300,
                    "lot_bounds", "lot extends outside project")
            require(not any(tx < p["x"] + p["width"] and tx + lw > p["x"] and
                            ty < p["y"] + p["height"] and ty + lh > p["y"] for p in lots),
                    "lot_overlap", "rectangular lots overlap, including across cells")
            path = str(relative_path(lot.get("map")))
            require(path.endswith(".tbx") and path not in paths, "tbx_reference", "missing/duplicate TBX reference")
            paths.add(path)
            try:
                tbx = validate_tbx(read_tbx(path))
            except (OSError, KeyError) as error:
                raise SemanticValidationError("missing_tbx", path) from error
            require((lw, lh) == (tbx["width"], tbx["height"]), "lot_dimensions", path)
            lots.append({"tbx_path": path, "x": tx, "y": ty, "width": lw, "height": lh,
                         "cell": [cx, cy], "offset": [x, y], "level": level, "semantics": tbx})
    require(len(cells) == w * h, "cell_reference", "every project cell must be declared once")
    require(len(root.findall(".//lot")) == len(lots), "orphan_lot", "lot is not attached to a cell")
    return {"width_cells": w, "height_cells": h, "world_origin_cells": origin,
            "lots": sorted(lots, key=lambda p: p["tbx_path"])}


def validate_export(report, documents):
    """Cross-check files with manifest, deterministic allocation and references."""
    projects = report["projects"]
    policy = report["world_placement"]
    require(policy["policy"] == "local_grids_sorted_eastward" and policy["gap_cells"] == 2,
            "origin_policy", "unknown allocation policy")
    nx, oy = policy["base_origin_cells"]
    require(type(nx) is int and type(oy) is int, "world_origin", "noninteger base origin")
    ids = [p["local_grid_id"] for p in projects]
    require(ids == sorted(set(ids)), "project_order", "grids must be unique and sorted")
    count, references = 0, set()
    generated_records = []
    for project in projects:
        path = relative_path(project["pzw_path"])
        require(str(path) == f"grid_{project['local_grid_id']}/world.pzw", "project_path", "LocalGrid project separation violated")
        try:
            world = validate_pzw(documents[str(path)], lambda name: documents[str(path.parent / name)])
        except KeyError as error:
            raise SemanticValidationError("missing_project", str(path)) from error
        require(world["world_origin_cells"] == project["world_origin_cells"] == [nx, oy],
                "origin_allocation", "origin does not follow deterministic allocation")
        for key in ("width_cells", "height_cells"):
            require(world[key] == project[key], "project_dimensions", key)
        require(project["width_cells"] == (project["width_tiles"] + 299) // 300 and
                project["height_cells"] == (project["height_tiles"] + 299) // 300,
                "project_dimensions", "tile and cell dimensions disagree")
        nx += world["width_cells"] + 2
        expected = {b["placement"]["tbx_path"]: b for b in project["buildings"]}
        require(len(expected) == len(project["buildings"]) == len(world["lots"]), "manifest_lots", "lot count mismatch")
        for lot in world["lots"]:
            require(lot["tbx_path"] in expected, "manifest_lots", "unrecorded lot")
            building = expected[lot["tbx_path"]]
            require(lot["x"] + lot["width"] <= project["width_tiles"] and
                    lot["y"] + lot["height"] <= project["height_tiles"], "manifest_dimensions", "lot exceeds declared project tile extent")
            require(building["placement"] == {k: v for k, v in lot.items() if k != "semantics"},
                    "manifest_placement", "cell/lot metadata differs")
            fp, semantics = building["footprint"], lot["semantics"]
            require(fp["width"] == lot["width"] and fp["height"] == lot["height"] and
                    ["".join(map(str, row)) for row in fp["mask"]] == semantics["mask"],
                    "manifest_footprint", "mask/dimensions differ from serialized rooms")
            require(len(building["plan"]["floors"]) == semantics["levels"], "manifest_levels", "levels differ")
            require(building["plan"]["core"] == semantics["core"], "manifest_core", "stair core differs")
            shift = project["project_origin_in_frame_tiles"]
            require([fp["position"][0] - shift[0], fp["position"][1] - shift[1]] == [lot["x"], lot["y"]],
                    "manifest_frame", "LocalGrid and project coordinates differ")
            references.add(str(path.parent / lot["tbx_path"]))
            if report.get("version") in (2, 3):
                require(building["status"] == "generated" and building["stage"] == "complete" and not building["errors"],
                        "manifest_status", "generated record has inconsistent status")
                require(building["local_grid_id"] == project["local_grid_id"] and
                        building["local_grid_angle"] == project["bearing_degrees"] and
                        building["tbx_path"] == str(path.parent / lot["tbx_path"]),
                        "manifest_identity", "LocalGrid or TBX identity differs")
                require(building["levels"] == semantics["levels"] and
                        building["footprint_dimensions"]["tiles"] == [lot["width"], lot["height"]],
                        "manifest_dimensions", "level/dimension summary differs")
                origin = world["world_origin_cells"]
                require(building["pz_position"] == {"project_tiles": [lot["x"], lot["y"], 0],
                        "world_tiles": [origin[0] * 300 + lot["x"], origin[1] * 300 + lot["y"], 0]},
                        "manifest_position", "PZ position differs")
                generated_records.append(building)
            count += 1
    require(references == {p for p in documents if p.endswith(".tbx")}, "orphan_tbx", "unplaced or missing building file")
    require({p["pzw_path"] for p in projects} == {p for p in documents if p.endswith(".pzw")},
            "orphan_project", "unrecorded project document")
    require(count == report["buildings_generated"], "manifest_count", "building count differs")
    if report.get("version") in (2, 3):
        records = report["buildings"]
        indexes = [r["source_feature_index"] for r in records]
        require(indexes == sorted(set(indexes)), "manifest_identity", "duplicate/unsorted feature records")
        require([r for r in records if r["status"] == "generated"] == sorted(generated_records, key=lambda r: r["source_feature_index"]),
                "manifest_records", "flat/project building records differ")
        rejected = [r for r in records if r["status"] == "rejected"]
        require(len(rejected) == report["buildings_rejected"] == len(report["skipped"]) and len(records) == count + len(rejected),
                "manifest_count", "rejected count differs")
        require(all(r["stage"] != "complete" and r["reason"] and r["errors"] and r["tbx_path"] is None and
                    r["pz_position"] is None for r in rejected), "manifest_rejection", "incomplete rejection diagnostic")
        if report.get("version") == 3:
            rejection_counts = {}
            for record in rejected:
                rejection_counts[record["reason"]] = rejection_counts.get(record["reason"], 0) + 1
            require(report.get("rejection_counts") == dict(sorted(rejection_counts.items())),
                    "manifest_rejection_counts", "rejection reason counts differ")
        from pz_debug import debug_document
        try:
            debug = json.loads(documents["debug.json"])
        except (KeyError, ValueError) as error:
            raise SemanticValidationError("debug_document", "missing/invalid debug.json") from error
        require(debug == json.loads(json.dumps(debug_document(records, projects))),
                "debug_document", "debug trace differs from manifest")
    return {"status": "passed", "profile": "orientation-pz-v1", "projects": len(projects),
            "buildings": count, "editor_verified": False}


def validate_directory(directory):
    root = Path(directory).resolve()
    report = json.loads((root / "manifest.json").read_text())
    require(isinstance(report, dict), "manifest_root", "manifest must be a JSON object")
    documents = {}
    if report.get("version") in (2, 3):
        documents["debug.json"] = (root / "debug.json").read_text()
    # Load only manifest-owned documents; old unreferenced files may be retained
    # intentionally by the transactional writer.
    for project in report["projects"]:
        pzw = relative_path(project["pzw_path"])
        names = [str(pzw)] + [str(pzw.parent / relative_path(b["placement"]["tbx_path"])) for b in project["buildings"]]
        for name in names:
            path = (root / name).resolve()
            owner = root / pzw.parent
            require(path.is_relative_to(owner), "resource_path", "symlink escapes owning LocalGrid project")
            try:
                documents[name] = path.read_text()
            except OSError as error:
                raise SemanticValidationError("missing_resource", name) from error
        world = parse(documents[str(pzw)], "world")
        for cell in world.findall("cell"):
            if cell.get("map"):
                terrain_path = Path(cell.get("map"))
                if not terrain_path.is_absolute():
                    terrain_path = root / pzw.parent / terrain_path
                require(terrain_path.is_file(), "missing_terrain_map", str(terrain_path))
    return validate_export(report, documents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(validate_directory(args.directory), indent=2))
    except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
