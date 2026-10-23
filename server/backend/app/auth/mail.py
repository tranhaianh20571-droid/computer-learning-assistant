"""Token delivery never returns credentials through the public auth API.

QQ / Gmail providers are routed by recipient domain and fall back to each other.
Provider credentials only come from the process environment or an ignored .env;
addresses, tokens and provider errors are never logged here.
"""
from dataclasses import dataclass
from email.message import EmailMessage
import smtplib
import ssl

from fastapi import HTTPException

QQ_DOMAINS = frozenset({"qq.com", "foxmail.com", "163.com", "126.com", "yeah.net"})
GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


@dataclass(frozen=True)
class MailProvider:
    name: str
    host: str
    port: int
    use_ssl: bool
    use_tls: bool
    user: str
    password: str
    from_email: str
    domains: frozenset[str] = frozenset()


def providers_from(settings) -> list[MailProvider]:
    """Configured providers in preference order: QQ, Gmail, legacy SMTP."""
    providers: list[MailProvider] = []
    qq_user = getattr(settings, "QQ_SMTP_USER", None)
    qq_password = getattr(settings, "QQ_SMTP_PASSWORD", None)
    if qq_user and qq_password:
        providers.append(MailProvider(
            name="qq",
            host=getattr(settings, "QQ_SMTP_HOST", "smtp.qq.com"),
            port=getattr(settings, "QQ_SMTP_PORT", 465),
            use_ssl=getattr(settings, "QQ_SMTP_SSL", True),
            use_tls=getattr(settings, "QQ_SMTP_TLS", False),
            user=qq_user,
            password=qq_password,
            from_email=str(getattr(settings, "QQ_FROM_EMAIL", None) or qq_user),
            domains=QQ_DOMAINS,
        ))
    gmail_user = getattr(settings, "GMAIL_SMTP_USER", None)
    gmail_password = getattr(settings, "GMAIL_SMTP_PASSWORD", None)
    if gmail_user and gmail_password:
        providers.append(MailProvider(
            name="gmail",
            host=getattr(settings, "GMAIL_SMTP_HOST", "smtp.gmail.com"),
            port=getattr(settings, "GMAIL_SMTP_PORT", 465),
            use_ssl=getattr(settings, "GMAIL_SMTP_SSL", True),
            use_tls=getattr(settings, "GMAIL_SMTP_TLS", False),
            user=gmail_user,
            password=gmail_password,
            from_email=str(getattr(settings, "GMAIL_FROM_EMAIL", None) or gmail_user),
            domains=GMAIL_DOMAINS,
        ))
    legacy_host = getattr(settings, "SMTP_HOST", None)
    legacy_from = getattr(settings, "EMAILS_FROM_EMAIL", None)
    if legacy_host and legacy_from:
        providers.append(MailProvider(
            name="smtp",
            host=legacy_host,
            port=getattr(settings, "SMTP_PORT", 587),
            use_ssl=getattr(settings, "SMTP_SSL", False),
            use_tls=getattr(settings, "SMTP_TLS", True),
            user=str(getattr(settings, "SMTP_USER", None) or ""),
            password=str(getattr(settings, "SMTP_PASSWORD", None) or ""),
            from_email=str(legacy_from),
        ))
    return providers


def ordered_providers(providers: list[MailProvider], address: str) -> list[MailProvider]:
    """Prefer the provider matching the recipient's mailbox domain."""
    domain = address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""
    preferred = [provider for provider in providers if domain in provider.domains]
    return preferred + [provider for provider in providers if provider not in preferred]


def delivery_settings():
    from ..infrastructure.config import settings
    if not providers_from(settings):
        raise HTTPException(503, detail={"error_code": "INTERNAL_ERROR",
            "message": "邮件服务未配置，请联系管理员。"})
    return settings


def _deliver(provider: MailProvider, address: str, token: str, purpose: str) -> None:
    message = EmailMessage()
    message["From"], message["To"] = provider.from_email, address
    message["Subject"] = "学习助手伙伴 · " + ("验证邮箱" if purpose == "verify" else "密码重置")
    message.set_content("请在学习助手伙伴的" + ("邮箱验证" if purpose == "verify" else "密码重置") +
                        "页面输入以下令牌：\n\n" + token + "\n\n请勿向他人透露。")
    kwargs = {"timeout": 15}
    if provider.use_ssl:
        cls = smtplib.SMTP_SSL
        kwargs["context"] = ssl.create_default_context()
    else:
        cls = smtplib.SMTP
    with cls(provider.host, provider.port, **kwargs) as smtp:
        if provider.use_tls and not provider.use_ssl:
            smtp.starttls(context=ssl.create_default_context())
        if provider.user:
            smtp.login(provider.user, provider.password)
        if smtp.send_message(message):
            raise RuntimeError("delivery rejected")


def send_token(address, token, purpose):
    settings = delivery_settings()
    providers = ordered_providers(providers_from(settings), address)
    for provider in providers:
        try:
            _deliver(provider, address, token, purpose)
            return
        except Exception:
            continue
    raise HTTPException(503, detail={"error_code": "INTERNAL_ERROR",
        "message": "邮件暂未发送成功，请联系管理员。"})
