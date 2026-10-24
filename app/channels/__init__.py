"""Messaging channels. Each channel module knows how to deliver one message; app.crm.conversations
decides what to send, records it and enforces consent / channel rules."""
from app.models import ChannelAccount

CHANNEL_LABELS = {"email": "Email", "whatsapp": "WhatsApp", "sms": "SMS"}


def account_for(session, kind):
    return (session.query(ChannelAccount)
            .filter(ChannelAccount.kind == kind, ChannelAccount.status != "disabled")
            .order_by(ChannelAccount.is_default.desc(), ChannelAccount.id).first())


def status(session):
    """{kind: {'configured': bool, 'account': ChannelAccount|None}} for the integrations page."""
    from app import mailer
    from app.channels import whatsapp
    return {
        "email": {"configured": mailer.smtp_configured(), "account": account_for(session, "email")},
        "whatsapp": {"configured": whatsapp.configured(), "account": account_for(session, "whatsapp")},
    }
