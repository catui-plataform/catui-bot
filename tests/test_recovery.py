import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from tools.build_core import apply_integration_patch, apply_repairs, failure_context
from tools.repair import propose_repair
from tools.run_build import checkpoint_source, execute, repair_python, run
from tools.publish_core import push_verified


class RecoveryTests(unittest.TestCase):
    def test_empty_upstream_integration_patch_is_a_valid_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            patch_path = Path(folder) / "integration.patch"
            patch_path.write_bytes(b"\r\n")
            with patch("tools.build_core.command") as command:
                self.assertEqual(apply_integration_patch(Path(folder), patch_path), [])
            command.assert_not_called()

    def test_supervisor_streams_progress_before_child_process_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            release = root / "release"
            script = (
                "import pathlib, sys, time\n"
                "print('phase-one', flush=True)\n"
                "deadline = time.monotonic() + 5\n"
                "while not pathlib.Path(sys.argv[1]).exists():\n"
                "    if time.monotonic() > deadline: sys.exit(8)\n"
                "    time.sleep(0.01)\n"
                "print('phase-two', file=sys.stderr, flush=True)\n"
                "sys.exit(7)\n")

            def observe(line, **kwargs):
                if "phase-one" in line:
                    release.write_text("continue")

            with patch("tools.run_build.ROOT", root), patch("builtins.print", side_effect=observe):
                result = execute(["-c", script, str(release)])
            self.assertEqual(result.returncode, 7)
            self.assertIn("phase-one", result.stdout)
            self.assertIn("phase-two", result.stdout)

    def test_instruction_hunks_cannot_abort_source_integration_or_hide_conflicts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work = root / "work"
            work.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-C", str(work), *args], stderr=subprocess.DEVNULL)

            git("init")
            git("config", "core.autocrlf", "false")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            for name, content in {"AGENTS.md": "old instructions\n", "CLAUDE.md": "old instructions\n",
                                  "bridge.cpp": "int version = 0;\n", "CMakeLists.txt": "project(fixture)\n"}.items():
                (work / name).write_text(content, encoding="utf-8", newline="\n")
            git("add", ".")
            git("-c", "commit.gpgsign=false", "commit", "-m", "base")
            base = git("rev-parse", "HEAD").decode().strip()
            (work / "AGENTS.md").write_text("integration instructions\n")
            (work / "CLAUDE.md").write_text("integration instructions\n")
            (work / "bridge.cpp").write_text("int version = 1;\n")
            (work / "CMakeLists.txt").write_text("project(fixture)\nadd_subdirectory(libretro)\n")
            patch_path = root / "integration.patch"
            patch_path.write_bytes(git("diff", "--binary", "HEAD"))
            git("restore", "--source=" + base, "--staged", "--worktree", ".")
            (work / "AGENTS.md").write_text("upstream instructions\n")
            (work / "CLAUDE.md").unlink()
            (work / "bridge.cpp").write_text("int version = 2;\n")
            git("add", "-A")
            git("-c", "commit.gpgsign=false", "commit", "-m", "upstream")
            previous = Path.cwd()
            try:
                os.chdir(root)
                conflicts = apply_integration_patch(work, patch_path)
                self.assertEqual(conflicts, ["bridge.cpp"])
                self.assertIn("<<<<<<<", (work / "bridge.cpp").read_text())
                self.assertIn("add_subdirectory(libretro)", (work / "CMakeLists.txt").read_text())
                self.assertEqual((work / "AGENTS.md").read_text(), "upstream instructions\n")
                self.assertFalse((work / "CLAUDE.md").exists())
                apply_repairs(work, [{"path": "bridge.cpp", "content": "int version = 3;\n"}], set(conflicts))
                self.assertEqual(git("diff", "--name-only", "--diff-filter=U"), b"")
            finally:
                os.chdir(previous)

    def test_atomic_patch_failure_does_not_become_an_editable_conflict(self):
        with patch.object(Path, "read_bytes", return_value=b"diff --git"), patch("tools.build_core.command", side_effect=[RuntimeError("missing blob"), ""]), self.assertRaisesRegex(
                RuntimeError, "before source conflicts"):
            apply_integration_patch(Path("work"), Path("integration.patch"))

    def test_python_restart_checkpoint_preserves_completed_source_repairs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work = root / "core-work"
            work.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-C", str(work), *args], stderr=subprocess.DEVNULL)

            git("init")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            source = work / "bridge.cpp"
            source.write_text("// original\n")
            git("add", "bridge.cpp")
            git("-c", "commit.gpgsign=false", "commit", "-m", "fixture")
            sha = git("rev-parse", "HEAD").decode().strip()
            source.write_text("// repaired integration\n")
            git("add", "bridge.cpp")
            with patch("tools.run_build.ROOT", root), patch.dict(os.environ, {"APPROVED_PLAN": json.dumps({"sha": sha})}):
                checkpoint_source()
            saved = json.loads((root / "core-recovery.json").read_text())
            self.assertEqual(saved["sha"], sha)
            git("restore", "--source=HEAD", "--staged", "--worktree", ".")
            patch_path = root / "saved.patch"
            patch_path.write_text(saved["patch"], encoding="utf-8", newline="\n")
            git("apply", "--3way", str(patch_path))
            self.assertEqual(source.read_text(), "// repaired integration\n")

    def test_publication_recovers_network_errors_and_detects_lost_push_response(self):
        failed = subprocess.CalledProcessError(1, ["git", "push"])
        for remote in ("old", "new", "concurrent", ""):
            calls = []

            def git(*args):
                calls.append(args)
                if args[0] == "push" and len(calls) == 1:
                    raise failed
                return remote + "\trefs/heads/main" if args[0] == "ls-remote" and remote else "new" if args[0] == "rev-parse" else ""

            with patch("tools.publish_core.time.sleep"):
                if remote in ("old", "new"):
                    push_verified(git, "old")
                else:
                    with self.assertRaises(subprocess.CalledProcessError):
                        push_verified(git, "old")
            self.assertEqual(sum(c[0] == "push" for c in calls), 2 if remote == "old" else 1)

    def test_current_unknown_target_includes_cmake_wiring_before_old_compiler_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, work = root / "store", root / "core-work"
            source = store / "store/cores/eden/source"
            source.mkdir(parents=True)
            paths = ["old.cpp", "CMakeLists.txt", "src/CMakeLists.txt", "src/eden_libretro/CMakeLists.txt"]
            (source / "upstream.json").write_text(json.dumps({
                "commit": "a" * 40, "integrationDirectory": "src/eden_libretro", "changedFiles": paths}))
            for name in paths:
                path = work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("// fixture")
            (root / "build.log").write_text("old.cpp: error: stale conflict")
            (root / "attempt.log").write_text("ninja: error: unknown target 'eden_libretro'")
            previous = Path.cwd()
            try:
                os.chdir(root)
                ctx = failure_context({"core": "eden", "sha": "b" * 40}, store, work, stage="compile")
            finally:
                os.chdir(previous)
            self.assertEqual([f["path"] for f in ctx["files"]][:3], paths[1:])
            self.assertNotIn("stale conflict", ctx["log"])

    def test_malformed_model_files_are_ignored(self):
        response = MagicMock()
        for payload in ([], {"files": [{"path": [], "content": 1}]},
                        {"files": [{"path": "ok.cpp", "content": None}]}):
            response.__enter__.return_value.read.return_value = json.dumps({
                "choices": [{"message": {"content": json.dumps(payload)}}]}).encode()
            with patch("tools.repair.urllib.request.urlopen", return_value=response):
                self.assertEqual(propose_repair({"files": [{"path": "ok.cpp", "content": "x"}]}, api_key="fixture"), [])

    def test_python_repair_only_accepts_traceback_files_and_valid_python(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            helper = root / "tools/build_core.py"
            helper.parent.mkdir()
            helper.write_text("print(missing)\n")
            log = f'Traceback (most recent call last):\n  File "{helper}", line 1\nNameError: missing'
            with patch("tools.run_build.ROOT", root), patch("tools.run_build.propose_repair", return_value=[{
                    "path": "tools/build_core.py", "content": "print('fixed')\n"}]):
                self.assertTrue(repair_python(log, 1))
            self.assertEqual(helper.read_text(), "print('fixed')\n")
            for name, content in (("tools/publish_core.py", "print('bypass')"), ("tools/build_core.py", "bad syntax!")):
                with patch("tools.run_build.ROOT", root), patch("tools.run_build.propose_repair", return_value=[{
                        "path": name, "content": content}]), self.assertRaises((SyntaxError, ValueError)):
                    repair_python(log, 2)
            self.assertEqual(helper.read_text(), "print('fixed')\n")

    def test_python_repair_reexecutes_fixed_script_and_removes_partial_build(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "core-work").mkdir()
            helper = root / "tools/build_core.py"
            helper.parent.mkdir()
            helper.write_text("print(missing)\n")
            log = f'Traceback (most recent call last):\n  File "{helper}", line 1\nNameError: missing'
            results = [subprocess.CompletedProcess([], 1, "", log), subprocess.CompletedProcess([], 0, "validated", "")]
            with patch("tools.run_build.ROOT", root), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture", "AI_REPAIR_ENABLED": "true"}), patch(
                    "tools.run_build.execute", side_effect=results) as execute, patch(
                    "tools.run_build.propose_repair", return_value=[{"path": "tools/build_core.py", "content": "print('fixed')\n"}]):
                self.assertEqual(run(), 0)
            self.assertEqual(execute.call_count, 2)
            self.assertFalse((root / "core-work").exists())
            self.assertEqual(len(json.loads((root / "tooling-repairs.json").read_text())), 1)

    def test_compiler_failure_is_not_misclassified_as_python_bug(self):
        log = 'Traceback (most recent call last):\nRuntimeError: cmake failed'
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture"}), patch(
                "tools.run_build.execute", return_value=subprocess.CompletedProcess([], 1, "", log)), patch(
                "tools.run_build.repair_python") as repair:
            self.assertEqual(run(), 1)
            repair.assert_not_called()

    def test_python_proposal_failures_share_one_budget(self):
        log = 'Traceback (most recent call last):\nNameError: missing'
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture", "AI_REPAIR_ENABLED": "true", "PYTHON_REPAIR_MAX_ATTEMPTS": "2"}), patch(
                "tools.run_build.execute", return_value=subprocess.CompletedProcess([], 1, "", log)) as execute, patch(
                "tools.run_build.repair_python", side_effect=TimeoutError) as repair, patch("tools.run_build.time.sleep"):
            self.assertEqual(run(), 1)
            self.assertEqual(repair.call_count, 2)
            execute.assert_called_once()
