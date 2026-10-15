"""Create a small Square Cloud ZIP from an explicit file allowlist."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
destination = root / "dist/catui-bot-squarecloud.zip"
destination.parent.mkdir(exist_ok=True)
files = [root / name for name in ("main.py", "requirements.txt", "squarecloud.app")]
files += list((root / "catui_bot").glob("*.py"))
files += [root / "config/cores.json"]
with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, path.relative_to(root))
print(destination)
