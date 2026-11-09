# SPDX-License-Identifier: MPL-2.0
"""Anthropic-format Messages client using only the standard library.

DeepSeek exposes an Anthropic-compatible endpoint at
``https://api.deepseek.com/anthropic``, which is the same endpoint Claude Code
uses for this workspace. The API key is read from the environment and is
never written to disk, logs, or request traces.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-flash"
ANTHROPIC_VERSION = "2023-06-01"


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def model_from_environment() -> str:
    configured = os.environ.get("DSH_MODEL") or os.environ.get("ANTHROPIC_MODEL")
    if not configured:
        return DEFAULT_MODEL
    # Claude Code's model names may carry a context-window suffix
    # (e.g. "deepseek-flash[1m]"); the API wants the bare name.
    return configured.split("[", 1)[0]


def key_from_environment() -> str:
    for name in ("DSH_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    raise ApiError(
        "no API key: set DSH_API_KEY, DEEPSEEK_API_KEY or ANTHROPIC_API_KEY"
    )


class MessagesClient:
    """One endpoint, one key, one model; `create` is a Messages call."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 180.0,
        retries: int = 2,
    ) -> None:
        self._api_key = api_key
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or model_from_environment()
        self.timeout = timeout
        self.retries = retries

    def create(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
    ) -> dict:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
        }
        payload = json.dumps(body).encode("utf-8")
        url = f"{self.base_url}/v1/messages"
        headers = {
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, data=payload, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")[:500]
                last_error = ApiError(
                    f"HTTP {error.code} from the messages endpoint: {detail}",
                    status=error.code,
                )
                if error.code not in (429, 500, 502, 503, 529):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = ApiError(f"transport error: {error}")
            if attempt < self.retries:
                time.sleep(1.0 + attempt)
        assert last_error is not None
        raise last_error
