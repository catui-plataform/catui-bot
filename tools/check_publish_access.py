"""Verify the existing credential can open a store push without writing files."""
import base64
import os
import subprocess

if __name__ == "__main__":
    environment = os.environ.copy()
    token = environment.pop("STORE_PUBLISH_TOKEN", "").strip()
    if not token:
        raise SystemExit("Configure the existing GitHub token in the core-publish environment")
    environment.update(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
        GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode(),
        GIT_CONFIG_KEY_1="credential.helper", GIT_CONFIG_VALUE_1="", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", "store-repo", "push", "--dry-run", "origin", "HEAD:refs/heads/main"],
                            env=environment, capture_output=True, timeout=60)
    if result.returncode:
        raise SystemExit("Store write access was denied. Grant the existing token access to CATui with Contents: Read and write. Nothing was changed.")
    print("Store publishing access verified without changes")
