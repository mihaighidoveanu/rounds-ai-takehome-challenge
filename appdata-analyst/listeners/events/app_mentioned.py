import re
from logging import Logger

from slack_bolt import BoltContext
from slack_sdk import WebClient

from agent.deps import AgentDeps
from agent.executor import AgentQueueFull, submit_agent_job
from agent.job import run_agent_job
from db.connection import get_pool
from thread_context import conversation_store


def handle_app_mentioned(
    client: WebClient,
    context: BoltContext,
    event: dict,
    logger: Logger,
):
    channel_id = context.channel_id
    text = event.get("text", "")
    thread_ts = event.get("thread_ts") or event["ts"]
    user_id = context.user_id

    cleaned_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    if not cleaned_text:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Hey! Ask me anything about your app analytics.",
        )
        return

    interim = client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=":mag: Looking that up...",
    )
    interim_ts = interim["ts"]

    history = conversation_store.get_history(channel_id, thread_ts)
    deps = AgentDeps(
        client=client,
        user_id=user_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        message_ts=event["ts"],
        user_token=context.user_token,
        db_pool=get_pool(),
    )

    try:
        submit_agent_job(run_agent_job, cleaned_text, deps, history, interim_ts, client, logger)
    except AgentQueueFull:
        client.chat_update(
            channel=channel_id,
            ts=interim_ts,
            text=":hourglass: I'm at capacity — try again in a moment.",
        )
