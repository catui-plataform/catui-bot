import asyncio
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from .security import sign, validate_plan
from .http import RemoteError
from .config import CORE_IDS, CORE_NAMES
from .llm import AssistantError, strip_emojis


@dataclass
class ScanReport:
    results: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    busy: bool = False


class Service:
    def __init__(self, state, upstream, assistant, config):
        self.state, self.upstream, self.assistant, self.config = state, upstream, assistant, config
        self.scan_lock = asyncio.Lock()
        self.approval_lock = asyncio.Lock()

    async def release_context(self, core, latest):
        source = getattr(self.upstream, "registry", {}).get(core, {}).get("repository", "").removesuffix(".git")
        source_link = {"url": source + "/releases"} if source else {}
        try:
            release = latest.get("release") or await self.upstream.latest_release(core)
        except Exception:
            return {"status": "unavailable", **source_link}
        if not release:
            return {"status": "missing", **source_link}
        revision = hashlib.sha256(json.dumps(["release-v2-nonthinking", release, getattr(self.config, "llm_model", "deepseek-flash")], sort_keys=True).encode()).hexdigest()
        cached = self.state.release_summary(core, revision)
        if cached and cached.get("summary"):
            return cached
        summary = ""
        summary_status = "not_configured" if not self.config.llm_key else "provider_error"
        if release["body"].strip() and self.config.llm_key and self.assistant:
            try:
                summary = await self.assistant.summarize_release(CORE_NAMES[core], release)
            except AssistantError as exc:
                summary_status = exc.reason
            except Exception:
                pass  # Release summaries must never block core checks.
        data = {k: v for k, v in release.items() if k != "body"}
        data.update(status="available", summary=summary,
                    summary_status="ready" if summary else summary_status,
                    has_description=bool(release["body"].strip()))
        self.state.cache_release(core, revision, data)
        return data

    async def scan(self, cores=None, *, manual=False):
        if self.scan_lock.locked():
            return ScanReport(busy=True)
        async with self.scan_lock:
            self.state.expire()
            report = ScanReport()
            if cores is not None:
                target_cores = [c for c in (cores if isinstance(cores, (list, tuple, set)) else [cores]) if c in CORE_IDS]
            elif manual:
                watched = [core for core, enabled in self.state.watches().items() if enabled]
                target_cores = watched if watched else list(CORE_IDS)
            else:
                target_cores = [core for core, enabled in self.state.watches().items() if enabled]
            if not target_cores:
                return report
            try:
                head = await self.upstream.store_head()
            except Exception as exc:
                report.errors.append(f"Store: {type(exc).__name__}")
                report.results = [{"core": core, "status": "error", "notes": "Could not read the store. Please try again."} for core in target_cores]
                return report
            for core in target_cores:
                item = {"core": core}
                try:
                    try:
                        manifest = await self.upstream.content(f"store/cores/{core}/manifest.json", head)
                        source = await self.upstream.content(f"store/cores/{core}/source/upstream.json", head)
                    except RemoteError as exc:
                        if exc.status != 404:
                            raise
                        item.update(status="source_missing")
                        continue
                    snapshot = source.get("snapshot", {})
                    if (snapshot.get("archive") != "source.tar.gz"
                            or not re.fullmatch(r"[a-f0-9]{64}", snapshot.get("sha256", ""))
                            or not re.fullmatch(r"[a-f0-9]{40}", source.get("commit", ""))):
                        item.update(status="source_missing")
                        continue
                    entry = getattr(self.upstream, "registry", {}).get(core, {})
                    if entry and source.get("repository") != entry["repository"]:
                        item.update(status="source_missing")
                        continue
                    latest = await self.upstream.latest(core)
                    item["version"] = latest["version"]
                    # The branch can move independently of optional release metadata.
                    for job in self.state.jobs("pending"):
                        if job["core"] == core and (job["sha"] != latest["sha"] or
                                json.loads(job["payload"])["version"] != latest["version"]):
                            self.state.transition(job["id"], "pending", "stale")
                    item["release"] = await self.release_context(core, latest)
                    base = manifest.get("upstreamCommit", source["commit"])
                    item["store_version"] = base[:12]
                    if latest["sha"] == base:
                        item.update(status="up_to_date", notes="The store already has this upstream revision.", commits_ahead=0)
                        continue
                    previous = self.state.latest_for(core, latest["sha"])
                    if previous and manual and (
                        previous["status"] in {"failed", "rejected", "cancelled"}
                        or (previous["status"] == "pending" and json.loads(previous["payload"])["store_head"] != head)
                        or previous["status"] == "pending" and any(bad in previous["notes"].lower() for bad in ["resumo (", "não há evidência", "para ação", "somente mudanças", "mudanças upstream"])
                    ):
                        self.state.transition(previous["id"], previous["status"], "stale")
                        previous = None
                    if previous:
                        p_notes = previous["notes"]
                        item.update(status=previous["status"], notes=p_notes,
                                    job_id=previous["id"])
                        m = re.search(r"\+(\d+)\s+commits\s+ahead", p_notes)
                        if m:
                            item["commits_ahead"] = int(m.group(1))
                        continue
                    comparison = await self.upstream.compare(core, base, latest["sha"])
                    if comparison["status"] == "identical":
                        item.update(status="up_to_date", notes="No newer upstream revision is available for this baseline.", commits_ahead=0)
                        continue
                    if comparison["status"] == "behind":
                        count = comparison.get("behind_by")
                        detail = f" is {count} commits newer than" if count else " already includes"
                        item.update(status="baseline_ahead", source_commits_ahead=count,
                                    notes=f"The store's recorded source base{detail} upstream commit {latest['version']}.")
                        continue
                    if comparison["status"] == "diverged":
                        item.update(status="review", notes=(f"The store core and upstream commit {latest['version']} have different source histories. "
                                    "The upstream changes cannot be applied as a simple forward update. "
                                    "Updates are paused for this core until its source changes are reviewed."))
                        continue
                    commits_ahead = comparison.get("total_commits") or len(comparison.get("commits", []))
                    item["commits_ahead"] = commits_ahead
                    if entry.get("build_enabled") is False:
                        item.update(status="monitor_only")
                        continue
                    raw_commits = [c.get("subject", "").strip() for c in comparison.get("commits", []) if c.get("subject")]
                    clean_commits = [re.sub(r"^\[.*?\]\s*", "", c).strip() for c in raw_commits]
                    clean_commits = [c for c in clean_commits if c and not c.lower().startswith("merge ")]

                    # The release description is summarized once in release_context and cached.
                    # Commit highlights need no second model request for the same release.
                    fallback_lines = [f"* {c}" for c in (clean_commits or raw_commits)[:5]]
                    body_notes = "\n".join(fallback_lines) or "New upstream commits are ready for review."

                    notes = f"+{commits_ahead} commits ahead\n{body_notes}" if commits_ahead else body_notes
                    payload = {"id": secrets.token_hex(12), "core": core, "sha": latest["sha"],
                               "version": latest["version"], "store_head": head,
                               "owner": str(self.config.owner_id), "expires": int(time.time()) + 24 * 3600}
                    validate_plan(payload)
                    self.state.insert(payload, notes)
                    item.update(status="pending", notes=notes, job_id=payload["id"])
                except Exception as exc:
                    report.errors.append(f"{core}: {type(exc).__name__}")
                    item.update(status="error", notes="Could not check upstream. Please try again.")
                finally:
                    report.results.append(item)
            return report

    async def approve(self, job_id):
        async with self.approval_lock:
            return await self._approve(job_id)

    async def _approve(self, job_id):
        if not self.config.updates_enabled:
            return "Updates are disabled during setup. Configure the runners and validators before enabling CORE_UPDATES_ENABLED."
        job = self.state.get(job_id)
        if not job or job["status"] != "pending":
            return "This proposal was already handled or does not exist."
        payload = json.loads(job["payload"])
        try:
            validate_plan(payload)
        except ValueError:
            self.state.transition(job_id, "pending", "expired")
            return "This proposal expired. Use /cores-check."
        if getattr(self.upstream, "registry", {}).get(payload["core"], {}).get("build_enabled") is False:
            return "Automatic builds are not configured for this core yet."
        if self.state.jobs("running", "dispatching", "uncertain"):
            return "Another update is in progress. Wait for it to finish before checking against the updated store."
        try:
            latest = await self.upstream.latest(payload["core"])
        except Exception:
            return "Could not verify the latest upstream commit. Nothing was started; please try again."
        if latest["sha"] != payload["sha"] or latest["version"] != payload["version"]:
            self.state.transition(job_id, "pending", "stale")
            return "Upstream changed since this proposal. Use /cores-check to compare again."
        try:
            current_head = await self.upstream.store_head()
        except Exception:
            return "Could not verify the store revision. Nothing was started; please try again."
        if current_head != payload["store_head"]:
            self.state.transition(job_id, "pending", "stale")
            return "The store changed since this proposal. Use /cores-check to compare again."
        if not self.state.transition(job_id, "pending", "dispatching"):
            return "This proposal was already handled."
        try:
            await self.upstream.dispatch(payload, sign(payload, self.config.signing_key))
        except Exception as exc:
            if isinstance(exc, RemoteError) and exc.status is not None and 400 <= exc.status < 500:
                self.state.transition(job_id, "dispatching", "failed")
                return "GitHub rejected the request. Check permissions and configuration, then use /cores-check for a new proposal."
            self.state.transition(job_id, "dispatching", "uncertain")
            return "Could not confirm dispatch. I will check GitHub before taking further action."
        self.state.transition(job_id, "dispatching", "running")
        return f"Approved: {payload['core']} -> {payload['version']}. Build dispatched; tracking progress on this card."

    async def resume_after_update(self):
        """Resume an existing approval when a new runner revision fixes a failure."""
        async with self.approval_lock:
            if not self.config.updates_enabled or self.state.jobs("running", "uncertain", "dispatching"):
                return
            failed = self.state.jobs("failed")
            if not failed:
                return
            runs = await self.upstream.runs()
            head = await self.upstream.bot_head()
            for job in reversed(failed):
                plan = json.loads(job["payload"])
                try:
                    validate_plan(plan)
                except ValueError:
                    continue
                latest = self.state.latest_for(job["core"], job["sha"])
                if plan["owner"] != str(self.config.owner_id) or not latest or latest["id"] != job["id"]:
                    continue
                title = f"catui/{job['core']}/{job['id']}"
                run = next((r for r in runs if r.get("display_title") == title), None)
                if run and (run.get("status") != "completed" or
                            (run.get("conclusion") in {"success", "cancelled"} and str(run["id"]) != str(job.get("run_id")))):
                    self.state.transition(job["id"], "failed", "running")
                    self.state.update(job["id"], run_id=str(run["id"]), run_url=run["html_url"],
                                      result="", notified=0, reported_stage="", stage="Continuing the approved update")
                    return
                if (not run or run.get("conclusion") != "failure" or not run.get("head_sha")
                        or run["head_sha"] == head):
                    continue
                if await self.upstream.store_head() != plan["store_head"]:
                    continue  # A changed source baseline needs a new owner approval.
                if not self.state.transition(job["id"], "failed", "dispatching"):
                    continue
                self.state.update(job["id"], run_id=str(run["id"]), result="", notified=0, reported_stage="",
                                  stage="Resuming approved update with the corrected runner")
                try:
                    await self.upstream.dispatch(plan, sign(plan, self.config.signing_key))
                except Exception as exc:
                    # Delivery can be uncertain; reconcile before sending again.
                    rejected = isinstance(exc, RemoteError) and exc.status is not None and 400 <= exc.status < 500
                    self.state.transition(job["id"], "dispatching", "failed" if rejected else "uncertain")
                    if rejected:
                        self.state.update(job["id"], result=f"GitHub rejected the recovery request: HTTP {exc.status}.")
                    raise
                self.state.transition(job["id"], "dispatching", "running")
                return

    async def cancel(self, job_id):
        async with self.approval_lock:
            job = self.state.get(job_id)
            if not job or job["status"] not in {"running", "uncertain", "dispatching"}:
                return "This update is no longer running."
            if job.get("stage") == "Cancellation requested on GitHub":
                return "Cancellation is already requested. Waiting for GitHub to stop the workflow."
            run_id = job.get("run_id")
            if not run_id:
                title = f"catui/{job['core']}/{job['id']}"
                run = next((r for r in await self.upstream.runs() if r.get("display_title") == title), None)
                if not run:
                    return "GitHub has not registered the run yet. Try cancelling again shortly."
                run_id = str(run["id"])
                self.state.update(job_id, run_id=run_id, run_url=run["html_url"])
            try:
                await self.upstream.cancel(run_id)
            except RemoteError as exc:
                if exc.status != 409:
                    raise
                await self.reconcile()
                return "GitHub already finished this workflow. Its final status is being checked."
            self.state.update(job_id, stage="Cancellation requested on GitHub", reported_stage="",
                              result="Cancellation requested; waiting for GitHub to stop the workflow.")
            return "Cancellation requested on GitHub. Waiting for the workflow to stop."

    async def reconcile(self):
        active = self.state.jobs("running", "uncertain", "dispatching")
        if not active:
            return
        runs = await self.upstream.runs()
        for job in active:
            expected = f"catui/{job['core']}/{job['id']}"
            matching = [r for r in runs if r.get("display_title") == expected]
            run = matching[0] if matching else None
            if not run:
                if time.time() - job["created"] > 26 * 3600:
                    self.state.transition(job["id"], job["status"], "failed")
                continue
            if (job.get("stage", "").startswith("Resuming approved update")
                    and str(run["id"]) == str(job.get("run_id")) and run["status"] == "completed"):
                continue  # The new dispatch is not visible yet; this is the previous run.
            self.state.update(job["id"], run_id=str(run["id"]), run_url=run["html_url"])
            if run["status"] != "completed":
                self.state.transition(job["id"], job["status"], "running")
                if hasattr(self.upstream, "stage") and job.get("stage") != "Cancellation requested on GitHub":
                    try:
                        self.state.update(job["id"], stage=await self.upstream.stage(run["id"]))
                    except Exception:
                        pass  # A progress lookup must not disrupt a running build.
                continue
            # Success means the publish job, including hash checks, completed.
            following = {"success": "published", "cancelled": "cancelled"}.get(run["conclusion"], "failed")
            result = ""
            if following == "cancelled":
                self.state.update(job["id"], result="GitHub confirmed workflow cancellation.")
                self.state.transition(job["id"], job["status"], following)
                continue
            if following == "published":
                try:
                    receipt = await self.upstream.content(f"store/cores/{job['core']}/source/build-receipt.json")
                    if receipt.get("plan", {}).get("id") == job["id"]:
                        scope = "Native + CATui compatibility (gameplay not tested)" if receipt.get("runtime", {}).get("gameplayTested") is False else "Native + CATui"
                        result = f"SHA-256: {receipt['sha256']}\nChecks: {scope}\nIntegration repair files: " + ", ".join(receipt.get("aiRepairFiles", []))
                        review = receipt.get("integrationReview") or {}
                        if review.get("summary"):
                            result += "\nIntegration: " + review["summary"][:650]
                except Exception:
                    result = "Workflow confirmed publication. See the build report."
                self.state.update(job["id"], result=result)
                self.state.transition(job["id"], job["status"], following)
            else:
                # Repair/rebuild happens in Actions with the current workspace and logs.
                # Redispatching the same plan would discard those repairs and repeat the failure.
                try:
                    diagnostics = await self.upstream.diagnostics(run["id"])
                    stopped = [d for d in diagnostics if d.get("result") in {"failure", "cancelled", "timed_out"}]
                    details = [d["job"] + (": " + ", ".join(d.get("failed_steps", [])) if d.get("failed_steps") else "") for d in stopped]
                    result = "Automatic recovery stopped at " + ("; ".join(details) or "GitHub Actions") + ". See the diagnostic artifact in the build report."
                except Exception:
                    result = "Automatic recovery stopped. See the diagnostic artifact in the build report."
                self.state.update(job["id"], result=result)
                self.state.transition(job["id"], job["status"], following)


    async def create_github_issue(self, thread_title, starter, extra_messages=None, reporter=None, attachments=None, description=None, repo=None, thread_id=None, new=False, force_new=False, existing_issue=None, solved=False, reopen=False, thread_tags=None):
        target_repo = (repo or getattr(self.config, "issues_repo", "arthiee4/catui-main")).strip()
        is_new = bool(new or force_new)
        existing = existing_issue or (self.state.get_thread_issue(thread_id) if (thread_id and not is_new) else None)
        is_update = bool(
            not is_new
            and existing
            and existing.get("issue_number")
            and str(existing.get("repo", "")).lower().strip("/") == target_repo.lower().strip("/")
        )

        formatted = await self.assistant.format_issue(
            thread_title=thread_title,
            starter=starter,
            extra_messages=extra_messages,
            reporter=reporter,
            attachments=attachments,
            description=description,
            is_update=is_update,
            solved=solved,
            reopen=reopen,
            thread_tags=thread_tags
        )

        if is_update:
            issue_number = existing["issue_number"]
            issue_url = existing["issue_url"]
            state = "closed" if solved else ("open" if reopen else None)
            state_reason = "completed" if solved else ("reopened" if reopen else None)
            try:
                result = await self.upstream.update_issue(
                    repo=target_repo,
                    issue_number=issue_number,
                    title=formatted["title"],
                    body=formatted["body"],
                    labels=formatted.get("labels"),
                    state=state,
                    state_reason=state_reason
                )
            except RemoteError as exc:
                if exc.status in {404, 410}:
                    if thread_id:
                        self.state.delete_thread_issue(thread_id)
                    is_update = False
                else:
                    raise

            if is_update:
                comment_body = formatted.get("comment")
                if not comment_body:
                    comment_parts = ["### Update from Discord Thread", "Issue updated with the latest messages/notes from Discord."]
                    if description and description.strip():
                        comment_parts.append(f"\n**Maintainer Note:** {description.strip()}")
                    if extra_messages:
                        comment_parts.append(f"\n*Incorporated {len(extra_messages)} additional message(s) / screenshot(s).*")
                    comment_body = "\n".join(comment_parts)
                comment_body = strip_emojis(comment_body).strip()

                try:
                    await self.upstream.add_issue_comment(target_repo, issue_number, comment_body)
                except Exception:
                    pass

                if thread_id:
                    self.state.save_thread_issue(thread_id, target_repo, issue_number, issue_url)

                return {
                    "repo": target_repo,
                    "title": formatted["title"],
                    "body": formatted["body"],
                    "labels": result.get("labels") or formatted.get("labels", []),
                    "url": issue_url,
                    "number": issue_number,
                    "action": "solved" if solved else ("reopened" if reopen else "updated"),
                    "comment": comment_body
                }

        result = await self.upstream.create_issue(
            repo=target_repo,
            title=formatted["title"],
            body=formatted["body"],
            labels=formatted.get("labels")
        )
        issue_number = result.get("number")
        if solved and issue_number:
            try:
                await self.upstream.update_issue(
                    repo=target_repo,
                    issue_number=issue_number,
                    state="closed",
                    state_reason="completed"
                )
            except Exception:
                pass

        if thread_id and issue_number:
            self.state.save_thread_issue(thread_id, target_repo, result["number"], result.get("url", ""))

        return {
            "repo": target_repo,
            "title": formatted["title"],
            "body": formatted["body"],
            "labels": result.get("labels") or formatted.get("labels", []),
            "url": result.get("url", ""),
            "number": result.get("number", 0),
            "action": "solved" if solved else ("reopened" if reopen else "created")
        }

    async def delete_github_issue(self, thread_id=None, repo=None, issue_number=None):
        target_repo = (repo or getattr(self.config, "issues_repo", "arthiee4/catui-main")).strip()
        num = issue_number
        if not num and thread_id:
            existing = self.state.get_thread_issue(thread_id)
            if existing:
                num = existing.get("issue_number")
                if existing.get("repo"):
                    target_repo = existing["repo"]

        details = None
        if num:
            details = await self.upstream.delete_issue(target_repo, num)

        if thread_id:
            self.state.delete_thread_issue(thread_id)

        return {
            "deleted": True,
            "repo": target_repo,
            "number": num,
            "details": details
        }

    async def get_release_announcement(self, tag=None):
        release = await self.upstream.catui_release(tag=tag)
        if not release:
            tag_label = f" for tag '{tag}'" if tag else ""
            raise ValueError(f"No release found{tag_label}.")
        announcement = await self.assistant.format_release(
            tag_name=release["tag"],
            release_name=release["name"],
            release_body=release["body"]
        )
        return {
            "announcement": announcement,
            "tag": release["tag"],
            "name": release["name"],
            "url": release["url"]
        }
