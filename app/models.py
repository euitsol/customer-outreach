import json
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Boolean, Text, DateTime, Date, ForeignKey, Table,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db import Base


def utcnow():
    return datetime.utcnow()


contact_tags = Table(
    "contact_tags", Base.metadata,
    Column("contact_id", Integer, ForeignKey("contacts.id"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id"), primary_key=True),
)


class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    # draft | active | paused | archived.  `is_active` is kept in sync for old data.
    status = Column(String(20), default="draft", index=True)
    is_active = Column(Boolean, default=False)
    start_at = Column(DateTime, nullable=True)          # don't send the first email before this
    daily_limit = Column(Integer, nullable=True)        # per-campaign cap (None = global only)
    min_delay = Column(Integer, nullable=True)          # seconds between emails (None = global)
    max_delay = Column(Integer, nullable=True)
    track_opens = Column(Boolean, default=True)
    track_clicks = Column(Boolean, default=True)
    sender_name = Column(String(200), nullable=True)
    reply_to = Column(String(300), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    contacts = relationship("Contact", back_populates="campaign")
    steps = relationship("CampaignStep", back_populates="campaign",
                         order_by="CampaignStep.position", cascade="all, delete-orphan")


class CampaignStep(Base):
    """One email in a campaign's sequence (stage)."""
    __tablename__ = "campaign_steps"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)
    position = Column(Integer, nullable=False)          # 1-based
    name = Column(String(200))
    subject = Column(Text)                              # empty on follow-ups => "Re: <first subject>"
    body = Column(Text)
    delay_hours = Column(Integer, default=0)            # wait after the previous stage was sent
    created_at = Column(DateTime, server_default=func.now())

    campaign = relationship("Campaign", back_populates="steps")

    @property
    def delay_days_part(self):
        return (self.delay_hours or 0) // 24

    @property
    def delay_hours_part(self):
        return (self.delay_hours or 0) % 24


class Tag(Base):
    __tablename__ = "tags"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)


class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)
    company = Column(String(300))
    first_name = Column(String(150))
    last_name = Column(String(150))
    email = Column(String(300), nullable=False, index=True)
    phone = Column(String(100))
    priority = Column(String(50))
    notes = Column(Text)
    custom = Column(Text)                               # JSON object of extra merge fields

    send_flag = Column(Boolean, default=True)           # False = paused for this contact
    needs_verification = Column(Boolean, default=False)
    unsubscribed = Column(Boolean, default=False)
    replied = Column(Boolean, default=False)
    bounced = Column(Boolean, default=False)

    stage = Column(Integer, default=0)                  # position of the last completed stage
    message_id = Column(String(300), nullable=True)     # RFC Message-ID of stage 1 (for threading)
    next_due_at = Column(DateTime, nullable=True, index=True)  # NULL = due immediately
    last_step_at = Column(DateTime, nullable=True)      # when the previous stage was sent/skipped

    # --- legacy columns (pre-stages schema). Only read by the one-off data migration. ---
    queued = Column(Boolean, default=False)
    subject = Column(Text)
    body1 = Column(Text)
    body2 = Column(Text)
    body2_date = Column(Date, nullable=True)
    body3 = Column(Text)
    body3_date = Column(Date, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    campaign = relationship("Campaign", back_populates="contacts")
    tags = relationship("Tag", secondary=contact_tags, backref="contacts")
    messages = relationship("Message", back_populates="contact", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_contacts_campaign_email", "campaign_id", "email"),)

    @property
    def full_name(self):
        return f"{self.first_name or ''} {self.last_name or ''}".strip()

    @property
    def custom_fields(self):
        try:
            data = json.loads(self.custom) if self.custom else {}
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    @custom_fields.setter
    def custom_fields(self, value):
        value = {k: v for k, v in (value or {}).items() if k and v not in (None, "")}
        self.custom = json.dumps(value, ensure_ascii=False) if value else None


class Message(Base):
    """The email for one contact at one stage: optional per-person overrides + delivery state."""
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey("campaign_steps.id"), nullable=False, index=True)
    position = Column(Integer, nullable=False)

    # per-person customisation (NULL/empty = use the campaign stage template)
    subject = Column(Text)
    body = Column(Text)
    due_at = Column(DateTime, nullable=True)            # override of the stage delay

    status = Column(String(20), default="pending", index=True)  # pending|sent|failed|skipped
    queued = Column(Boolean, default=False, index=True)         # send as soon as possible
    attempts = Column(Integer, default=0)
    last_error = Column(Text)
    next_attempt_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    message_id = Column(String(300))
    sent_subject = Column(Text)                         # exact snapshot of what went out
    sent_body = Column(Text)
    open_token = Column(String(64), index=True)
    unsub_token = Column(String(64), index=True)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    contact = relationship("Contact", back_populates="messages")
    step = relationship("CampaignStep")

    __table_args__ = (UniqueConstraint("contact_id", "step_id", name="uq_message_contact_step"),)


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False, index=True)
    stage = Column(Integer)
    type = Column(String(30), index=True)  # sent, failed, opened, clicked, replied, bounced, unsubscribed
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)


class ClickLink(Base):
    __tablename__ = "click_links"
    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    stage = Column(Integer)
    target_url = Column(Text, nullable=False)


class Suppression(Base):
    """Global do-not-contact list: exact addresses or whole domains."""
    __tablename__ = "suppressions"
    id = Column(Integer, primary_key=True)
    kind = Column(String(10), nullable=False)           # email | domain
    value = Column(String(300), nullable=False, index=True)
    reason = Column(String(200))
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("kind", "value", name="uq_suppression"),)


class Template(Base):
    """Reusable email snippets that can be loaded into any campaign stage."""
    __tablename__ = "templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    subject = Column(Text)
    body = Column(Text)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text)
