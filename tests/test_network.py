import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from catui_bot.http import HTTP, RemoteError
from catui_bot.llm import Assistant, AssistantError


class Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def content(self):
        return self

    async def iter_chunked(self, size):
        yield b'{}'


class Session:
    def __init__(self):
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return Response()


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_github_token_never_sent_to_eden(self):
        session = Session()
        api = HTTP(session, "unit-test-credential")
        await api.json("https://git.eden-emu.dev/api/v1/repos/eden-emu/eden")
        self.assertNotIn("Authorization", session.calls[-1][1]["headers"])
        await api.json("https://api.github.com/repos/PCSX2/pcsx2")
        self.assertEqual(session.calls[-1][1]["headers"]["Authorization"], "Bearer unit-test-credential")
        self.assertFalse(session.calls[-1][1]["allow_redirects"])

    async def test_private_and_unknown_hosts_rejected(self):
        session = Session()
        for url in ("http://api.github.com", "https://localhost", "https://169.254.169.254", "https://evil.invalid"):
            with self.assertRaises(RemoteError):
                await HTTP(session, "credential").json(url)
        self.assertEqual(session.calls, [])

    async def test_deepseek_only_gets_its_own_key(self):
        session = Session()
        await HTTP(session, "github-unit-credential").json("https://api.deepseek.com/chat/completions",
            method="POST", headers={"Authorization": "Bearer model-unit-credential"})
        self.assertEqual(session.calls[0][1]["headers"]["Authorization"], "Bearer model-unit-credential")

    async def test_llm_redacts_configured_secrets_and_never_exposes_them_in_reply(self):
        api = SimpleNamespace(json=AsyncMock(return_value={"choices": [{"message": {"content": "unit-discord-secret @everyone"}}]}))
        config = SimpleNamespace(llm_key="unit-model-secret", github_token="unit-github-secret", signing_key="unit-signing-secret",
                                 discord_token="unit-discord-secret", llm_model="deepseek-flash")
        answer = await Assistant(api, config).answer("unit-discord-secret", {"value": "unit-signing-secret"})
        text = json.dumps(api.json.call_args.kwargs["payload"])
        self.assertNotIn("unit-discord-secret", text)
        self.assertNotIn("unit-signing-secret", text)
        self.assertNotIn("unit-discord-secret", answer)
        self.assertNotIn("@", answer)

    async def test_release_summary_uses_flash_and_redacts_secrets(self):
        api = SimpleNamespace(json=AsyncMock(return_value={"choices": [{"message": {"content": "Fixes crashes. unit-discord-secret"}}]}))
        config = SimpleNamespace(llm_key="unit-model-secret", github_token="unit-github-secret", signing_key="unit-signing-secret",
                                 discord_token="unit-discord-secret", llm_model="deepseek-flash")
        release = {"tag": "v1", "title": "Hotfix", "prerelease": False, "body": "unit-github-secret"}
        summary = await Assistant(api, config).summarize_release("PCSX2", release)
        payload = api.json.call_args.kwargs["payload"]
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("unit-github-secret", json.dumps(payload))
        self.assertNotIn("unit-discord-secret", summary)
        api.json.reset_mock()
        await Assistant(api, config).summarize_release("PCSX2", release)
        api.json.assert_awaited_once()

    async def test_empty_reasoning_only_output_and_api_errors_are_reported_safely(self):
        api = SimpleNamespace(json=AsyncMock(return_value={"choices": [{"finish_reason": "length", "message": {"content": None, "reasoning_content": "private reasoning"}}]}))
        config = SimpleNamespace(llm_key="fixture-key", github_token="", signing_key="", discord_token="", llm_model="deepseek-flash")
        assistant = Assistant(api, config)
        with self.assertLogs("catui", level="WARNING") as logs:
            with self.assertRaises(AssistantError) as error:
                await assistant.request("English only", "Summarize the notes", 220)
        self.assertEqual(error.exception.reason, "empty_response")
        self.assertNotIn("private reasoning", " ".join(logs.output))
        for status, reason in ((401, "authentication"), (402, "balance"), (429, "rate_limit"), (503, "provider_error")):
            api.json.side_effect = RemoteError("fixture-key must never be logged", status)
            with self.assertLogs("catui", level="WARNING") as logs:
                with self.assertRaises(AssistantError) as error:
                    await assistant.request("English only", "Release", 220)
            self.assertEqual(error.exception.reason, reason)
            self.assertNotIn("fixture-key", " ".join(logs.output))
