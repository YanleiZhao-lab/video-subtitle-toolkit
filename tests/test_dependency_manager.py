import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from dependency_manager import DependencyManager  # noqa: E402


class DependencyManagerTests(unittest.TestCase):
    def make_manager(self, root: Path, component: dict) -> DependencyManager:
        manifest = root / "dependencies.json"
        manifest.write_text(json.dumps({"components": [component]}), encoding="utf-8")
        return DependencyManager(manifest, root / "data")

    def test_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../outside.txt", "bad")
            with self.assertRaisesRegex(ValueError, "不安全路径"):
                DependencyManager._extract_zip(archive, root / "extract")

    def test_checksum_mismatch_removes_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            source.write_bytes(b"content")
            target = root / "target.bin"
            with self.assertRaisesRegex(ValueError, "校验失败"):
                DependencyManager._download(source.as_uri(), target, "0" * 64, lambda *_: None)
            self.assertFalse(target.exists())

    def test_installs_direct_file_and_reports_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.exe"
            source.write_bytes(b"binary")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manager = self.make_manager(
                root,
                {
                    "id": "tool",
                    "name": "Tool",
                    "kind": "file",
                    "url": source.as_uri(),
                    "sha256": digest,
                    "target": "tool.exe",
                    "destination": "tools/tool",
                    "expected": ["tool.exe"],
                },
            )
            self.assertFalse(manager.state("tool").installed)
            manager.install("tool")
            self.assertTrue(manager.state("tool").installed)
            self.assertEqual((root / "data/tools/tool/tool.exe").read_bytes(), b"binary")

    def test_remove_cannot_escape_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self.make_manager(
                root,
                {
                    "id": "bad",
                    "name": "Bad",
                    "kind": "file",
                    "url": "file:///unused",
                    "target": "bad.exe",
                    "destination": "../outside",
                    "expected": ["bad.exe"],
                },
            )
            with self.assertRaisesRegex(ValueError, "拒绝"):
                manager.remove("bad")

    def test_portable_tools_and_user_models_have_separate_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.exe"
            source.write_bytes(b"binary")
            component = {
                "id": "tool", "name": "Tool", "kind": "file",
                "url": source.as_uri(), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "target": "tool.exe", "destination": "tools/tool", "expected": ["tool.exe"],
            }
            manifest = root / "dependencies.json"
            manifest.write_text(json.dumps({"components": [component, {
                **component, "id": "model", "destination": "models/model"
            }]}), encoding="utf-8")
            manager = DependencyManager(manifest, root / "user-data", tools_root=root / "portable/tools")
            manager.install("tool")
            manager.install("model")
            self.assertTrue((root / "portable/tools/tool/tool.exe").exists())
            self.assertTrue((root / "user-data/models/model/tool.exe").exists())
            manager.remove("tool")
            self.assertFalse((root / "portable/tools/tool").exists())
            self.assertTrue((root / "user-data/models/model/tool.exe").exists())

    def test_portable_destination_cannot_escape_tools_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self.make_manager(root, {
                "id": "bad", "name": "Bad", "kind": "file", "url": "file:///unused",
                "target": "bad.exe", "destination": "tools/../outside", "expected": ["bad.exe"],
            })
            manager = DependencyManager(manager.manifest_path, manager.data_root, root / "portable/tools")
            with self.assertRaisesRegex(ValueError, "拒绝"):
                manager.install("bad")

    def test_unwritable_portable_directory_fails_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = self.make_manager(root, {
                "id": "tool", "name": "Tool", "kind": "file", "url": "file:///unused",
                "target": "tool.exe", "destination": "tools/tool", "expected": ["tool.exe"],
            })
            manager = DependencyManager(manager.manifest_path, manager.data_root, root / "portable/tools")
            existing = root / "portable/tools/tool/tool.exe"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"previous installation")
            with patch("dependency_manager.tempfile.TemporaryFile", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(PermissionError, "写入权限"):
                    manager.install("tool")
            self.assertEqual(existing.read_bytes(), b"previous installation")


if __name__ == "__main__":
    unittest.main()
