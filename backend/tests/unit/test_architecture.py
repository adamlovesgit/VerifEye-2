"""Guard the route/recognition/runtime dependency boundaries."""

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2] / "src" / "verifeye"


def imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}


class ArchitectureTests(unittest.TestCase):
    def test_camera_runtime_has_no_web_dependency(self):
        self.assertFalse(any(name.startswith("fastapi") for name in imports(ROOT / "cameras" / "runtime.py")))
    def test_camera_services_have_no_web_dependency(self):
        self.assertFalse(any(name.startswith("fastapi") for name in imports(ROOT / "cameras" / "service.py")))
    def test_routes_do_not_contain_vision_or_capture_dependencies(self):
        route_imports = imports(ROOT / "app.py")
        self.assertTrue({"cv2", "mediapipe", "av"}.isdisjoint(route_imports))


if __name__ == "__main__": unittest.main()
