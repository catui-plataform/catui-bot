"""Run explicitly configured, bounded native/CATui validators."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import sys


def validation_failure(check, output, reason):
    log = Path(check["log"])
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write("\n" + output.decode("utf-8", errors="replace"))
    print(log.read_text(encoding="utf-8", errors="replace")[-20000:], file=sys.stderr)
    raise SystemExit(f"{check['layer']} validation failed: {reason}; see {log}")


def validate(core, package_dir, report, checks=None):
    if checks is None:
        config = Path(__file__).with_name("commands.json")
        if not config.exists():
            config = Path(__file__).with_name("checks.json")
        if not config.exists():
            raise SystemExit("No compatibility checks are configured")
        checks = json.loads(config.read_text()).get(core, [])
    layers = {check["layer"] for check in checks}
    if not {"native", "catui"} <= layers:
        raise SystemExit("Both native and CATui validation layers are required; an ABI check alone is insufficient.")
    evidence = []
    for check in checks:
        command = [part.replace("{package}", str(Path(package_dir).resolve())).replace("{python}", sys.executable).replace("{core}", core) for part in check["command"]]
        started = time.time()
        try:
            result = subprocess.run(command, capture_output=True, timeout=min(240, int(check.get("timeout", 120))))
        except subprocess.TimeoutExpired as exc:
            validation_failure(check, (exc.stdout or b"") + (exc.stderr or b""), f"timed out after {exc.timeout}s")
        if result.returncode:
            code = result.returncode & 0xffffffff
            validation_failure(check, result.stdout + result.stderr, f"exit {result.returncode} (0x{code:08X})")
        log = Path(check["log"])
        if not log.is_file() or log.stat().st_size == 0 or log.stat().st_mtime < started:
            raise SystemExit("Validator did not produce its required log")
        success = check.get("success")
        if not success or success not in log.read_text(encoding="utf-8", errors="replace"):
            raise SystemExit("Validator did not produce its required success marker")
        evidence.append({"layer": check["layer"], "log": log.name,
                         "scope": check.get("scope", "unspecified"),
                         "sha256": hashlib.sha256(log.read_bytes()).hexdigest()})
    Path(report).write_text(json.dumps({"core": core, "passed": True, "gameplayTested": False, "evidence": evidence}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", required=True)
    parser.add_argument("--package-dir", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    validate(args.core, args.package_dir, args.report)
