from dataclasses import dataclass, field

from slack_sdk import WebClient


@dataclass
class AgentDeps:
    client: WebClient
    user_id: str
    channel_id: str
    thread_ts: str
    message_ts: str
    user_token: str | None = None
    db_pool: object | None = None  # psycopg ConnectionPool
    aggregate_cache: dict = field(default_factory=dict)
    original_result_cache: dict = field(default_factory=dict)
    compression_planner_failed: bool = False
    stale_summary: bool = False
