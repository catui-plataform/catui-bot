import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from tools.build_core import inspect_package
from tools.publish_core import publish


class PipelineTests(unittest.TestCase):
    def test_publication_replaces_only_target_core_without_tags_or_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, artifact = root / "store", root / "artifact"
            core_dir = store / "store/cores/pcsx2"
            core_dir.mkdir(parents=True)
            (core_dir / "source").mkdir()
            other = store / "store/cores/eden/eden.dll"
            other.parent.mkdir()
            other.write_bytes(b"untouched")
            manifest = {"filename": "pcsx2.dll"}
            (core_dir / "manifest.json").write_text(json.dumps(manifest))
            source = artifact / "source"
            source.mkdir(parents=True)
            package = artifact / "pcsx2.dll"
            package.write_bytes(b"MZ-new-build")
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            plan = {"core": "pcsx2", "sha": "b" * 40, "version": "2.8", "store_head": "c" * 40}
            archive = source / "source.tar.gz"
            archive.write_bytes(b"source-fixture")
            (source / "upstream.json").write_text(json.dumps({"commit": plan["sha"],
                "snapshot": {"archive": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()},
                "package": {"filename": package.name, "sha256": digest}}))
            (artifact / "receipt.json").write_text(json.dumps({"plan": plan, "filename": package.name,
                "sha256": digest, "size": package.stat().st_size, "abi": True,
                "runtime": {"passed": True, "evidence": [{"layer": "native"}, {"layer": "catui"}]}}))
            calls = []

            def command(args, **kwargs):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, stdout=plan["store_head"] if "rev-parse" in args else "", stderr="")

            with patch("tools.publish_core.subprocess.run", side_effect=command), patch.dict(
                    "os.environ", {"GITHUB_STEP_SUMMARY": str(root / "summary.txt")}):
                publish(plan, artifact, store, {})
            self.assertEqual((core_dir / package.name).read_bytes(), package.read_bytes())
            self.assertEqual(other.read_bytes(), b"untouched")
            self.assertTrue(any(args[-3:] == ["push", "origin", "HEAD:refs/heads/main"] for args in calls))
            self.assertFalse(any("tag" in args or "gh" in args or any("refs/tags/" in arg for arg in args) for args in calls))
            self.assertTrue(any(args[-2:] == ["--ref", "main"] for args in calls))

    def test_archive_rejects_traversal_and_missing_runtime_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "core.zip"
            manifest = {"packageType": "archive", "entryFilename": "core/core_libretro.dll"}
            for name in ("../evil.dll", "..\\evil.dll", "/evil.dll", "C:/evil.dll"):
                with self.subTest(name=name):
                    with zipfile.ZipFile(path, "w") as output:
                        output.writestr(manifest["entryFilename"], b"MZ")
                        output.writestr(name, b"MZ")
                    with self.assertRaises(ValueError):
                        inspect_package(path, manifest)
            with zipfile.ZipFile(path, "w") as output:
                output.writestr("other.dll", b"MZ")
            with self.assertRaises(ValueError):
                inspect_package(path, manifest)

    def test_valid_archive_hash_and_license(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "core.zip"
            with zipfile.ZipFile(path, "w") as output:
                output.writestr("core.dll", b"MZ-test-fixture")
                output.writestr("LICENSE", "Test fixture")
            self.assertEqual(inspect_package(path, {"packageType": "archive", "entryFilename": "core.dll"}), hashlib.sha256(path.read_bytes()).hexdigest())

    def test_oversized_package_fails_before_reading(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "core.dll"
            with path.open("wb") as output:
                output.truncate(100 * 1024 * 1024)
            with self.assertRaises(ValueError):
                inspect_package(path, {})

    def test_deploy_zip_has_only_runtime(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run([sys.executable, "tools/package_deploy.py"], cwd=root, check=True, capture_output=True, timeout=15)
        with zipfile.ZipFile(root / "dist/catui-bot-squarecloud.zip") as archive:
            names = archive.namelist()
            self.assertIn("main.py", names)
            self.assertIn("squarecloud.app", names)
            for name in names:
                self.assertFalse(any(part in name.split("/") for part in (".git", "node_modules", "__pycache__", "data", ".env")))
                self.assertFalse(name.startswith(("tools/", "tests/", "validation/", "docs/")))

    def test_runtime_validator_refuses_missing_configuration(self):
        root = Path(__file__).resolve().parents[1]
        if (root / "validation/commands.json").exists():
            self.skipTest("Operator has installed real validators")
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.json"
            result = subprocess.run([sys.executable, "validation/validate.py", "--core", "pcsx2", "--package-dir", folder, "--report", str(report)],
                                    cwd=root, capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(report.exists())
