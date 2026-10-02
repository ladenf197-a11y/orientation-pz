import copy
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

from orientation_detector import GridEvidence, LocalGrid, ProjectionContext, annotate_collection
from pz_generation import generate_buildings, parse_building_levels
from pz_plan import Furniture, Opening, Room, build_plan, exterior_edges
from pz_tbx import render_tbx
from pz_validate import validate_export
from pz_world import LotPlacement, render_pzw
from tile_footprint import GridTileFrame, TileFootprint, rasterize_footprint


def footprint(mask, position=(0, 0)):
    return TileFootprint(len(mask[0]), len(mask), tuple(tuple(row) for row in mask),
                         position, {}, 0, 1)


def grid(grid_id=0, angle=0):
    return LocalGrid(grid_id, [(-20, -20), (100, -20), (100, 100), (-20, 100), (-20, -20)],
                     angle, GridEvidence(3, 3, 1, 1, 0, 1), ProjectionContext(0))


def geometry(rings, projection=None, angle=0):
    projection = projection or ProjectionContext(0)
    a = math.radians(angle)
    return {"type": "Polygon", "coordinates": [[list(projection.inverse(
        x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)))
        for x, y in ring] for ring in rings]}


def rectangle(x=0, y=0, w=12, h=8):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]


def feature(x=0, **properties):
    return {"type": "Feature", "geometry": geometry([rectangle(x)]),
            "properties": {"building": "yes", "generation_eligible": True,
                           "local_grid_id": 0, **properties}}


def fixture():
    return {"type": "FeatureCollection", "features": [
        feature(0), feature(25), feature(50),
        {"type": "Feature", "properties": {"highway": "residential"},
         "geometry": {"type": "LineString", "coordinates": [
             list(ProjectionContext(0).inverse(x, -5)) for x in (-10, 80)]}},
    ]}


class TileMaskTests(unittest.TestCase):
    def test_rotated_local_frame_and_cropped_mask(self):
        for angle in (0, 27, 89):
            with self.subTest(angle=angle):
                frame = GridTileFrame.from_local_grid(grid(angle=angle))
                fp = rasterize_footprint(geometry([rectangle(10, 15, 12, 8)], angle=angle), frame)
                self.assertEqual((fp.width, fp.height, fp.occupied_tiles), (12, 8, 96))
                self.assertEqual(fp.mask, ((1,) * 12,) * 8)
                self.assertAlmostEqual(fp.position[0], 10 - frame.origin_u_meters)
                self.assertAlmostEqual(fp.position[1], frame.origin_v_meters - 23)

    def test_building_levels_parse_fractional_lists_and_invalid_values(self):
        levels, warning = parse_building_levels("2.5")
        self.assertEqual(levels, 3)
        self.assertEqual(warning["code"], "building_levels_normalized")
        levels, warning = parse_building_levels("1;2")
        self.assertEqual(levels, 2)
        self.assertEqual(warning["code"], "building_levels_normalized")
        levels, warning = parse_building_levels("NaN")
        self.assertEqual(levels, 1)
        self.assertEqual(warning["code"], "invalid_building_levels")
        for value in ("", "0", "31", "1;bad", True):
            with self.subTest(value=value):
                self.assertEqual(parse_building_levels(value)[0], 1)

    def test_axis_aligned_squares_preserve_strict_tile_center_containment(self):
        class IdentityProjection:
            def forward(self, x, y):
                return x, y

            def inverse(self, x, y):
                return x, y

        frame = GridTileFrame(0, 0, 1, 0, 0, 10, 10, IdentityProjection())

        def square(minimum, maximum):
            ring = [[minimum, -minimum], [maximum, -minimum],
                    [maximum, -maximum], [minimum, -maximum], [minimum, -minimum]]
            return {"type": "Polygon", "coordinates": [ring]}

        whole_meter = rasterize_footprint(square(0, 3), frame)
        self.assertEqual(whole_meter.mask, ((1, 1, 1),) * 3)
        boundary_centers = rasterize_footprint(square(0.5, 2.5), frame)
        self.assertEqual((boundary_centers.position, boundary_centers.mask), ((1, 1), ((1,),)))

    def test_hole_and_concavity_are_not_filled(self):
        frame = GridTileFrame.from_local_grid(grid())
        rings = [rectangle(0, 0, 6, 6), rectangle(2, 2, 2, 2)]
        fp = rasterize_footprint(geometry(rings), frame)
        self.assertEqual(fp.occupied_tiles, 32)
        self.assertEqual(fp.mask[2][2:4], (0, 0))
        l_shape = [(0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6), (0, 0)]
        fp = rasterize_footprint(geometry([l_shape]), frame)
        self.assertEqual(fp.occupied_tiles, 20)
        self.assertEqual(fp.mask[0], (1, 1, 0, 0, 0, 0))

    def test_empty_disconnected_and_oversize_have_explicit_outcomes(self):
        frame = GridTileFrame.from_local_grid(grid())
        self.assertIsNone(rasterize_footprint(geometry([rectangle(0, 0, .1, .1)]), frame))
        with self.assertRaisesRegex(ValueError, "budget"):
            rasterize_footprint(geometry([rectangle(w=2000, h=2000)]), frame)
        with self.assertRaisesRegex(ValueError, "disconnected"):
            build_plan(footprint([[1, 0, 1]]))
        with self.assertRaisesRegex(ValueError, "dimensions"):
            build_plan(footprint([[1] * 301]))
        with self.assertRaisesRegex(ValueError, "empty"):
            build_plan(footprint([[0]]))

    def test_tile_scale_and_finite_parameters(self):
        fp = rasterize_footprint(geometry([rectangle()]), GridTileFrame.from_local_grid(grid(), 2))
        self.assertEqual((fp.width, fp.height), (6, 4))
        for size in (0, -1, math.inf, math.nan):
            with self.assertRaises(ValueError):
                GridTileFrame.from_local_grid(grid(), size)


class PlanAndTBXTests(unittest.TestCase):
    def test_exterior_entrances_including_south_east_and_courtyard(self):
        mask = ((1, 1, 1, 1), (1, 0, 0, 1), (1, 0, 0, 1), (1, 1, 1, 1))
        edges = exterior_edges(mask)
        self.assertEqual(len(edges), 16)
        for side in ("north", "south", "west", "east"):
            plan = build_plan(footprint(mask), entrance_side=side)
            door = plan.floors[0].openings[0]
            self.assertIn((door.x, door.y, door.direction, side), [edge[:4] for edge in edges])
            if side == "south":
                self.assertEqual((door.y, door.direction), (4, "N"))
            if side == "east":
                self.assertEqual((door.x, door.direction), (4, "W"))

    def test_roof_exactly_covers_concave_and_holey_masks(self):
        for mask in (((1, 1, 1), (1, 0, 0), (1, 0, 0)),
                     ((1, 1, 1), (1, 0, 1), (1, 1, 1))):
            plan = build_plan(footprint(mask))
            covered = [(x, y) for r in plan.roof for y in range(r.y, r.y + r.height)
                       for x in range(r.x, r.x + r.width)]
            expected = {(x, y) for y, row in enumerate(mask) for x, value in enumerate(row) if value}
            self.assertEqual(set(covered), expected)
            self.assertEqual(len(covered), len(expected))
            self.assertIsNotNone(ET.fromstring(render_tbx(plan)))

    def test_one_room_storey_roof_and_reference_conventions(self):
        plan = build_plan(footprint(((1,) * 6,) * 4), furnish=True)
        root = ET.fromstring(render_tbx(plan))
        self.assertEqual(root.get("version"), "4")
        self.assertEqual(len(root.findall("room")), 1)
        floors = root.findall("floor")
        self.assertEqual(len(floors), 2)  # one occupied floor and roof support
        for level, floor in enumerate(floors):
            self.assertEqual([int(v) for v in floor.findtext("rooms").split(",")], [1 - level] * 24)
        self.assertEqual(len(floors[0].findall("object[@type='door']")), 1)
        self.assertEqual(len(floors[0].findall("object[@type='window']")), 1)
        self.assertFalse(root.findall(".//object[@type='wall']"))
        roof = floors[0].find("object[@type='roof']")
        self.assertEqual((roof.get("RoofType"), roof.get("Depth")), ("FlatTop", "Three"))
        self.assertEqual(root.find(".//object[@type='furniture']").get("FurnitureTiles"), "0")
        self.assertEqual(root.findtext("used_furniture"), "0")
        entries = root.findall("tile_entry")
        self.assertEqual(entries[int(root.get("ExteriorWall")) - 1].get("category"), "exterior_walls")
        self.assertEqual(root.get("ExteriorWallTrim"), "0")
        for node in root.iter():
            for key, value in node.attrib.items():
                if key in {"ExteriorWall", "InteriorWall", "Floor", "Ceiling", "Tile", "FrameTile",
                           "CapTiles", "SlopeTiles", "TopTiles", "Door", "DoorFrame", "Window"}:
                    self.assertTrue(0 <= int(value) <= len(entries))

    def test_requested_levels_remap_room_indexes_and_only_top_is_roofed(self):
        root = ET.fromstring(render_tbx(build_plan(footprint(((1,) * 6,) * 8), levels=3)))
        self.assertEqual(len(root.findall("room")), 3)
        floors = root.findall("floor")
        self.assertEqual(len(floors), 4)
        for floor, room in zip(floors, (1, 2, 3, 0)):
            self.assertEqual([int(v) for v in floor.findtext("rooms").split(",")], [room] * 48)
        self.assertIsNone(floors[0].find("object[@type='roof']"))
        self.assertIsNotNone(floors[2].find("object[@type='roof']"))

    def test_xml_escaping_and_furniture_table(self):
        plan = build_plan(footprint([[1, 1], [1, 1]]))
        floor = replace(plan.floors[0], rooms=(Room('shop & "cafe"'),), furniture=(
            Furniture(0, 0, "test&tile", "W"), Furniture(1, 0, "test&tile", "W"),
            Furniture(1, 1, "second", "S")))
        root = ET.fromstring(render_tbx(replace(plan, floors=(floor,))))
        self.assertEqual(root.find("room").get("Name"), 'shop & "cafe"')
        self.assertEqual(len(root.findall("furniture")), 2)
        self.assertEqual([o.get("FurnitureTiles") for o in root.findall(".//object[@type='furniture']")],
                         ["1", "1", "0"])

    def test_serializer_rejects_invalid_model(self):
        plan = build_plan(footprint([[1, 1], [1, 1]]))
        for floor in (replace(plan.floors[0], grid=((9, 9), (1, 1))),
                      replace(plan.floors[0], openings=(Opening("door", 1, 1, "N"),)),
                      replace(plan.floors[0], furniture=(Furniture(2, 0, "chair"),))):
            with self.assertRaises(ValueError):
                render_tbx(replace(plan, floors=(floor,)))
        with self.assertRaisesRegex(ValueError, "roof"):
            render_tbx(replace(plan, roof=()))

    def test_translation_never_leaks_into_tbx(self):
        original = footprint(((1,) * 5,) * 3)
        shifted = replace(original, position=(310, -200), local_grid_angle=27)
        self.assertEqual(render_tbx(build_plan(original)), render_tbx(build_plan(shifted)))


class WorldPlacementTests(unittest.TestCase):
    def test_cell_boundaries_and_cross_cell_lot(self):
        lots = [LotPlacement('buildings/a&b.tbx', 299, 301, 10, 8),
                LotPlacement('buildings/second.tbx', 300, 0, 3, 3)]
        root = ET.fromstring(render_pzw(lots, 600, 600, (-2, 7)))
        self.assertEqual(root.find("GenerateLots/worldOrigin").get("origin"), "-2,7")
        self.assertEqual(len(root.findall("cell")), 4)
        lot = root.find("cell[@x='0'][@y='1']/lot")
        self.assertEqual((lot.get("x"), lot.get("y")), ("299", "1"))
        self.assertEqual(lot.get("map"), "buildings/a&b.tbx")
        self.assertEqual(len(root.findall(".//lot")), 2)

    def test_bad_placement_is_rejected(self):
        for lot in (LotPlacement("a.tbx", -1, 0, 5, 5),
                    LotPlacement("a.tbx", 298, 0, 5, 5),
                    LotPlacement("../a.tbx", 0, 0, 5, 5)):
            with self.assertRaises(ValueError):
                render_pzw([lot], 300, 300)


class CompilerIntegrationTests(unittest.TestCase):
    def test_level_normalization_warnings_are_written_to_manifest(self):
        generated = generate_buildings(
            [feature(0, **{"building:levels": "2.5"}),
             feature(25, **{"building:levels": "abc"})],
            [0, 1], [grid()])
        self.assertEqual(generated.report["version"], 3)
        self.assertEqual(generated.report["buildings"][0]["warnings"][0]["code"],
                         "building_levels_normalized")
        self.assertIn("3", generated.report["buildings"][0]["warnings"][0]["message"])
        self.assertEqual(generated.report["buildings"][1]["warnings"][0]["code"],
                         "invalid_building_levels")
        self.assertEqual(generated.report["buildings"][1]["levels"], 1)
        with tempfile.TemporaryDirectory() as directory:
            generated.write(directory)
            manifest = json.loads((Path(directory) / "manifest.json").read_text())
        self.assertEqual(manifest["buildings"][0]["warnings"][0]["code"],
                         "building_levels_normalized")
        self.assertEqual(manifest["buildings"][1]["warnings"][0]["code"],
                         "invalid_building_levels")
        legacy_report = dict(generated.report, version=2)
        legacy_report.pop("rejection_counts")
        validate_export(legacy_report, generated.files)

    def test_tiny_footprints_have_specific_reason_and_manifest_count(self):
        tiny = feature()
        tiny["geometry"] = geometry([rectangle(0, 0, .1, .1)])
        generated = generate_buildings([tiny, feature(25, generation_eligible=False)], [0, 1], [grid()])
        self.assertEqual(generated.report["buildings_rejected"], 2)
        self.assertEqual(generated.report["rejection_counts"], {
            "no_tile_centers": 1,
            "not_generation_ready": 1,
        })
        self.assertIn("no tile centers", generated.report["skipped"][0]["reason"])

    def test_gates_levels_frontage_and_no_input_mutation(self):
        features = [feature(0, **{"building:levels": "2",
                    "road_relationship": {"approach_direction": {"local_grid_side": "grid_axis_positive"}}}),
                    feature(25, generation_eligible=False), feature(50, local_grid_id=None),
                    feature(75, **{"building:levels": "NaN"}), feature(90)]
        before = copy.deepcopy(features)
        generated = generate_buildings(features, [0, 1, 2, 3], [grid()])
        self.assertEqual(features, before)
        self.assertEqual(generated.report["buildings_generated"], 2)
        self.assertEqual([s["source_feature_index"] for s in generated.report["skipped"]], [1, 2, 4])
        fallback = generated.report["buildings"][3]
        self.assertEqual(fallback["levels"], 1)
        self.assertEqual(fallback["warnings"][0]["code"], "invalid_building_levels")
        root = ET.fromstring(generated.files["grid_0/buildings/building_0.tbx"])
        self.assertEqual(len(root.findall("floor")), 3)
        door = root.find("floor/object[@type='door']")
        self.assertEqual((door.get("x"), door.get("dir")), (root.get("width"), "W"))

    def test_independent_grids_and_overlap_reporting(self):
        features = [feature(), feature(), feature(25, local_grid_id=1)]
        generated = generate_buildings(features, [0, 1, 2], [grid(), grid(1, 20)])
        self.assertEqual(generated.report["buildings_generated"], 2)
        self.assertEqual(len(generated.report["projects"]), 2)
        self.assertEqual(generated.report["skipped"], [{"source_feature_index": 1, "reason": "overlapping_lot"}])

    def test_negative_frame_positions_are_shifted_as_one_project(self):
        generated = generate_buildings([feature(-30)], [0], [grid()])
        project = generated.report["projects"][0]
        self.assertEqual(project["project_origin_in_frame_tiles"][0], -10)
        self.assertEqual(project["buildings"][0]["placement"]["x"], 0)
        self.assertEqual(project["buildings"][0]["footprint"]["position"][0], -10)

    def test_full_compiler_preserves_existing_results_and_writes_projects(self):
        source = fixture()
        baseline = annotate_collection(source)
        with tempfile.TemporaryDirectory() as directory:
            result = annotate_collection(source, pz_output_directory=directory, pz_furnish=True)
            report = result.pop("pz_generation")
            self.assertEqual(result, baseline)
            self.assertEqual(report["buildings_generated"], 3)
            self.assertFalse(report["skipped"])
            self.assertEqual(json.loads((Path(directory) / "manifest.json").read_text())["buildings_generated"], 3)
            for project in report["projects"]:
                pzw_path = Path(directory) / project["pzw_path"]
                root = ET.parse(pzw_path).getroot()
                for lot in root.findall(".//lot"):
                    tbx = ET.parse(pzw_path.parent / lot.get("map")).getroot()
                    self.assertEqual((tbx.get("width"), tbx.get("height")),
                                     (lot.get("width"), lot.get("height")))

    def test_cli_deterministic_across_fresh_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source.geojson"
            collection = fixture()
            for index, levels in enumerate((2, 3, 4)):
                collection["features"][index]["properties"]["building:levels"] = levels
            source.write_text(json.dumps(collection))
            outputs = []
            for seed in ("1", "98765"):
                # Native export paths are absolute: determinism is defined for
                # the same destination as well as the same input/settings.
                destination = base / "export"
                completed = subprocess.run([
                    sys.executable, "orientation_detector.py", str(source), str(base / f"{seed}.json"),
                    "--pz-output-dir", str(destination), "--pz-furnish"],
                    cwd=Path(__file__).parent, env={**os.environ, "PYTHONHASHSEED": seed},
                    capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                outputs.append({str(path.relative_to(destination)): path.read_bytes()
                                for path in destination.rglob("*") if path.is_file()})
            self.assertEqual(outputs[0], outputs[1])
            self.assertTrue(any(path.endswith(".tbx") for path in outputs[0]))


if __name__ == "__main__":
    unittest.main()
