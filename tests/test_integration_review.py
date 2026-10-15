import json
import unittest
from unittest.mock import MagicMock, patch

from tools.review_core import review


class IntegrationReviewTests(unittest.TestCase):
    def context(self):
        return {"target": "a" * 40, "base": "b" * 40, "patchSha256": "c" * 64,
                "apiDiff": "changed declaration", "adapter": []}

    def response(self, content):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [{"message": {
            "content": content, "reasoning_content": "private internal reasoning"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30}}).encode()
        return response

    def test_code_review_has_reasoning_and_only_keeps_safe_result(self):
        content = json.dumps({"decision": "continue", "summary": "Keep callbacks; fixture-api-key"})
        with patch("tools.review_core.urllib.request.urlopen", return_value=self.response(content)) as request:
            result = review(self.context(), "fixture-api-key")
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertNotIn("fixture-api-key", json.dumps(payload))
        self.assertNotIn("fixture-api-key", json.dumps(result))
        self.assertEqual(result["target"], self.context()["target"])
        self.assertEqual(result["usage"]["completion_tokens"], 30)

    def test_unresolved_risk_is_preserved_instead_of_silently_approved(self):
        with patch("tools.review_core.urllib.request.urlopen", return_value=self.response(
                json.dumps({"decision": "needs_review", "summary": "Controller callback signature changed"}))):
            result = review(self.context(), "fixture-key")
        self.assertEqual(result["decision"], "needs_review")

    def test_malformed_review_falls_back_safely_to_needs_review(self):
        with patch("tools.review_core.urllib.request.urlopen", return_value=self.response('')):
            result = review(self.context(), "fixture-key")
        self.assertEqual(result["decision"], "needs_review")

    def test_context_patch_digest_ignores_windows_crlf_checkout(self):
        import hashlib, tempfile, time
        from pathlib import Path
        from tools.review_core import context_for
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "store/cores/pcsx2/source"
            (source / "integration").mkdir(parents=True)
            (source / "upstream.json").write_text(json.dumps({
                "repository": "https://github.com/PCSX2/pcsx2.git",
                "commit": "a" * 40,
                "changedFiles": ["CMakeLists.txt"],
            }))
            lf_bytes = b"diff --git a/CMakeLists.txt b/CMakeLists.txt\n--- a/CMakeLists.txt\n+++ b/CMakeLists.txt\n"
            (source / "integration.patch").write_bytes(lf_bytes.replace(b"\n", b"\r\n"))
            plan = {"id": "a" * 24, "core": "pcsx2", "sha": "b" * 40, "version": "2.8",
                    "store_head": "c" * 40, "owner": "123456789012345678", "expires": int(time.time()) + 3600}
            with patch("tools.review_core.subprocess.run") as run:
                run.return_value.stdout = b""
                ctx = context_for(plan, root, root / "work")
            self.assertEqual(ctx["patchSha256"], hashlib.sha256(lf_bytes).hexdigest())
