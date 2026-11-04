"""PlaneSite - drone mapping and spray mission planning."""
__version__ = "2.0.4"
APP_NAME = "PlaneSite"
UPDATE_REPO = "EdkoLLC/PlaneSite"   # GitHub repo that hosts updates/latest.json
RUNTIME = 3                          # version of the bundled python/ folder this app needs


def _use_own_geodata():
    """Point PROJ and GDAL at the data files bundled with PlaneSite's rasterio.

    rasterio only uses its own PROJ database when PROJ_DATA / PROJ_LIB aren't set. Other GIS
    programs (XGEOMapper, older QGIS or ArcGIS installs, OSGeo4W) often set those for the whole
    computer to their own, older copy, and then every coordinate system fails with "proj.db
    contains DATABASE.LAYOUT.VERSION.MINOR = 3 whereas a number >= 6 is expected". Setting them
    here, before rasterio is imported, also covers the processing workers, which inherit the
    environment. pyproj already prefers its own bundled data."""
    import importlib.util
    import os
    try:
        spec = importlib.util.find_spec("rasterio")
    except Exception:
        return
    if not spec or not spec.submodule_search_locations:
        return
    base = list(spec.submodule_search_locations)[0]
    proj = os.path.join(base, "proj_data")
    gdal = os.path.join(base, "gdal_data")
    if os.path.isfile(os.path.join(proj, "proj.db")):
        for k in ("PROJ_DATA", "PROJ_LIB"):
            if os.environ.get(k) != proj:
                if os.environ.get(k):
                    os.environ["PLANESITE_OUTSIDE_" + k] = os.environ[k]   # noted in diagnostics
                os.environ[k] = proj
    if os.path.isdir(gdal):
        os.environ["GDAL_DATA"] = gdal


_use_own_geodata()


def migrate_from_fieldstitch():
    """One-time carry-over of settings and timing calibration from the app's old name.

    Projects are not carried over: the old "FieldStitch Projects" folder is left alone, and
    recent-project and default-folder entries that point into it are dropped.
    """
    import json, os
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    old, new = os.path.join(base, "FieldStitch"), os.path.join(base, "PlaneSite")
    if not os.path.isdir(old) or os.path.exists(os.path.join(new, ".migrated")):
        return
    try:
        os.makedirs(new, exist_ok=True)
        cal = os.path.join(old, "timing_calibration.json")
        if os.path.isfile(cal) and not os.path.exists(os.path.join(new, "timing_calibration.json")):
            with open(cal, "rb") as f:
                data = f.read()
            with open(os.path.join(new, "timing_calibration.json"), "wb") as f:
                f.write(data)
        cfg_old = os.path.join(old, "config.json")
        if os.path.isfile(cfg_old) and not os.path.exists(os.path.join(new, "config.json")):
            with open(cfg_old, encoding="utf-8") as f:
                cfg = json.load(f)
            def stale(path):
                return "fieldstitch projects" in os.path.normcase(str(path or "")).replace("\\", "/")
            cfg["recent"] = [r for r in cfg.get("recent", []) if not stale(r)]
            if stale(cfg.get("projects_dir")):
                cfg.pop("projects_dir", None)
            with open(os.path.join(new, "config.json"), "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=1)
        open(os.path.join(new, ".migrated"), "w").close()
    except Exception:
        pass
