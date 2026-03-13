import hmac
import hashlib

from src.config import settings


def verify_gitlab_webhook(token: str) -> bool:
    """验证 GitLab Webhook Secret Token。"""
    return hmac.compare_digest(token, settings.gitlab_webhook_secret)


def verify_sonarqube_webhook(payload: bytes, signature: str) -> bool:
    """验证 SonarQube Webhook 签名（如果配置了 secret）。"""
    if not settings.sonarqube_token:
        return True
    expected = hmac.new(settings.sonarqube_token.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
