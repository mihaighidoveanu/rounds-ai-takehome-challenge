import json
import threading
import time

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)


def _project_messages(messages: list) -> list:
    """Filter messages for cross-turn persistence:
    - Keep ModelRequest with UserPromptPart
    - Keep ModelResponse with TextPart (including the synthesized final result)
    - Find final_result ToolCallPart, deserialize and store as TextPart in ModelResponse
    - Drop all execute_sql ToolCallPart/ToolReturnPart
    """
    result = []
    # Find final structured result from tool calls named "final_result"
    final_result_json = None
    for msg in messages:
        if not isinstance(msg, ModelResponse):
            continue
        for part in msg.parts:
            if isinstance(part, ToolCallPart) and part.tool_name == "final_result":
                try:
                    args = part.args if isinstance(part.args, dict) else json.loads(part.args)
                    final_result_json = json.dumps(args, indent=2)
                except Exception:
                    pass

    for msg in messages:
        if isinstance(msg, ModelRequest):
            # Keep if has user prompt parts
            user_parts = [p for p in msg.parts if isinstance(p, UserPromptPart)]
            if user_parts:
                result.append(ModelRequest(parts=user_parts))
        elif isinstance(msg, ModelResponse):
            # Keep text parts, drop tool calls
            text_parts = [p for p in msg.parts if isinstance(p, TextPart)]
            if text_parts:
                result.append(ModelResponse(parts=text_parts))

    # Append a synthetic response with the final structured JSON if found
    if final_result_json:
        result.append(ModelResponse(parts=[TextPart(content=final_result_json)]))

    return result


class ConversationStore:
    """Thread-safe in-memory conversation history store.

    Stores Pydantic AI message histories keyed by (channel_id, thread_ts).
    Includes TTL-based cleanup and a maximum conversation limit.
    """

    def __init__(self, ttl_seconds: int = 86400, max_conversations: int = 1000):
        self._store: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        self._ttl_seconds = ttl_seconds
        self._max_conversations = max_conversations
        self._summary_cache: dict = {}

    def get_history(self, channel_id: str, thread_ts: str) -> list[ModelMessage] | None:
        """Retrieve conversation history for a thread.

        Returns None if no history exists or if the history has expired.
        """
        key = (channel_id, thread_ts)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.time() - entry["timestamp"] > self._ttl_seconds:
                del self._store[key]
                return None
            return entry["messages"]

    def set_history(self, channel_id: str, thread_ts: str, messages: list[ModelMessage]) -> None:
        """Store conversation history for a thread, filtering for persistence."""
        filtered = _project_messages(messages)
        key = (channel_id, thread_ts)
        with self._lock:
            self._store[key] = {"messages": filtered, "timestamp": time.time()}
            self._cleanup()

    def get_summary(self, channel_id: str, thread_ts: str, prefix_count: int) -> str | None:
        return self._summary_cache.get((channel_id, thread_ts, prefix_count))

    def set_summary(self, channel_id: str, thread_ts: str, prefix_count: int, summary: str) -> None:
        self._summary_cache[(channel_id, thread_ts, prefix_count)] = summary

    def _cleanup(self) -> None:
        """Remove expired entries and enforce max conversation limit."""
        now = time.time()

        expired = [
            k
            for k, v in self._store.items()
            if now - v["timestamp"] > self._ttl_seconds
        ]
        for k in expired:
            del self._store[k]

        if len(self._store) > self._max_conversations:
            sorted_keys = sorted(
                self._store.keys(), key=lambda k: self._store[k]["timestamp"]
            )
            for k in sorted_keys[: len(self._store) - self._max_conversations]:
                del self._store[k]
