import contextlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.build_core import failure_context
from tools.integration_fixes import apply_known_fixes
from tools.repair import propose_repair
from validation.core_native import probe
from validation.validate import validate


class RuntimeRecoveryTests(unittest.TestCase):
    def test_validator_timeout_exposes_the_native_last_call_to_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log = root / "native.log"
            log.write_text("CORE_NATIVE_STEP: retro_init\n")
            checks = [{"layer": "native", "command": ["probe"], "log": str(log)},
                      {"layer": "catui", "command": ["probe"], "log": str(root / "catui.log")}]
            output = io.StringIO()
            with patch("validation.validate.subprocess.run", side_effect=subprocess.TimeoutExpired("probe", 90)), contextlib.redirect_stderr(output):
                with self.assertRaisesRegex(SystemExit, "native validation failed: timed out"):
                    validate("flycast", root, root / "report.json", checks)
            self.assertIn("retro_init", output.getvalue())
            self.assertFalse((root / "report.json").exists())

    def test_ppsspp_handles_missing_content_and_unavailable_graphics_without_dereferencing_null(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / "libretro/libretro.cpp"
            source.parent.mkdir()
            source.write_text('bool retro_load_game(const struct retro_game_info *game) {\n'
                              '   retro_pixel_format fmt;\n'
                              '   ctx       = LibretroGraphicsContext::CreateGraphicsContext();\n'
                              '   ctx->Ident();\n   return true;\n}\n')
            self.assertEqual(apply_known_fixes("ppsspp", work), ["libretro/libretro.cpp"])
            text = source.read_text()
            self.assertLess(text.index("if (!game"), text.index("CreateGraphicsContext"))
            self.assertLess(text.index("if (!ctx)"), text.index("ctx->Ident"))
            self.assertIn("return true;", text)
            self.assertEqual(apply_known_fixes("ppsspp", work), [])

    def test_flycast_keeps_gnu_linker_flags_out_of_msvc_builds(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / "CMakeLists.txt"
            source.write_text('if(NOT CMAKE_SYSTEM_NAME STREQUAL "OpenBSD")\n'
                              'set(CMAKE_SHARED_LINKER_FLAGS "-Wl,--no-undefined")\nendif()\n'
                              'set_target_properties(${PROJECT_NAME} PROPERTIES LINK_FLAGS_RELEASE -s)\n')
            self.assertEqual(apply_known_fixes("flycast", work), ["CMakeLists.txt"])
            text = source.read_text()
            self.assertIn('if(NOT MSVC AND NOT CMAKE_SYSTEM_NAME', text)
            self.assertIn('if(NOT MSVC)\nset_target_properties', text)
            self.assertEqual(apply_known_fixes("flycast", work), [])

    def test_eden_rejects_unknown_content_before_starting_the_emulation_system(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            source = work / "src/eden_libretro/libretro_core.cpp"
            source.parent.mkdir(parents=True)
            source.write_text('#include "libretro_core.h"\nbool LibretroSession::LoadGame(const char* filepath) {\n'
                              '    if (is_game_loaded) { UnloadGame(); }\n'
                              '    system->Initialize();\n    return true;\n}\n')
            self.assertEqual(apply_known_fixes("eden", work), [source.relative_to(work).as_posix()])
            text = source.read_text()
            self.assertLess(text.index("Loader::IdentifyFile"), text.index("system->Initialize"))
            self.assertIn("return false;", text)
            self.assertIn("return true;", text)
            self.assertEqual(apply_known_fixes("eden", work), [])
            self.assertEqual(source.read_text(), text)

    def test_probe_retains_the_last_call_on_failure_without_a_success_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "native.log"

            def fail(core, package, trace):
                trace("cycle=1 retro_load_game(invalid)")
                raise OSError("access violation")

            with patch("validation.core_native._probe", side_effect=fail), self.assertRaises(OSError):
                probe("eden", Path(folder), log)
            self.assertIn("retro_load_game(invalid)", log.read_text())
            self.assertNotIn("NATIVE_OK", log.read_text())

    def test_child_validator_errors_are_retained_and_not_reported_as_passed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            log, report = root / "native.log", root / "report.json"
            checks = [{"layer": "native", "command": [sys.executable, "-c", "import sys; print('actual child error', file=sys.stderr); sys.exit(17)"],
                       "log": str(log), "success": "NATIVE_OK"},
                      {"layer": "catui", "command": [], "log": str(root / "catui.log"), "success": "CATUI_OK"}]
            output = io.StringIO()
            with contextlib.redirect_stderr(output), self.assertRaisesRegex(SystemExit, "0x00000011"):
                validate("eden", root, report, checks)
            self.assertIn("actual child error", output.getvalue())
            self.assertIn("actual child error", log.read_text())
            self.assertFalse(report.exists())

    def test_runtime_crash_context_prioritizes_the_integration_entry_point(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work, store = root / "core-work", root / "store"
            metadata = store / "store/cores/eden/source/upstream.json"
            metadata.parent.mkdir(parents=True)
            paths = ["src/common.cpp", "src/eden_libretro/libretro_core.cpp", "src/eden_libretro/environment.cpp"]
            metadata.write_text(json.dumps({"changedFiles": paths, "integrationDirectory": "src/eden_libretro"}))
            for path in paths:
                target = work / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("// current source")
            (root / "build.log").write_text("src/common.cpp: old compiler error")
            (root / "attempt.log").write_text("CORE_NATIVE_STEP: retro_load_game(invalid)\nnative validation failed: 0xC0000005")
            previous = Path.cwd()
            try:
                os.chdir(root)
                context = failure_context({"core": "eden", "sha": "a" * 40}, store, work, stage="validation")
            finally:
                os.chdir(previous)
            self.assertEqual(context["files"][0]["path"], "src/eden_libretro/libretro_core.cpp")
            self.assertNotIn("old compiler error", context["log"])

    def test_targeted_model_edits_preserve_surrounding_source(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps({
            "edits": [{"path": "bridge.cpp", "old": "int old = 0;", "new": "int fixed = 1;"}]})}}]}).encode()
        with patch("tools.repair.urllib.request.urlopen", return_value=response):
            files = propose_repair({"files": [{"path": "bridge.cpp", "content": "// keep\nint old = 0;\n// also keep\n"}]}, api_key="fixture")
        self.assertEqual(files, [{"path": "bridge.cpp", "content": "// keep\nint fixed = 1;\n// also keep\n"}])

    def test_targeted_repair_can_preserve_a_large_cemu_adapter(self):
        original = "// surrounding code\n" * 12000 + "int old = 0;\n"
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps({
            "edits": [{"path": "CemuLibretro.cpp", "old": "int old = 0;", "new": "int fixed = 1;"}]})}}]}).encode()
        with patch("tools.repair.urllib.request.urlopen", return_value=response):
            files = propose_repair({"files": [{"path": "CemuLibretro.cpp", "content": original}]}, api_key="fixture")
        self.assertEqual(files, [{"path": "CemuLibretro.cpp", "content": original.replace("int old = 0;", "int fixed = 1;")}])
