"""QQ / Gmail delivery routing and fallback; no network or real credentials."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.auth import mail


def _settings(**overrides):
    base = dict(
        QQ_SMTP_HOST="smtp.qq.com", QQ_SMTP_PORT=465, QQ_SMTP_SSL=True, QQ_SMTP_TLS=False,
        QQ_SMTP_USER="synthetic-sender@qq.com", QQ_SMTP_PASSWORD="synthetic-qq-code",
        QQ_FROM_EMAIL="synthetic-sender@qq.com",
        GMAIL_SMTP_HOST="smtp.gmail.com", GMAIL_SMTP_PORT=465, GMAIL_SMTP_SSL=True, GMAIL_SMTP_TLS=False,
        GMAIL_SMTP_USER="synthetic-sender@gmail.com", GMAIL_SMTP_PASSWORD="synthetic-gmail-app-password",
        GMAIL_FROM_EMAIL="synthetic-sender@gmail.com",
        SMTP_HOST=None, SMTP_PORT=587, SMTP_SSL=False, SMTP_TLS=True,
        SMTP_USER=None, SMTP_PASSWORD=None, EMAILS_FROM_EMAIL=None, EMAILS_FROM_NAME="学伴市集",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_providers_route_by_recipient_domain():
    providers = mail.providers_from(_settings())
    assert [provider.name for provider in providers] == ["qq", "gmail"]
    assert mail.ordered_providers(providers, "synthetic@qq.com")[0].name == "qq"
    assert mail.ordered_providers(providers, "synthetic@163.com")[0].name == "qq"
    assert mail.ordered_providers(providers, "synthetic@gmail.com")[0].name == "gmail"
    assert mail.ordered_providers(providers, "synthetic@example.com")[0].name == "qq"


def test_gmail_delivery_falls_back_to_qq(monkeypatch):
    settings = _settings()
    calls = []

    def fake_deliver(provider, address, token, purpose):
        calls.append(provider.name)
        if provider.name == "gmail":
            raise RuntimeError("synthetic provider unavailable")

    monkeypatch.setattr(mail, "delivery_settings", lambda: settings)
    monkeypatch.setattr(mail, "_deliver", fake_deliver)

    mail.send_token("synthetic@gmail.com", "synthetic-token", "verify")
    assert calls == ["gmail", "qq"]


def test_all_providers_failing_returns_controlled_error(monkeypatch):
    settings = _settings()

    def always_fail(provider, address, token, purpose):
        raise RuntimeError("synthetic provider unavailable")

    monkeypatch.setattr(mail, "delivery_settings", lambda: settings)
    monkeypatch.setattr(mail, "_deliver", always_fail)

    with pytest.raises(HTTPException) as caught:
        mail.send_token("synthetic@qq.com", "synthetic-token", "reset")
    assert caught.value.status_code == 503
    assert "synthetic-token" not in str(caught.value.detail)


def test_unconfigured_providers_are_empty():
    settings = _settings(
        QQ_SMTP_USER=None, QQ_SMTP_PASSWORD=None,
        GMAIL_SMTP_USER=None, GMAIL_SMTP_PASSWORD=None,
        SMTP_HOST=None,
    )
    assert mail.providers_from(settings) == []


def test_legacy_smtp_provider_remains_supported():
    settings = _settings(
        QQ_SMTP_USER=None, QQ_SMTP_PASSWORD=None, GMAIL_SMTP_USER=None, GMAIL_SMTP_PASSWORD=None,
        SMTP_HOST="smtp.example.com", SMTP_PORT=587, SMTP_USER="synthetic-user",
        SMTP_PASSWORD="synthetic-password", EMAILS_FROM_EMAIL="sender@example.com",
    )
    providers = mail.providers_from(settings)
    assert [provider.name for provider in providers] == ["smtp"]
    assert providers[0].from_email == "sender@example.com"
