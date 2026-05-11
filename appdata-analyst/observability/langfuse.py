import contextlib
import logging
import os
import time

logger = logging.getLogger(__name__)

_langfuse = None


def _get_client():
    global _langfuse
    if _langfuse is not None:
        return _langfuse
    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
            host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        )
    except Exception as e:
        logger.warning(f"Langfuse init failed, tracing disabled: {e}")
    return _langfuse


class _Trace:
    def __init__(self, trace, client, start_time):
        self._trace = trace
        self._client = client
        self._start = start_time

    def log_messages(self, messages, usage=None):
        if self._trace is None:
            return
        try:
            from pydantic_ai.messages import ModelResponse, ModelRequest, ToolCallPart, ToolReturnPart

            tool_calls = {}
            tool_returns = {}
            for msg in messages:
                if isinstance(msg, ModelResponse):
                    for part in msg.parts:
                        if isinstance(part, ToolCallPart):
                            tool_calls[part.tool_call_id] = part
                elif isinstance(msg, ModelRequest):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_returns[part.tool_call_id] = part

            for call_id, call in tool_calls.items():
                ret = tool_returns.get(call_id)
                span = self._trace.span(
                    name=call.tool_name,
                    input=call.args,
                    output=ret.content if ret else None,
                )
                span.end()

            if usage is not None:
                self._trace.update(metadata={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "total_requests": usage.requests,
                    "total_tool_calls": usage.tool_calls,
                })

            if self._client:
                self._client.flush()
        except Exception as e:
            logger.warning(f"Langfuse message logging failed: {e}")

    def update(self, output=None, level="DEFAULT", status_message=None):
        if self._trace is None:
            return
        try:
            latency_ms = int((time.time() - self._start) * 1000)
            kwargs = {"metadata": {"latency_ms": latency_ms}}
            if output is not None:
                kwargs["output"] = output
            if level != "DEFAULT":
                kwargs["level"] = level
            if status_message is not None:
                kwargs["status_message"] = status_message
            self._trace.update(**kwargs)
            if self._client:
                self._client.flush()
        except Exception as e:
            logger.warning(f"Langfuse trace update failed: {e}")


@contextlib.contextmanager
def trace_agent_call(user_id: str, input_text: str, thread_ts: str):
    client = _get_client()
    start = time.time()
    trace = None
    try:
        if client:
            trace = client.trace(
                name="agent_call",
                user_id=user_id,
                session_id=thread_ts,
                input=input_text,
                metadata={"thread_ts": thread_ts},
            )
    except Exception as e:
        logger.warning(f"Langfuse trace creation failed: {e}")
    yield _Trace(trace, client, start)
