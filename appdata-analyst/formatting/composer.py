import json
import logging

from formatting.renderers import render_chart_png, render_component
from agent.response_model import AnalyticsResponse, ProseBlock

logger = logging.getLogger(__name__)

_COMPRESSION_KEYWORDS = ("partial", "reduced", "compressed", "summarized")
_COMPRESSION_NOTE = (
    "Some query results were compressed or truncated to fit context — totals may be approximate."
)


def ensure_compression_note(response: AnalyticsResponse, all_messages) -> None:
    """Append a compression warning to response.notes if any tool return indicates truncation."""
    found_compression = False
    for message in all_messages:
        parts = getattr(message, "parts", None)
        if parts is None:
            continue
        for part in parts:
            # Match pydantic-ai ToolReturnPart
            if type(part).__name__ != "ToolReturnPart":
                continue
            content = getattr(part, "content", None)
            if not isinstance(content, str):
                continue
            try:
                data = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                continue
            if data.get("compressed") or data.get("hard_capped"):
                found_compression = True
                break
        if found_compression:
            break

    if not found_compression:
        return

    notes = response.notes or []
    already_noted = any(
        any(kw in note.lower() for kw in _COMPRESSION_KEYWORDS)
        for note in notes
    )
    if not already_noted:
        notes.append(_COMPRESSION_NOTE)
        response.notes = notes


def post_response(
    client,
    channel: str,
    thread_ts: str,
    response: AnalyticsResponse,
    feedback_blocks: list,
    replace_ts: str | None = None,
) -> None:
    """Build Slack blocks from an AnalyticsResponse and post them to the channel."""
    blocks: list[dict] = []
    pending_charts: list = []  # (png_bytes, title) — uploaded after main blocks

    for comp in response.components:
        if comp.type == "chart":
            try:
                png = render_chart_png(comp)
                pending_charts.append((png, comp.title))
                # Placeholder so the chart appears roughly in order after surrounding blocks
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"_:bar_chart: {comp.title}_"},
                })
            except Exception as e:
                logger.warning(f"Chart render failed: {e}")
                blocks.extend(
                    render_component(
                        ProseBlock(type="prose", text=f":warning: Chart unavailable: {comp.title}")
                    )
                )
        else:
            blocks.extend(render_component(comp))

    if response.notes:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "\n".join(f":warning: _{n}_" for n in response.notes),
                }
            ],
        })

    blocks.extend(feedback_blocks)

    _post_with_continuation(client, channel, thread_ts, blocks, replace_ts=replace_ts)

    # Upload charts as thread file replies — Slack renders PNG files inline
    for png, title in pending_charts:
        try:
            client.files_upload_v2(
                content=png,
                filename=f"{title[:50]}.png",
                title=title[:150],
                channel=channel,
                thread_ts=thread_ts,
            )
        except Exception as e:
            logger.warning(f"Chart upload failed for '{title}': {e}")


def _post_with_continuation(
    client,
    channel: str,
    thread_ts: str,
    blocks: list,
    replace_ts: str | None = None,
) -> None:
    """Post blocks to Slack, splitting into chunks of 50 to respect the block limit."""
    chunk_size = 50
    chunks = [blocks[i : i + chunk_size] for i in range(0, len(blocks), chunk_size)]

    if not chunks:
        chunks = [[]]

    first_chunk = chunks[0]
    if replace_ts:
        client.chat_update(
            channel=channel,
            ts=replace_ts,
            blocks=first_chunk,
            text="Response",
        )
    else:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=first_chunk,
            text="Response",
        )

    for chunk in chunks[1:]:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=chunk,
            text="_continued_",
        )
