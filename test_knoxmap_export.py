import json
import os
import importlib.util
from pathlib import Path
import tempfile
import sys
import unittest
import xml.etree.ElementTree as ET

from knoxmap_export import export_knoxmap_buildings
from knoxmap_pipeline import run_knoxmap_pipeline
from install_knoxmap_hook import HOOK_MARKER, install_hook


def square(x, y, size=0.001):
    return {"type": "Polygon", "coordinates": [[
        [x, y], [x + size, y], [x + size, y + size],
        [x, y + size], [x, y],
    ]]}


class KnoxMapExportTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("ORIENTATION_PZ_KNOXMAP_ROOT"),
                         "set ORIENTATION_PZ_KNOXMAP_ROOT to run KnoxMap's Flask Build route")
    def test_knoxmap_build_button_route_runs_orientation_hook(self):
        from PIL import Image

        root = Path(os.environ["ORIENTATION_PZ_KNOXMAP_ROOT"]).resolve()
        sys.path.insert(0, str(root))
        config_path = root / "knoxmap_config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        config["orientation_pz_root"] = str(Path(__file__).parent.resolve())
        config_path.write_text(json.dumps(config, indent=2))
        spec = importlib.util.spec_from_file_location("orientation_pz_knoxmap_app", root / "app.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory(prefix="orientation-pz-", dir=module.OUTPUT_DIR) as temporary:
            map_dir = Path(temporary)
            map_name = map_dir.name
            info = {"map_name": map_name, "bbox": {"south": 35.0, "west": -84.0,
                    "north": 35.002, "east": -83.998}, "rotation": 0,
                    "meters_per_tile": 1, "width_tiles": 300,
                    "height_tiles": 300, "cells_x": 1, "cells_y": 1}
            (map_dir / f"{map_name}_info.json").write_text(json.dumps(info))
            (map_dir / "settings.json").write_text(json.dumps({"preset": "rural", "true_map": 1}))
            Image.new("RGB", (300, 300), (110, 145, 80)).save(map_dir / f"{map_name}.bmp")
            geometry = {"type": "Polygon", "coordinates": [[
                [-83.9995, 35.0005], [-83.9992, 35.0005],
                [-83.9992, 35.0008], [-83.9995, 35.0008],
                [-83.9995, 35.0005],
            ]]}
            (map_dir / f"{map_name}_buildings.geojson").write_text(json.dumps({
                "type": "FeatureCollection", "features": [{
                    "type": "Feature", "id": "osm/selected/1",
                    "properties": {"building": "house", "building:levels": "2.5",
                                   "name": "Selected KnoxMap House"},
                    "geometry": geometry,
                }],
            }))

            response = module.app.test_client().post("/api/buildings", json={"mapName": map_name})
            output = json.loads((map_dir / f"{map_name}_buildings.geojson").read_text())
            project = ET.parse(map_dir / f"{map_name}.pzw").getroot()
            lots = project.findall(".//lot")
            lot_targets_exist = all((map_dir / "buildings" / lot.get("map").split("/")[-1]).is_file()
                                    for lot in lots)

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()["count"] >= 1)
        self.assertEqual(output["features"][0]["properties"]["building:levels"], "3")
        self.assertTrue(lots)
        self.assertTrue(lot_targets_exist)

    def test_knoxmap_build_hook_installs_idempotently_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "KnoxMap"
            (root / "knoxbuild").mkdir(parents=True)
            (root / "knoxbuild" / "build.py").write_text("def build(): pass\n")
            app = root / "app.py"
            original = """from contextlib import redirect_stdout
def api_buildings():
        with redirect_stdout(out):
            build_buildings(str(map_dir), settings=settings,
                            should_stop=_stopper(map_dir.name))
"""
            app.write_text(original)
            first = install_hook(root, Path(__file__).parent)
            patched = app.read_text()
            backup_content = Path(first["app_backup"]).read_text()
            second = install_hook(root, Path(__file__).parent)
            newer_source = original + "\n# simulated KnoxMap update\n"
            app.write_text(newer_source)
            updated = install_hook(root, Path(__file__).parent)
            updated_backup_content = Path(updated["app_backup"]).read_text()
            updated_source = app.read_text()
            config = json.loads((root / "knoxmap_config.json").read_text())

        self.assertIn(HOOK_MARKER, patched)
        self.assertEqual(patched.count(HOOK_MARKER), 1)
        self.assertEqual(backup_content, original)
        self.assertTrue(second["already_installed"])
        self.assertNotEqual(updated["app_backup"], first["app_backup"])
        self.assertEqual(updated_backup_content, newer_source)
        self.assertEqual(updated_source.count(HOOK_MARKER), 1)
        self.assertEqual(config["orientation_pz_root"], str(Path(__file__).parent.resolve()))

    def test_missing_knoxmap_root_does_not_replace_selected_area_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "town_info.json").write_text(json.dumps({
                "map_name": "town", "bbox": {"south": 0, "west": 0,
                "north": 0.01, "east": 0.01}, "meters_per_tile": 1}))
            (root / "town.bmp").write_bytes(b"terrain")
            source = root / "town_buildings.geojson"
            original = json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"building": "house"},
                 "geometry": square(0.001, 0.001)},
            ]})
            source.write_text(original)
            with self.assertRaisesRegex(ValueError, "knoxmap-root"):
                run_knoxmap_pipeline(root)
            self.assertEqual(source.read_text(), original)

    @unittest.skipUnless(os.environ.get("ORIENTATION_PZ_KNOXMAP_ROOT"),
                         "set ORIENTATION_PZ_KNOXMAP_ROOT to run the pinned KnoxMap integration")
    def test_runs_selected_area_through_real_knoxmap_builder(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = {"map_name": "knoxmap-integration", "bbox": {"south": 35.0,
                    "west": -84.0, "north": 35.002, "east": -83.998},
                    "rotation": 0, "meters_per_tile": 1, "width_tiles": 300,
                    "height_tiles": 300, "cells_x": 1, "cells_y": 1}
            (root / "knoxmap-integration_info.json").write_text(json.dumps(info))
            (root / "settings.json").write_text(json.dumps({"preset": "rural", "true_map": 1}))
            Image.new("RGB", (300, 300), (110, 145, 80)).save(root / "knoxmap-integration.bmp")
            geometry = {"type": "Polygon", "coordinates": [[
                [-83.9995, 35.0005], [-83.9992, 35.0005],
                [-83.9992, 35.0008], [-83.9995, 35.0008],
                [-83.9995, 35.0005],
            ]]}
            (root / "knoxmap-integration_buildings.geojson").write_text(json.dumps({
                "type": "FeatureCollection", "features": [{
                    "type": "Feature", "id": "osm/selected/1",
                    "properties": {"building": "house", "building:levels": "2.5",
                                   "name": "Selected KnoxMap House"},
                    "geometry": geometry,
                }],
            }))

            result = run_knoxmap_pipeline(
                root, knoxmap_root=os.environ["ORIENTATION_PZ_KNOXMAP_ROOT"])
            output = json.loads((root / "knoxmap-integration_buildings.geojson").read_text())
            project = ET.parse(root / "knoxmap-integration.pzw").getroot()
            lots = project.findall(".//lot")
            lot_targets_exist = all((root / "buildings" / lot.get("map").split("/")[-1]).is_file()
                                    for lot in lots)

        self.assertTrue(result["knoxmap_build_run"])
        self.assertEqual(output["features"][0]["properties"]["building:levels"], "3")
        self.assertTrue(lots)
        self.assertTrue(lot_targets_exist)

    def test_pipeline_analyzes_selected_knoxify_data_and_is_rerunnable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = {"map_name": "selected-area", "bbox": {"south": 0, "west": 0,
                    "north": 0.01, "east": 0.01}, "meters_per_tile": 1,
                    "width_tiles": 300, "height_tiles": 300,
                    "cells_x": 1, "cells_y": 1}
            (root / "selected-area_info.json").write_text(json.dumps(info))
            (root / "selected-area.bmp").write_bytes(b"selected-area terrain")
            source_features = [
                {"type": "Feature", "id": f"osm/{index}",
                 "properties": {"building": "house", "building:levels": "2.5"},
                 "geometry": square(0.001 + index * 0.0005, 0.001)}
                for index in range(3)
            ]
            source_features.append({"type": "Feature", "properties": {"highway": "residential"},
                                    "geometry": {"type": "LineString",
                                                 "coordinates": [[0, 0], [0.01, 0.01]]}})
            target = root / "selected-area_buildings.geojson"
            original = json.dumps({"type": "FeatureCollection", "features": source_features})
            target.write_text(original)

            first = run_knoxmap_pipeline(root, build_map=False)
            first_output = json.loads(target.read_text())
            second = run_knoxmap_pipeline(root, build_map=False)
            backup = Path(first["knoxmap_backup"])
            backup_content = backup.read_text()
            final_output = json.loads(target.read_text())

        self.assertEqual(first["selected_features"], 4)
        self.assertEqual(first["generation_ready_features"], 4)
        self.assertEqual(first["local_grids_detected"], 1)
        self.assertEqual(len(first_output["features"]), 3)
        self.assertEqual(first_output["features"][0]["properties"]["building:levels"], "3")
        self.assertEqual(len(final_output["features"]), 3)
        self.assertEqual(second["knoxmap_backup"], str(backup))
        self.assertEqual(backup_content, original)
        self.assertFalse(second["knoxmap_build_run"])

    def test_writes_knoxify_named_collection_and_normalizes_levels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            info = {"map_name": "town", "bbox": {"south": 0, "west": 0,
                    "north": 0.01, "east": 0.01}, "meters_per_tile": 1,
                    "width_tiles": 300, "height_tiles": 300,
                    "cells_x": 1, "cells_y": 1}
            (root / "town_info.json").write_text(json.dumps(info))
            (root / "town.bmp").write_bytes(b"synthetic bitmap marker")
            source = root / "oriented.geojson"
            source.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "id": "osm/1", "properties": {
                    "building": "yes", "building:levels": "2.5", "local_grid_id": 1},
                 "geometry": square(0.001, 0.001)},
                {"type": "Feature", "properties": {
                    "building": "apartments", "num_floors": "1;2"},
                 "geometry": {"type": "MultiPolygon", "coordinates": [
                     square(0.002, 0.002)["coordinates"],
                     square(0.004, 0.004)["coordinates"]]}},
                {"type": "Feature", "properties": {"highway": "residential"},
                 "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}},
                {"type": "Feature", "properties": {"building": "yes", "generation_eligible": False},
                 "geometry": square(0.006, 0.006)},
            ]}))

            result = export_knoxmap_buildings(source, root)
            output = json.loads((root / "town_buildings.geojson").read_text())

        self.assertEqual(result["buildings_written"], 3)
        self.assertEqual(result["skipped_features"], 2)
        self.assertEqual(result["split_multipolygon_components"], 1)
        self.assertEqual(output["type"], "FeatureCollection")
        self.assertEqual(output["features"][0]["properties"]["building:levels"], "3")
        self.assertEqual(output["features"][0]["properties"]["orientation_pz_original_levels"], "2.5")
        self.assertEqual(output["features"][1]["properties"]["building:levels"], "2")
        self.assertEqual(output["features"][2]["geometry"]["type"], "Polygon")
        self.assertEqual(output["features"][0]["id"], "osm/1")

    def test_existing_knoxmap_file_requires_explicit_backup_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "town_info.json").write_text(json.dumps({
                "map_name": "town", "bbox": {"south": 0, "west": 0,
                "north": 0.01, "east": 0.01}, "meters_per_tile": 1}))
            (root / "town.bmp").write_bytes(b"bitmap")
            target = root / "town_buildings.geojson"
            target.write_text("original")
            source = root / "oriented.geojson"
            source.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"building": "yes"},
                 "geometry": square(0.001, 0.001)},
            ]}))
            with self.assertRaises(FileExistsError):
                export_knoxmap_buildings(source, root)
            result = export_knoxmap_buildings(source, root, replace_existing=True)
            self.assertEqual(Path(result["backup"]).read_text(), "original")
            self.assertEqual(json.loads(target.read_text())["type"], "FeatureCollection")


if __name__ == "__main__":
    unittest.main()