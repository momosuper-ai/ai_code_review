from src.config import settings


def verify_gitlab_token(token: str | None) -> bool:
    """Verify the X-Gitlab-Token header matches the configured secret."""
    if not settings.GITLAB_WEBHOOK_SECRET:
        return True
    return token == settings.GITLAB_WEBHOOK_SECRET
