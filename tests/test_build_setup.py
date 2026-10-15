import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.build_core import apply_repairs, failure_context, load_recipe, single_dll_check
from tools.prepare_dependencies import vcpkg_dependencies
from tools.repair import propose_repair


class BuildSetupTests(unittest.TestCase):
    def test_upstream_native_core_dependency_cli_accepts_registered_targets(self):
        script = Path(__file__).resolve().parents[1] / "tools/prepare_dependencies.py"
        for core in ("ppsspp", "flycast"):
            result = subprocess.run([sys.executable, str(script), "--core", core],
                                    env={key: value for key, value in os.environ.items() if key != "GITHUB_ENV"},
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_core_recipes_bootstrap_missing_store_configuration_and_preserve_store_recipe(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            for core in ("ppsspp", "flycast", "cemu"):
                recipe = load_recipe(core, source)
                self.assertIn("-DENABLE_LIBRETRO=ON" if core == "cemu" else "-DLIBRETRO=ON", recipe["cmake"])
                self.assertEqual(recipe["outputs"][0]["name"], core + "_libretro.dll")
            saved = {"cmake": ["-DCUSTOM=ON"], "targets": ["custom"], "outputs": []}
            (source / "build.json").write_text(json.dumps(saved))
            self.assertEqual(load_recipe("ppsspp", source), saved)

    def test_configuration_failure_without_local_patch_still_supplies_build_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = Path.cwd()
            try:
                os.chdir(folder)
                root = Path(folder)
                store, work = root / "store", root / "core-work"
                source = store / "store/cores/flycast/source"
                source.mkdir(parents=True)
                (source / "upstream.json").write_text(json.dumps({
                    "changedFiles": [], "integrationDirectory": "shell/libretro"}))
                work.mkdir()
                (work / "CMakeLists.txt").write_text("find_package(Missing REQUIRED)\n")
                ctx = failure_context({"core": "flycast", "sha": "b" * 40}, store, work,
                                      stage="compile", error="CMake configure failed")
                self.assertEqual([item["path"] for item in ctx["files"]], ["CMakeLists.txt"])
            finally:
                os.chdir(previous)

    def test_single_dll_gate_rejects_extra_runtime_and_wrong_architecture(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "core.dll"
            data = bytearray(256)
            struct.pack_into("<I", data, 0x3c, 0x80)
            data[0x80:0x84] = b"PE\0\0"
            struct.pack_into("<H", data, 0x84, 0x8664)
            binary.write_bytes(data)
            with patch("tools.build_core.command", return_value="    KERNEL32.dll\n    api-ms-win-core-path-l1-1-0.dll\n"):
                self.assertIn("kernel32.dll", single_dll_check(binary))
            with patch("tools.build_core.command", return_value="    KERNEL32.dll\n    VCRUNTIME140.dll\n"):
                with self.assertRaisesRegex(ValueError, "vcruntime140"):
                    single_dll_check(binary)
            struct.pack_into("<H", data, 0x84, 0x14c)
            binary.write_bytes(data)
            with self.assertRaisesRegex(ValueError, "x64"):
                single_dll_check(binary)

    def test_static_dependency_installation_is_pinned_and_has_no_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = Path.cwd()
            try:
                os.chdir(folder)
                calls = []

                def run(args, **kwargs):
                    calls.append((args, kwargs))
                    return subprocess.CompletedProcess(args, 0, stdout=b"")

                with patch("tools.prepare_dependencies.subprocess.run", side_effect=run), patch.dict(
                        os.environ, {"DEEPSEEK_API_KEY": "fixture", "GITHUB_TOKEN": "fixture", "APPROVAL_SIGNING_KEY": "fixture"}):
                    settings = vcpkg_dependencies("pcsx2", {"commit": "a" * 40, "triplet": "x64-windows-static"})
                self.assertEqual(settings["VCPKG_TARGET_TRIPLET"], "x64-windows-static")
                self.assertEqual(settings["VCPKG_MANIFEST_INSTALL"], "OFF")
                self.assertTrue(any("fetch" in args and "a" * 40 in args for args, _ in calls))
                self.assertTrue(any("install" in args and "--triplet=x64-windows-static" in args for args, _ in calls))
                for _, options in calls:
                    self.assertNotIn("GITHUB_TOKEN", options["env"])
                    self.assertNotIn("DEEPSEEK_API_KEY", options["env"])
                    self.assertNotIn("APPROVAL_SIGNING_KEY", options["env"])
            finally:
                os.chdir(previous)

    def test_dependency_configuration_cannot_use_moving_refs_or_dynamic_crt(self):
        for config in ({"commit": "master", "triplet": "x64-windows-static"},
                       {"commit": "a" * 40, "triplet": "x64-windows"}):
            with self.assertRaises(ValueError), patch("tools.prepare_dependencies.subprocess.run") as run:
                vcpkg_dependencies("pcsx2", config)
            run.assert_not_called()

    def test_failure_context_prioritizes_error_files_and_modern_adapters_over_vendor_c_files(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = Path.cwd()
            try:
                os.chdir(folder)
                root = Path(folder)
                store = root / "store"
                work = root / "core-work"
                source = store / "store/cores/pcsx2/source"
                source.mkdir(parents=True)
                vendor_files = [f"libretro/libretro-common/compat/compat_{i:02d}.c" for i in range(25)]
                changed = vendor_files + ["libretro/modern_host.cpp", "libretro/modern_main.cpp", "pcsx2/CMakeLists.txt"]
                (source / "upstream.json").write_text(json.dumps({
                    "repository": "https://github.com/PCSX2/pcsx2.git",
                    "commit": "a" * 40,
                    "changedFiles": changed,
                }))
                for rel in changed + ["pcsx2/GS/Renderers/DX12/D3D12ShaderCache.cpp"]:
                    target = work / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(f"// {rel}\n", encoding="utf-8")
                Path("build.log").write_text(
                    "pcsx2/GS/Renderers/DX12/D3D12ShaderCache.cpp(283): error C2065: undeclared identifier\n",
                    encoding="utf-8",
                )
                ctx = failure_context({"core": "pcsx2", "sha": "b" * 40}, store, work, stage="compile", error="compile failed")
                selected = [item["path"] for item in ctx["files"]]
                self.assertEqual(selected[0], "pcsx2/GS/Renderers/DX12/D3D12ShaderCache.cpp")
                self.assertIn("libretro/modern_host.cpp", selected[:5])
                self.assertIn("libretro/modern_main.cpp", selected[:5])
                self.assertIn("pcsx2/CMakeLists.txt", selected[:5])
            finally:
                os.chdir(previous)

    def test_apply_repairs_and_propose_repair_enforce_allowed_paths_and_reject_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder) / "work"
            (work / "libretro").mkdir(parents=True)
            (work / "libretro/modern_host.cpp").write_text("// old\n", encoding="utf-8")
            with patch("tools.build_core.command") as cmd:
                applied = apply_repairs(
                    work,
                    [{"path": "libretro/modern_host.cpp", "content": "// updated\n"}],
                    {"libretro/modern_host.cpp"},
                )
            self.assertEqual(applied, ["libretro/modern_host.cpp"])
            self.assertEqual((work / "libretro/modern_host.cpp").read_text(encoding="utf-8"), "// updated\n")
            cmd.assert_called_once()
            with self.assertRaises(ValueError):
                apply_repairs(work, [{"path": "../escape.cpp", "content": "x"}], {"../escape.cpp"})
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "choices": [{"message": {"content": json.dumps({
                    "files": [{"path": "libretro/modern_host.cpp", "content": "int ok = 1;\n"}]
                })}}]
            }).encode()
            with patch("tools.repair.urllib.request.urlopen", return_value=response):
                proposed = propose_repair({"files": [{"path": "libretro/modern_host.cpp", "content": "// old"}]}, api_key="fixture")
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["path"], "libretro/modern_host.cpp")

            # Markdown-wrapped response
            response.reset_mock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "choices": [{"message": {"content": "```json\n" + json.dumps({
                    "files": [{"path": "libretro/modern_host.cpp", "content": "int ok = 2;\n"}]
                }) + "\n```"}}]
            }).encode()
            with patch("tools.repair.urllib.request.urlopen", return_value=response):
                proposed = propose_repair({"files": [{"path": "libretro/modern_host.cpp", "content": "// old"}]}, api_key="fixture")
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["content"], "int ok = 2;\n")

            # Truncated response recovery
            response.reset_mock()
            truncated_content = '{"files": [{"path": "libretro/modern_host.cpp", "content": "int ok = 3;\\n"}, {"path": "libretro/other.cpp", "content": "truncated'
            response.__enter__.return_value.read.return_value = json.dumps({
                "choices": [{"message": {"content": truncated_content}}]
            }).encode()
            with patch("tools.repair.urllib.request.urlopen", return_value=response):
                proposed = propose_repair({"files": [{"path": "libretro/modern_host.cpp", "content": "// old"}]}, api_key="fixture")
            self.assertEqual(len(proposed), 1)
            self.assertEqual(proposed[0]["content"], "int ok = 3;\n")
