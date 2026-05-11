def app_store_url(app_id: str, platform: str) -> str:
    if platform == "iOS":
        return f"https://apps.apple.com/app/id{app_id}"
    return f"https://play.google.com/store/apps/details?id={app_id}"


def slack_app_link(name: str, app_id: str, platform: str) -> str:
    return f"<{app_store_url(app_id, platform)}|{name}>"
