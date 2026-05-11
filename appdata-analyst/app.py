import atexit
import logging
import os

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

load_dotenv(dotenv_path=".env", override=False)

from agent import get_model
from agent.executor import shutdown_agent_executor
from db.connection import close_pool, create_pool
from listeners import register_listeners

get_model()  # Fail fast if no AI provider key is configured

logging.basicConfig(level=logging.DEBUG)

create_pool(os.environ.get("DATABASE_URL", "postgresql://analyst_ro:analyst_ro@localhost:5432/analytics"))
atexit.register(close_pool)
atexit.register(shutdown_agent_executor)

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    client=WebClient(
        base_url=os.environ.get("SLACK_API_URL", "https://slack.com/api"),
        token=os.environ.get("SLACK_BOT_TOKEN"),
    ),
)

register_listeners(app)

if __name__ == "__main__":
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
