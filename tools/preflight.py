"""Verify an owner-approved plan without executing emulator source."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catui_bot.security import verify

if __name__ == "__main__":
    plan = verify(json.loads(os.environ["APPROVED_PLAN"]), os.environ["APPROVAL_SIGNATURE"],
                  os.environ["APPROVAL_SIGNING_KEY"], os.environ["DISCORD_OWNER_ID"])
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"core={plan['core']}\nrequest={plan['id']}\n")
    print(f"Verified owner approval for {plan['core']} at {plan['sha']}")
