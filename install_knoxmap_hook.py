"""Install orientation-pz into KnoxMap's selected-area Build action."""

import argparse
import json
import os
from pathlib import Path
import tempfile


HOOK_MARKER = "        # orientation-pz: selected-area build hook"
BUILD_CALL = '''        with redirect_stdout(out):
            build_buildings(str(map_dir), settings=settings,
                            should_stop=_stopper(map_dir.name))'''
HOOKED_BUILD_CALL = '''        with redirect_stdout(out):
            from knoxpaths import load_config
            orientation_pz_root = load_config().get("orientation_pz_root")
            if orientation_pz_root:
                if orientation_pz_root not in sys.path:
                    sys.path.insert(0, orientation_pz_root)
                from knoxmap_pipeline import run_knoxmap_pipeline
                run_knoxmap_pipeline(
                    map_dir, knoxmap_root=BASE_DIR,
                    should_stop=_stopper(map_dir.name))
            else:
                build_buildings(str(map_dir), settings=settings,
                                should_stop=_stopper(map_dir.name))'''


def _atomic_write(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}-", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def install_hook(knoxmap_root, orientation_pz_root):
    root = Path(knoxmap_root).resolve()
    orientation_root = Path(orientation_pz_root).resolve()
    app_path = root / "app.py"
    config_path = root / "knoxmap_config.json"
    if not (root / "knoxbuild" / "build.py").is_file() or not app_path.is_file():
        raise ValueError("KnoxMap root must contain app.py and knoxbuild/build.py")
    if not (orientation_root / "knoxmap_pipeline.py").is_file():
        raise ValueError("orientation-pz root must contain knoxmap_pipeline.py")

    app_source = app_path.read_text(encoding="utf-8")
    if HOOK_MARKER not in app_source:
        if app_source.count(BUILD_CALL) != 1:
            raise ValueError("KnoxMap Build handler did not match the supported source; no files changed")
        backup = app_path.with_suffix(app_path.suffix + ".orientation-pz.bak")
        source_bytes = app_source.encode("utf-8")
        if backup.exists() and backup.read_bytes() != source_bytes:
            import hashlib

            digest = hashlib.sha256(source_bytes).hexdigest()[:12]
            backup = app_path.with_suffix(
                app_path.suffix + f".orientation-pz.{digest}.bak")
        if backup.exists() and backup.read_bytes() != source_bytes:
            raise FileExistsError(f"refusing to overwrite KnoxMap source backup: {backup}")

        import ast

        ast.parse(app_source)
        hooked_source = app_source.replace(
            BUILD_CALL, f"{HOOK_MARKER}\n" + HOOKED_BUILD_CALL, 1)
        ast.parse(hooked_source)
    else:
        backup = app_path.with_suffix(app_path.suffix + ".orientation-pz.bak")
        hooked_source = app_source

    try:
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except json.JSONDecodeError as error:
        raise ValueError(f"KnoxMap config is invalid JSON: {error}") from None
    if not isinstance(config, dict):
        raise ValueError("KnoxMap config must contain a JSON object")
    config["orientation_pz_root"] = str(orientation_root)

    if HOOK_MARKER not in app_source:
        if not backup.exists():
            with tempfile.NamedTemporaryFile("wb", dir=root, prefix=f".{app_path.name}-", delete=False) as staged:
                staged_path = Path(staged.name)
                staged.write(source_bytes)
                staged.flush()
                os.fsync(staged.fileno())
            try:
                os.replace(staged_path, backup)
            except BaseException:
                staged_path.unlink(missing_ok=True)
                raise
        try:
            _atomic_write(app_path, hooked_source)
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
    _atomic_write(config_path, json.dumps(config, indent=2) + "\n")
    return {"knoxmap_root": str(root), "orientation_pz_root": str(orientation_root),
            "app_backup": str(backup), "already_installed": HOOK_MARKER in app_source}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("knoxmap_root", type=Path, help="KnoxMap folder containing app.py")
    parser.add_argument("--orientation-pz-root", type=Path, default=Path(__file__).parent,
                        help="orientation-pz source folder (defaults to this tool's folder)")
    args = parser.parse_args()
    try:
        print(json.dumps(install_hook(args.knoxmap_root, args.orientation_pz_root), indent=2))
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()