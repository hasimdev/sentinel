"""Chat adapter: posts incidents for people to see and decide on (see CLAUDE.md adapters).

The AI never uses this. Only the orchestrator posts, and only a person's button click
leads to a decision being recorded.
"""

from typing import Protocol

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from orchestrator.adapters.base import AdapterError
from orchestrator.models import ChatRef


class Chat(Protocol):
    def post(self, text: str, blocks: list[dict]) -> ChatRef: ...
    def update(self, ref: ChatRef, text: str, blocks: list[dict]) -> None: ...
    def reply(self, ref: ChatRef, text: str) -> None: ...


class SlackChat:
    """Chat backed by a Slack channel. Needs only the bot scope chat:write(.public)."""

    def __init__(self, bot_token: str, channel: str, client: WebClient | None = None) -> None:
        self.channel = channel
        self._client = client or WebClient(token=bot_token, timeout=10)

    def post(self, text: str, blocks: list[dict]) -> ChatRef:
        resp = self._call("chat_postMessage", channel=self.channel, text=text, blocks=blocks)
        return ChatRef(channel=resp["channel"], ts=resp["ts"])

    def update(self, ref: ChatRef, text: str, blocks: list[dict]) -> None:
        self._call("chat_update", channel=ref.channel, ts=ref.ts, text=text, blocks=blocks)

    def reply(self, ref: ChatRef, text: str) -> None:
        self._call("chat_postMessage", channel=ref.channel, thread_ts=ref.ts, text=text)

    def _call(self, method: str, **kwargs):
        try:
            return getattr(self._client, method)(**kwargs)
        except SlackApiError as exc:
            raise AdapterError(f"Slack {method} failed: {exc.response.get('error')}") from exc
        except Exception as exc:  # network trouble, timeouts
            raise AdapterError(f"Slack {method} failed: {exc}") from exc
