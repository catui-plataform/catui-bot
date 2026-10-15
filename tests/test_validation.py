import json
from pathlib import Path
import tempfile
import unittest
from validation.core_native import CORE_CONFIGS, find_entry_dll

ROOT = Path(__file__).resolve().parents[1]


class ValidationTests(unittest.TestCase):
    def test_checks_configuration_covers_active_cores(self):
        checks_file = ROOT / "validation/checks.json"
        self.assertTrue(checks_file.is_file())
        checks = json.loads(checks_file.read_text(encoding="utf-8"))
        registry = json.loads((ROOT / "config/cores.json").read_text())
        for core, entry in registry.items():
            if entry.get("build_enabled") is False:
                continue
            self.assertIn(core, checks, f"{core} missing from checks.json")
            self.assertIn(core, CORE_CONFIGS, f"{core} missing native configuration")
            layers = {entry["layer"] for entry in checks[core]}
            self.assertEqual(layers, {"native", "catui"}, f"{core} must define both native and catui layers")

    def test_find_entry_dll_for_cores(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            direct = root / "custom.dll"
            direct.write_bytes(b"dummy")
            self.assertEqual(find_entry_dll(direct, "pcsx2"), direct)

            pcsx2_dll = root / "pcsx2_libretro.dll"
            pcsx2_dll.write_bytes(b"dummy")
            self.assertEqual(find_entry_dll(root, "pcsx2"), pcsx2_dll)

            eden_dll = root / "eden_libretro.dll"
            eden_dll.write_bytes(b"dummy")
            self.assertEqual(find_entry_dll(root, "eden"), eden_dll)

            rpcs3_sub = root / "rpcs3"
            rpcs3_sub.mkdir()
            rpcs3_dll = rpcs3_sub / "rpcs3_libretro.dll"
            rpcs3_dll.write_bytes(b"dummy")
            self.assertEqual(find_entry_dll(root, "rpcs3"), rpcs3_dll)

            shad_dll = root / "shadps4_libretro.dll"
            shad_dll.write_bytes(b"dummy")
            self.assertEqual(find_entry_dll(root, "shadps4"), shad_dll)
