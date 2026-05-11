import logging

from pydantic_ai import UnexpectedModelBehavior

from agent.agent import run_agent
from formatting.composer import ensure_compression_note, post_response
from observability.langfuse import trace_agent_call
from thread_context import conversation_store

STRUCTURED_OUTPUT_VALIDATION_ERROR = UnexpectedModelBehavior


def run_agent_job(text, deps, history, interim_ts, client, logger_inst=None):
    log = logger_inst or logging.getLogger(__name__)
    with trace_agent_call(deps.user_id, text, deps.thread_ts) as trace:
        try:
            result = run_agent(text, deps, message_history=history)
            trace.log_messages(result.all_messages(), usage=result.usage())
            from agent.response_model import AnalyticsResponse
            response: AnalyticsResponse = result.output

            if deps.stale_summary:
                response.notes = (response.notes or []) + [
                    "Some earlier conversation context could not be refreshed — "
                    "this response may not reflect all prior filters or findings. "
                    "If the answer seems off, try restating your question with the relevant context."
                ]
            if deps.compression_planner_failed:
                response.notes = (response.notes or []) + [
                    "Context compression fell back to a coarse strategy for this turn — "
                    "some earlier query results may have been aggregated or dropped. "
                    "If the answer seems off, narrow the question or start a fresh thread."
                ]

            ensure_compression_note(response, result.all_messages())
            from listeners.views.feedback_builder import build_feedback_blocks
            post_response(
                client,
                deps.channel_id,
                deps.thread_ts,
                response,
                feedback_blocks=build_feedback_blocks(),
                replace_ts=interim_ts,
            )
            conversation_store.set_history(deps.channel_id, deps.thread_ts, result.all_messages())
            trace.update(output=response.model_dump())

        except TimeoutError:
            client.chat_update(
                channel=deps.channel_id,
                ts=interim_ts,
                text=":hourglass: That query took too long. Try narrowing the date range or asking for fewer apps.",
            )
            trace.update(level="ERROR", status_message="llm_timeout")
        except STRUCTURED_OUTPUT_VALIDATION_ERROR as e:
            log.warning(f"Structured output validation failed: {e}")
            client.chat_update(
                channel=deps.channel_id,
                ts=interim_ts,
                text=":warning: I couldn't structure a response for that question — try rephrasing.",
            )
            trace.update(level="ERROR", status_message="validation")
        except Exception as e:
            log.exception(f"Agent job failed: {e}")
            client.chat_update(
                channel=deps.channel_id,
                ts=interim_ts,
                text=":warning: Something went wrong; the error was logged. Try again.",
            )
            trace.update(level="ERROR", status_message=str(e)[:200])
