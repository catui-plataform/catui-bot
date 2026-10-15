"""Publish verified build artifacts in a separate job with restricted credentials."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catui_bot.security import verify
from tools.build_core import inspect_package


def push_verified(git, approved_head):
    for attempt in range(3):
        try:
            git("push", "origin", "HEAD:refs/heads/main")
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # A lost response can follow a successful push. Confirm before retrying.
            refs = git("ls-remote", "origin", "refs/heads/main").split()
            remote = refs[0] if refs else ""
            if remote == git("rev-parse", "HEAD"):
                return
            if remote != approved_head or attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def publish(plan, artifact, store, env):
    def git(*args):
        result = subprocess.run(["git", "-C", str(store), *args], env=env, text=True,
                                capture_output=True, check=True, timeout=120)
        return result.stdout.strip()

    if git("rev-parse", "HEAD") != plan["store_head"]:
        raise ValueError("Store changed since approval; request a new comparison")
    receipt = json.loads((artifact / "receipt.json").read_text())
    if receipt["plan"] != plan or receipt.get("abi") is not True or receipt.get("runtime", {}).get("passed") is not True:
        raise ValueError("Build receipt does not match approved request")
    if not {"native", "catui"} <= {e.get("layer") for e in receipt["runtime"].get("evidence", [])}:
        raise ValueError("Missing runtime validation layers")
    core_dir = store / "store/cores" / plan["core"]
    manifest = json.loads((core_dir / "manifest.json").read_text())
    filename = manifest.get("filename", plan["core"] + "_libretro.dll")
    package = artifact / filename
    if package.is_symlink() or receipt["filename"] != filename:
        raise ValueError("Invalid package filename")
    digest = inspect_package(package, manifest)
    if digest != receipt["sha256"] or package.stat().st_size != receipt["size"]:
        raise ValueError("Artifact checksum mismatch")
    source = artifact / "source"
    metadata = json.loads((source / "upstream.json").read_text())
    if metadata["commit"] != plan["sha"]:
        raise ValueError("Source provenance mismatch")
    snapshot = source / "source.tar.gz"
    if (not snapshot.is_file() or snapshot.is_symlink()
            or metadata.get("snapshot", {}).get("archive") != snapshot.name
            or hashlib.sha256(snapshot.read_bytes()).hexdigest() != metadata["snapshot"].get("sha256")
            or metadata.get("package") != {"filename": filename, "sha256": digest}):
        raise ValueError("Source snapshot does not match the build receipt")
    for path in source.rglob("*"):
        if path.is_symlink() or any(part in {".git", ".github", "node_modules", "__pycache__"} for part in path.relative_to(source).parts):
            raise ValueError("Unexpected source artifact")
        limit = 100 if path == source / "source.tar.gz" else 15
        if path.is_file() and path.stat().st_size >= limit * 1024 * 1024:
            raise ValueError("Source file exceeds limit")
    manifest.update(version=plan["version"], upstreamCommit=plan["sha"])
    shutil.copy2(package, core_dir / filename)
    (core_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    shutil.rmtree(core_dir / "source")
    shutil.copytree(source, core_dir / "source")
    (core_dir / "source/build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="\n")
    subprocess.run(["node", "store/build-catalog.mjs", "--ref", "main"], cwd=store, check=True, timeout=60)
    subprocess.run(["node", "store/build-catalog.mjs", "--check"], cwd=store, check=True, timeout=60)
    git("config", "user.name", "CATui Bot")
    git("config", "user.email", "catui-bot@users.noreply.github.com")
    git("add", "--", f"store/cores/{plan['core']}", "store/catalog.json")
    git("diff", "--cached", "--check", "--", f"store/cores/{plan['core']}/manifest.json",
        f"store/cores/{plan['core']}/source/upstream.json", "store/catalog.json")
    git("commit", "-m", f"Update {plan['core']} to {plan['version']}"[:72])
    # A concurrent store update fails instead of being overwritten. No release or tag.
    push_verified(git, plan["store_head"])
    summary = f"Updated {plan['core']} to {plan['version']}\nUpstream: {plan['sha']}\nSHA-256: {digest}\n"
    Path(os.environ.get("GITHUB_STEP_SUMMARY", "publish-summary.txt")).write_text(summary)
    print(summary)


if __name__ == "__main__":
    plan = verify(json.loads(os.environ["APPROVED_PLAN"]), os.environ["APPROVAL_SIGNATURE"],
                  os.environ["APPROVAL_SIGNING_KEY"], os.environ["DISCORD_OWNER_ID"])
    env = os.environ.copy()
    # Credentials stay in this publisher process and never appear in URLs or CLI arguments.
    token = env.pop("STORE_PUBLISH_TOKEN")
    for name in ("APPROVAL_SIGNING_KEY", "DEEPSEEK_API_KEY", "DISCORD_TOKEN"):
        env.pop(name, None)
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
               GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode())
    publish(plan, Path("core-output").resolve(), Path("store-repo").resolve(), env)
