from timing_editor import _editor_paths, _list_projects, EditorPaths
import os

try:
    paths = _editor_paths("config.yaml")
    print(f"Root dir: {paths.root_dir}")
    print(f"Temp dir: {paths.temp_dir}")
    projects = _list_projects(paths)
    print(f"Found {len(projects)} projects")
    for p in projects:
        print(f" - {p.get('name')} (ID: {p.get('id')})")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
