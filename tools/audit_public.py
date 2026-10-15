"""Reject common credential files/patterns in tracked public source."""
from pathlib import Path
import re
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
paths = subprocess.run(["git", "ls-files", "--cached", "-z"], cwd=root, capture_output=True, check=True, timeout=30).stdout.decode().split("\0")
patterns = [re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
            re.compile(rb"gh[pousr]_[A-Za-z0-9_]{30,}"), re.compile(rb"sk-[A-Za-z0-9_-]{24,}"),
            re.compile(rb"[a-zA-Z]:\\[Uu]sers\\"),
            re.compile(rb"474757466304020490|1448705912490430484|1447762781075017839")]
issues = []
for name in filter(None, paths):
    if name == "tools/audit_public.py":
        continue
    path = root / name
    if not path.is_file():
        continue
    if path.name in {".env", "state.json", "state.db"} or path.name.startswith(".env.") or path.suffix in {".pem", ".key", ".sqlite3"}:
        issues.append(name)
    elif any(pattern.search(path.read_bytes()) for pattern in patterns):
        issues.append(name)
if issues:
    print("Public source audit failed; inspect these files locally: " + ", ".join(issues), file=sys.stderr)
    sys.exit(1)
print("No credential files or supported secret patterns in tracked source")
