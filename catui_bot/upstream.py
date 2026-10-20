import base64
import json
import re
import time
from urllib.parse import quote
from .config import BOT_REPO, CORE_IDS, ROOT, STORE_REPO
from .http import RemoteError


class Upstream:
    def __init__(self, http):
        self.http = http
        self.registry = json.loads((ROOT / "config/cores.json").read_text())
        self.release_cache = {}
        if set(self.registry) != set(CORE_IDS):
            raise ValueError("Core registry mismatch")
        if any(entry["mode"] != "commit" or not entry.get("branch") for entry in self.registry.values()):
            raise ValueError("Core watchers must specify an upstream branch")

    async def content(self, path, ref="main"):
        data = await self.http.json(f"https://api.github.com/repos/{STORE_REPO}/contents/{path}?ref={quote(ref, safe='')}")
        return json.loads(base64.b64decode(data["content"]))

    async def store_head(self):
        data = await self.http.json(f"https://api.github.com/repos/{STORE_REPO}/git/ref/heads/main")
        return data["object"]["sha"]

    async def latest(self, core):
        entry = self.registry[core]
        commit = await self.http.json(f"{entry['api']}/commits/{quote(entry['branch'], safe='')}")
        sha = commit["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("Invalid upstream commit SHA")
        return {"sha": sha, "version": sha[:12],
                "notes": str(commit.get("commit", {}).get("message", "")).split("\n", 1)[0][:300]}

    @staticmethod
    def release_metadata(release, entry):
        # Construct links from the configured repository; never follow URLs in release prose.
        repository = entry["repository"].removesuffix(".git")
        tag = str(release["tag_name"])[:100]
        return {"tag": tag, "title": str(release.get("name") or tag)[:200],
                "body": str(release.get("body") or "")[:12000],
                "url": repository + "/releases/tag/" + quote(tag, safe=""),
                "published_at": str(release.get("published_at") or "")[:40],
                "prerelease": bool(release.get("prerelease"))}

    async def latest_release(self, core):
        cached = self.release_cache.get(core)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        entry = self.registry[core]
        if entry.get("canonical_api"):
            # The Eden GitHub mirror has no releases. Its canonical Forgejo API is authoritative.
            releases = await self.http.json(entry["canonical_api"] + "/releases?limit=10")
            public = [r for r in releases if not r.get("draft") and not r.get("prerelease")]
            release = max(public, key=lambda r: r.get("published_at") or "", default=None)
        else:
            try:
                release = await self.http.json(entry["api"] + "/releases/latest")
            except RemoteError as exc:
                if exc.status != 404:
                    raise
                release = None
        if release and (release.get("draft") or release.get("prerelease")):
            release = None
        result = self.release_metadata(release, entry) if release else None
        self.release_cache[core] = (time.monotonic(), result)
        return result

    async def compare(self, core, base, target):
        for value in (base, target):
            if not re.fullmatch(r"[a-f0-9]{40}", value):
                raise ValueError("Expected commit SHA")
        data = await self.http.json(f"{self.registry[core]['api']}/compare/{base}...{target}")
        return {"status": data.get("status", "ahead" if data.get("commits") else "identical"),
                "ahead_by": data.get("ahead_by"), "behind_by": data.get("behind_by"),
                "commits": [{"sha": c["sha"][:12], "subject": c["commit"]["message"].splitlines()[0][:300]} for c in data.get("commits", [])[:40]],
                "files": [{"path": f.get("filename"), "status": f.get("status")} for f in data.get("files", [])[:60]],
                "total_commits": data.get("total_commits"), "bounded": True}

    async def dispatch(self, payload, signature):
        return await self.http.json(f"https://api.github.com/repos/{BOT_REPO}/actions/workflows/core-update.yml/dispatches",
            method="POST", payload={"ref": "main", "inputs": {"plan": json.dumps(payload, separators=(",", ":")), "signature": signature}})

    async def runs(self):
        data = await self.http.json(f"https://api.github.com/repos/{BOT_REPO}/actions/workflows/core-update.yml/runs?event=workflow_dispatch&per_page=50")
        return data.get("workflow_runs", [])

    async def bot_head(self):
        data = await self.http.json(f"https://api.github.com/repos/{BOT_REPO}/git/ref/heads/main")
        return data["object"]["sha"]

    async def cancel(self, run_id):
        if not str(run_id).isdigit():
            raise ValueError("Invalid workflow run ID")
        await self.http.json(f"https://api.github.com/repos/{BOT_REPO}/actions/runs/{run_id}/cancel", method="POST")

    async def diagnostics(self, run_id):
        if not str(run_id).isdigit():
            raise ValueError("Invalid workflow run ID")
        data = await self.http.json(f"https://api.github.com/repos/{BOT_REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=50")
        return [{"job": job["name"], "result": job["conclusion"],
                 "failed_steps": [step["name"] for step in job.get("steps", []) if step.get("conclusion") == "failure"]}
                for job in data.get("jobs", [])]

    async def stage(self, run_id):
        if not str(run_id).isdigit():
            raise ValueError("Invalid workflow run ID")
        data = await self.http.json(f"https://api.github.com/repos/{BOT_REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=50")
        jobs = data.get("jobs", [])
        if not jobs:
            return "Starting workflow on GitHub Actions"

        by_name = {}
        for job in jobs:
            jname = (job.get("name") or "").lower()
            if "publish" in jname:
                by_name["publish"] = job
            elif "build" in jname or "compile" in jname:
                by_name["build"] = job
            elif "review" in jname:
                by_name["review"] = job
            elif "access" in jname:
                by_name["access"] = job
            elif "approval" in jname or "preflight" in jname:
                by_name["approval"] = job

        # Check in reverse chronological order (highest stage reached takes precedence)
        for stage_key in ("publish", "build", "review", "access", "approval"):
            job = by_name.get(stage_key)
            if not job:
                continue
            status = job.get("status")
            conclusion = job.get("conclusion")

            if status == "in_progress":
                current_step = ""
                for step in job.get("steps", []):
                    if step.get("status") == "in_progress":
                        current_step = (step.get("name") or "").lower()
                        break
                combo = f"{stage_key} {current_step}"
                if stage_key == "publish":
                    return "Publishing verified package to store"
                if stage_key == "build":
                    if "build and validate" in combo or "compile" in combo:
                        return "Compiling and validating the core"
                    if "frontend" in combo:
                        return "Preparing CATui compatibility tools"
                    if "static" in combo:
                        return "Preparing static dependencies"
                    return "Running Windows build worker"
                if stage_key == "review":
                    return "Reviewing upstream code changes"
                if stage_key == "access":
                    return "Checking store publishing access"
                if stage_key == "approval":
                    return "Verifying approval & signature"

            elif status == "completed" and conclusion == "success":
                if stage_key == "publish":
                    return "Store publication complete"
                if stage_key == "build":
                    return "Build validated; preparing store publication"
                if stage_key == "review":
                    return "Review complete; starting Windows build runner"
                if stage_key == "access":
                    return "Access verified; reviewing upstream code"
                if stage_key == "approval":
                    return "Approval verified; checking store access"

        approval_job = by_name.get("approval")
        if approval_job and approval_job.get("status") == "queued":
            return "Waiting for runner to start preflight"

        return "Dispatching workflow to runner"


    async def create_issue(self, repo, title, body, labels=None):
        url = f"https://api.github.com/repos/{repo}/issues"
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        try:
            data = await self.http.json(url, method="POST", payload=payload)
        except RemoteError as exc:
            if exc.status == 422 and "labels" in payload:
                payload.pop("labels")
                data = await self.http.json(url, method="POST", payload=payload)
            else:
                raise
        return {
            "url": data.get("html_url", ""),
            "number": data.get("number", 0),
            "labels": [l.get("name", "") for l in data.get("labels", []) if isinstance(l, dict)]
        }

    async def catui_release(self, tag=None):
        if tag:
            url = f"https://api.github.com/repos/{STORE_REPO}/releases/tags/{quote(tag, safe='')}"
        else:
            url = f"https://api.github.com/repos/{STORE_REPO}/releases/latest"
        try:
            data = await self.http.json(url)
        except RemoteError as exc:
            if exc.status == 404:
                return None
            raise
        return {
            "tag": str(data.get("tag_name") or ""),
            "name": str(data.get("name") or data.get("tag_name") or ""),
            "body": str(data.get("body") or ""),
            "url": str(data.get("html_url") or ""),
            "published_at": str(data.get("published_at") or "")
        }
