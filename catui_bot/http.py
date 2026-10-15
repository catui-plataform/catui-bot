import json
from urllib.parse import urlsplit
import aiohttp


class RemoteError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class HTTP:
    def __init__(self, session, github_token=""):
        self.session, self.github_token = session, github_token

    async def json(self, url, *, method="GET", payload=None, headers=None):
        host = urlsplit(url).hostname
        if urlsplit(url).scheme != "https" or host not in {"api.github.com", "git.eden-emu.dev", "api.deepseek.com"}:
            raise RemoteError("Unapproved API host")
        request_headers = {"User-Agent": "catui-bot", "Accept": "application/json"}
        if host == "api.github.com":
            request_headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
            if self.github_token:
                request_headers["Authorization"] = "Bearer " + self.github_token
        request_headers.update(headers or {})
        async with self.session.request(method, url, json=payload, headers=request_headers, allow_redirects=False,
                                        timeout=aiohttp.ClientTimeout(total=45)) as response:
            if not 200 <= response.status < 300:
                raise RemoteError(f"{host}: HTTP {response.status}", response.status)
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise RemoteError("API response exceeds limit")
            return json.loads(body) if body else {}
