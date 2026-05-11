from logging import Logger

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from agent.deps import AgentDeps
from agent.executor import AgentQueueFull, submit_agent_job
from agent.job import run_agent_job
from db.connection import get_pool
from thread_context import conversation_store

ACTION_ID = "analytics_followup"


def handle_analytics_followup(
    ack: Ack,
    body: dict,
    client: WebClient,
    context: BoltContext,
    logger: Logger,
):
    ack()
    value = body["actions"][0]["value"]
    channel_id = body["channel"]["id"]
    thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
    user_id = body["user"]["id"]

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
        message_ts=interim_ts,
        user_token=context.get("user_token"),
        db_pool=get_pool(),
    )

    try:
        submit_agent_job(run_agent_job, value, deps, history, interim_ts, client, logger)
    except AgentQueueFull:
        client.chat_update(
            channel=channel_id,
            ts=interim_ts,
            text=":hourglass: I'm at capacity — try again in a moment.",
        )
