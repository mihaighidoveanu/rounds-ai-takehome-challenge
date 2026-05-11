import re

from slack_bolt import App

from .analytics_followup import handle_analytics_followup
from .feedback_buttons import handle_feedback_button


def register(app: App):
    app.action("feedback")(handle_feedback_button)
    app.action(re.compile(r"^analytics_followup"))(handle_analytics_followup)
