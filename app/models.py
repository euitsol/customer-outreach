"""Database schema.

Three layers:

* CRM core      - people, companies, their channel identities (contact_points), consents, tags,
                 deals/pipelines, activities, tasks, custom field definitions.
* Engagement    - omnichannel conversations (email, WhatsApp, ...) and the channel accounts they
                 run through; raw provider webhooks are kept for idempotency and replay.
* Outreach      - campaigns with ordered steps; a person is *enrolled* in a campaign
                 (enrollments), each step delivery is a sequence message, engagement is logged as
                 events.

Platform tables: users, api_keys, audit_log, settings, schema_migrations (see app.migrations).

Conventions: timestamps are naive UTC; flexible per-record data lives in `attributes` (JSONB on
Postgres) and is described by `custom_fields`; join tables are explicit (person_tags, company_tags,
deal_people) so they can carry their own columns.
"""
from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Table, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import relationship

from app.clock import utcnow
from app.db import Base, BigID, JSONDict, JSONList


def _created():
    return Column(DateTime, default=utcnow, server_default=func.now(), nullable=False)


def _updated():
    return Column(DateTime, default=utcnow, onupdate=utcnow)


# ====================================================================== platform

class User(Base):
    """A person who signs in to the panel. Records are owned / assigned to users."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(320), nullable=False, unique=True)
    name = Column(String(200))
    password_hash = Column(String(300))
    role = Column(String(20), nullable=False, default="member")       # owner | admin | member
    is_active = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime)
    created_at = _created()
    updated_at = _updated()

    @property
    def label(self):
        return self.name or self.email


class ApiKey(Base):
    """Bearer keys for the REST API and the MCP server. Only a SHA-256 hash is stored."""
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    prefix = Column(String(16), nullable=False, index=True)           # shown in the UI to recognise a key
    key_hash = Column(String(64), nullable=False, unique=True)
    scopes = Column(JSONList, nullable=False, default=lambda: ["read", "write"])
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    last_used_at = Column(DateTime)
    expires_at = Column(DateTime)
    revoked_at = Column(DateTime)
    created_at = _created()

    @property
    def active(self):
        from app.clock import utcnow as _now
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > _now())


class AuditLog(Base):
    """Who changed what - panel users, API keys, the MCP server (Claude) and background jobs."""
    __tablename__ = "audit_log"
    id = Column(BigID, primary_key=True)
    actor_type = Column(String(20), nullable=False)                   # user | api | mcp | system
    actor_id = Column(String(64))
    actor_label = Column(String(200))
    action = Column(String(50), nullable=False)                       # create | update | delete | send | ...
    entity_type = Column(String(30), nullable=False)
    entity_id = Column(String(64))
    changes = Column(JSONDict)
    created_at = _created()

    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_created_at", "created_at"),
    )


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text)


class CustomField(Base):
    """Definition of an extra field; values live in the owning record's `attributes` JSON."""
    __tablename__ = "custom_fields"
    id = Column(Integer, primary_key=True)
    entity = Column(String(20), nullable=False)                       # person | company | deal
    key = Column(String(64), nullable=False)
    label = Column(String(200), nullable=False)
    field_type = Column(String(20), nullable=False, default="text")   # text|textarea|number|date|select|multiselect|checkbox|url
    options = Column(JSONList)                                        # choices for select / multiselect
    is_required = Column(Boolean, nullable=False, default=False)
    position = Column(Integer, nullable=False, default=0)
    created_at = _created()

    __table_args__ = (UniqueConstraint("entity", "key", name="uq_custom_fields_entity_key"),)


# ====================================================================== CRM core

person_tags = Table(
    "person_tags", Base.metadata,
    Column("person_id", Integer, ForeignKey("people.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True),
    Column("created_at", DateTime, default=utcnow),
)

company_tags = Table(
    "company_tags", Base.metadata,
    Column("company_id", Integer, ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True),
    Column("created_at", DateTime, default=utcnow),
)


class Tag(Base):
    __tablename__ = "tags"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    color = Column(String(20))
    created_at = _created()


class Company(Base):
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True)
    name = Column(String(300), nullable=False)
    domain = Column(String(255), unique=True)                         # lower-case, no www.; NULL allowed many times
    website = Column(String(500))
    industry = Column(String(150))
    size = Column(String(50))                                         # 1-10, 11-50, ...
    phone = Column(String(50))
    country = Column(String(100))
    city = Column(String(150))
    address = Column(Text)
    description = Column(Text)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    attributes = Column(JSONDict, nullable=False, default=dict)
    created_at = _created()
    updated_at = _updated()

    owner = relationship("User")
    people = relationship("Person", back_populates="company", passive_deletes=True)
    tags = relationship("Tag", secondary=company_tags, order_by="Tag.name")

    __table_args__ = (Index("ix_companies_name", "name"),)


class Person(Base):
    """A real person (CRM contact). Can be enrolled in any number of campaigns."""
    __tablename__ = "people"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    first_name = Column(String(150))
    last_name = Column(String(150))
    job_title = Column(String(200))
    # Primary identities, mirrored from contact_points for fast lookups / sorting.
    email = Column(String(320), unique=True)                          # lower-case
    phone = Column(String(50), index=True)                            # E.164 when parseable
    lifecycle_stage = Column(String(30), nullable=False, default="lead", index=True)
    lead_status = Column(String(30))
    source = Column(String(100))
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    do_not_contact = Column(Boolean, nullable=False, default=False)
    attributes = Column(JSONDict, nullable=False, default=dict)
    last_contacted_at = Column(DateTime)
    last_activity_at = Column(DateTime, index=True)
    created_at = _created()
    updated_at = _updated()

    company = relationship("Company", back_populates="people")
    owner = relationship("User")
    tags = relationship("Tag", secondary=person_tags, order_by="Tag.name")
    contact_points = relationship("ContactPoint", back_populates="person", cascade="all, delete-orphan",
                                  passive_deletes=True, order_by="ContactPoint.id")
    consents = relationship("Consent", cascade="all, delete-orphan", passive_deletes=True)
    enrollments = relationship("Enrollment", back_populates="person", passive_deletes=True)

    @property
    def full_name(self):
        return f"{self.first_name or ''} {self.last_name or ''}".strip()

    @property
    def display_name(self):
        return self.full_name or self.email or self.phone or f"Person #{self.id}"


class ContactPoint(Base):
    """One way to reach a person: an email address, phone number, WhatsApp number, ...

    `value` is normalised (lower-case email, E.164 phone) and unique per kind, which is what lets
    an inbound email or WhatsApp message be matched to exactly one person.
    """
    __tablename__ = "contact_points"
    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(20), nullable=False)                         # email | phone | whatsapp | linkedin | ...
    value = Column(String(320), nullable=False)
    label = Column(String(50))                                        # work | personal | mobile
    is_primary = Column(Boolean, nullable=False, default=False)
    status = Column(String(20), nullable=False, default="active")     # active | unverified | bounced | invalid
    verified_at = Column(DateTime)
    created_at = _created()
    updated_at = _updated()

    person = relationship("Person", back_populates="contact_points")

    __table_args__ = (UniqueConstraint("kind", "value", name="uq_contact_points_kind_value"),)


class Consent(Base):
    """Per-channel permission (email marketing, WhatsApp, SMS). Latest decision wins."""
    __tablename__ = "consents"
    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    channel = Column(String(20), nullable=False)                      # email | whatsapp | sms
    status = Column(String(20), nullable=False)                       # granted | revoked
    source = Column(String(200))
    recorded_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("person_id", "channel", name="uq_consents_person_channel"),)


class Pipeline(Base):
    __tablename__ = "pipelines"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    is_default = Column(Boolean, nullable=False, default=False)
    position = Column(Integer, nullable=False, default=0)
    created_at = _created()

    stages = relationship("PipelineStage", back_populates="pipeline", order_by="PipelineStage.position",
                          cascade="all, delete-orphan", passive_deletes=True)


class PipelineStage(Base):
    __tablename__ = "pipeline_stages"
    id = Column(Integer, primary_key=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    position = Column(Integer, nullable=False, default=0)
    probability = Column(Integer, nullable=False, default=0)          # 0-100, used for weighted forecast
    kind = Column(String(10), nullable=False, default="open")         # open | won | lost

    pipeline = relationship("Pipeline", back_populates="stages")


deal_people = Table(
    "deal_people", Base.metadata,
    Column("deal_id", Integer, ForeignKey("deals.id", ondelete="CASCADE"), primary_key=True),
    Column("person_id", Integer, ForeignKey("people.id", ondelete="CASCADE"), primary_key=True, index=True),
    Column("role", String(50)),                                       # decision maker, champion, ...
)


class Deal(Base):
    __tablename__ = "deals"
    id = Column(Integer, primary_key=True)
    title = Column(String(300), nullable=False)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id", ondelete="RESTRICT"), nullable=False, index=True)
    stage_id = Column(Integer, ForeignKey("pipeline_stages.id", ondelete="RESTRICT"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="SET NULL"), index=True)  # main contact
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    amount = Column(Numeric(14, 2))
    currency = Column(String(3), nullable=False, default="EUR")
    status = Column(String(10), nullable=False, default="open", index=True)  # open | won | lost
    expected_close_date = Column(Date)
    closed_at = Column(DateTime)
    lost_reason = Column(String(300))
    source = Column(String(100))
    attributes = Column(JSONDict, nullable=False, default=dict)
    stage_changed_at = Column(DateTime)
    created_at = _created()
    updated_at = _updated()

    pipeline = relationship("Pipeline")
    stage = relationship("PipelineStage")
    company = relationship("Company")
    person = relationship("Person")
    owner = relationship("User")
    people = relationship("Person", secondary=deal_people)


class Activity(Base):
    """Something that happened with a person / company / deal: note, call, meeting, logged message."""
    __tablename__ = "activities"
    id = Column(Integer, primary_key=True)
    kind = Column(String(20), nullable=False)                         # note | call | meeting | email | whatsapp | system
    subject = Column(String(300))
    body = Column(Text)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"))
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    deal_id = Column(Integer, ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    occurred_at = Column(DateTime, nullable=False, default=utcnow)
    duration_minutes = Column(Integer)
    outcome = Column(String(50))
    meta = Column(JSONDict)
    created_at = _created()

    person = relationship("Person")
    company = relationship("Company")
    deal = relationship("Deal")
    user = relationship("User")

    __table_args__ = (Index("ix_activities_person_occurred", "person_id", "occurred_at"),)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    title = Column(String(300), nullable=False)
    notes = Column(Text)
    status = Column(String(15), nullable=False, default="open")       # open | done | cancelled
    priority = Column(String(10), nullable=False, default="normal")   # low | normal | high
    due_at = Column(DateTime)
    completed_at = Column(DateTime)
    assignee_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), index=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    deal_id = Column(Integer, ForeignKey("deals.id", ondelete="CASCADE"), index=True)
    created_at = _created()
    updated_at = _updated()

    assignee = relationship("User", foreign_keys=[assignee_id])
    person = relationship("Person")
    company = relationship("Company")
    deal = relationship("Deal")

    __table_args__ = (Index("ix_tasks_status_due", "status", "due_at"),)


# ====================================================================== engagement (omnichannel)

class ChannelAccount(Base):
    """A connected sending/receiving account: the SMTP mailbox, a WhatsApp Business number, ...

    Secrets (passwords, access tokens) stay in the environment; this row holds identity, non-secret
    configuration and health.
    """
    __tablename__ = "channel_accounts"
    id = Column(Integer, primary_key=True)
    kind = Column(String(20), nullable=False)                         # email | whatsapp | sms
    name = Column(String(200), nullable=False)
    address = Column(String(320))                                     # from-address or display phone number
    external_id = Column(String(200))                                 # e.g. WhatsApp phone_number_id
    config = Column(JSONDict, nullable=False, default=dict)
    status = Column(String(20), nullable=False, default="active")     # active | disabled | error
    last_error = Column(Text)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = _created()
    updated_at = _updated()

    __table_args__ = (UniqueConstraint("kind", "external_id", name="uq_channel_accounts_kind_external"),)


class Conversation(Base):
    """A thread with one person on one channel (the shared inbox)."""
    __tablename__ = "conversations"
    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_account_id = Column(Integer, ForeignKey("channel_accounts.id", ondelete="SET NULL"), index=True)
    channel = Column(String(20), nullable=False)
    subject = Column(String(500))
    status = Column(String(15), nullable=False, default="open")       # open | pending | closed
    assignee_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    external_thread_id = Column(String(500), index=True)              # root Message-ID / provider thread
    unread_count = Column(Integer, nullable=False, default=0)
    last_message_at = Column(DateTime)
    last_inbound_at = Column(DateTime)                                # WhatsApp 24h service window
    created_at = _created()
    updated_at = _updated()

    person = relationship("Person")
    channel_account = relationship("ChannelAccount")
    assignee = relationship("User")
    messages = relationship("ConversationMessage", back_populates="conversation", passive_deletes=True,
                            order_by="ConversationMessage.id")

    __table_args__ = (Index("ix_conversations_status_last", "status", "last_message_at"),)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id = Column(BigID, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    direction = Column(String(3), nullable=False)                     # in | out
    channel = Column(String(20), nullable=False)
    sender = Column(String(320))
    recipient = Column(String(320))
    subject = Column(String(500))
    body = Column(Text)
    status = Column(String(15), nullable=False, default="received")   # received|queued|sent|delivered|read|failed
    external_id = Column(String(300))                                 # Message-ID / wamid
    error = Column(Text)
    # A campaign email is stored once, on the sequence message; the inbox links to it.
    sequence_message_id = Column(Integer, ForeignKey("messages.id", ondelete="SET NULL"))
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    meta = Column(JSONDict)
    sent_at = Column(DateTime)
    delivered_at = Column(DateTime)
    read_at = Column(DateTime)
    created_at = _created()

    conversation = relationship("Conversation", back_populates="messages")
    sequence_message = relationship("Message")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("channel", "external_id", name="uq_conversation_messages_channel_external"),
        Index("ix_conversation_messages_conversation", "conversation_id", "id"),
    )


class WebhookEvent(Base):
    """Raw inbound provider callbacks. Unique (provider, external_id) makes retries idempotent."""
    __tablename__ = "webhook_events"
    id = Column(BigID, primary_key=True)
    provider = Column(String(30), nullable=False)
    external_id = Column(String(200), nullable=False)
    event_type = Column(String(50))
    payload = Column(JSONDict)
    status = Column(String(15), nullable=False, default="received")   # received | processed | failed | ignored
    error = Column(Text)
    received_at = Column(DateTime, nullable=False, default=utcnow, index=True)
    processed_at = Column(DateTime)

    __table_args__ = (UniqueConstraint("provider", "external_id", name="uq_webhook_events_provider_external"),)


# ====================================================================== outreach (campaigns)

class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text)
    channel = Column(String(20), nullable=False, default="email")
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
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = _updated()

    enrollments = relationship("Enrollment", back_populates="campaign", passive_deletes=True)
    steps = relationship("CampaignStep", back_populates="campaign",
                         order_by="CampaignStep.position", cascade="all, delete-orphan", passive_deletes=True)
    owner = relationship("User")


class CampaignStep(Base):
    """One email in a campaign's sequence (stage)."""
    __tablename__ = "campaign_steps"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
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


class Enrollment(Base):
    """A person's membership of one campaign, and where they are in its sequence.

    `email` is the address this sequence sends to (normally the person's primary email).
    `variables` holds campaign-specific merge fields (research notes, offer, ...); person and
    company fields are merged in on top at render time.
    """
    __tablename__ = "enrollments"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    email = Column(String(320), nullable=False, index=True)
    priority = Column(String(50))
    variables = Column(JSONDict)

    send_flag = Column(Boolean, default=True)           # False = paused for this enrollment
    needs_verification = Column(Boolean, default=False)
    unsubscribed = Column(Boolean, default=False)
    replied = Column(Boolean, default=False)
    bounced = Column(Boolean, default=False)

    stage = Column(Integer, default=0)                  # position of the last completed stage
    message_id = Column(String(300), nullable=True)     # RFC Message-ID of stage 1 (for threading)
    next_due_at = Column(DateTime, nullable=True, index=True)  # NULL = due immediately
    last_step_at = Column(DateTime, nullable=True)      # when the previous stage was sent/skipped

    created_at = Column(DateTime, server_default=func.now())
    updated_at = _updated()

    campaign = relationship("Campaign", back_populates="enrollments")
    person = relationship("Person", back_populates="enrollments")
    messages = relationship("Message", back_populates="enrollment", cascade="all, delete-orphan",
                            passive_deletes=True)

    __table_args__ = (
        Index("ix_enrollments_campaign_email", "campaign_id", "email"),
        UniqueConstraint("campaign_id", "person_id", name="uq_enrollments_campaign_person"),
    )

    # ---- convenience accessors used by templates, merge fields and exports
    @property
    def first_name(self):
        return self.person.first_name if self.person else None

    @property
    def last_name(self):
        return self.person.last_name if self.person else None

    @property
    def full_name(self):
        return self.person.full_name if self.person else ""

    @property
    def company(self):
        p = self.person
        return p.company.name if p and p.company else None

    @property
    def phone(self):
        return self.person.phone if self.person else None

    @property
    def tags(self):
        return self.person.tags if self.person else []

    @property
    def custom_fields(self):
        return dict(self.variables or {})

    @custom_fields.setter
    def custom_fields(self, value):
        value = {k: v for k, v in (value or {}).items() if k and v not in (None, "")}
        self.variables = value or None


class Message(Base):
    """The email for one enrollment at one stage: optional per-person overrides + delivery state."""
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    enrollment_id = Column(Integer, ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey("campaign_steps.id", ondelete="CASCADE"), nullable=False, index=True)
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
    message_id = Column(String(300), index=True)        # RFC Message-ID (reply matching)
    sent_subject = Column(Text)                         # exact snapshot of what went out
    sent_body = Column(Text)
    open_token = Column(String(64), index=True)
    unsub_token = Column(String(64), index=True)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    enrollment = relationship("Enrollment", back_populates="messages")
    step = relationship("CampaignStep")

    __table_args__ = (UniqueConstraint("enrollment_id", "step_id", name="uq_messages_enrollment_step"),)


class Event(Base):
    """Engagement log for campaign emails (sent, opened, clicked, replied, bounced, ...)."""
    __tablename__ = "events"
    id = Column(BigID, primary_key=True)
    enrollment_id = Column(Integer, ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False, index=True)
    stage = Column(Integer)
    type = Column(String(30), index=True)  # sent, failed, opened, clicked, replied, bounced, unsubscribed
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), default=utcnow, index=True)

    enrollment = relationship("Enrollment")

    __table_args__ = (Index("ix_events_enrollment_type", "enrollment_id", "type"),)


class ClickLink(Base):
    __tablename__ = "click_links"
    id = Column(BigID, primary_key=True)
    token = Column(String(64), unique=True, nullable=False)
    enrollment_id = Column(Integer, ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False, index=True)
    stage = Column(Integer)
    target_url = Column(Text, nullable=False)


class Suppression(Base):
    """Global do-not-contact list: exact addresses, whole domains or phone numbers."""
    __tablename__ = "suppressions"
    id = Column(Integer, primary_key=True)
    kind = Column(String(10), nullable=False)           # email | domain | phone
    value = Column(String(300), nullable=False, index=True)
    reason = Column(String(200))
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("kind", "value", name="uq_suppression"),)


class Template(Base):
    """Reusable message snippets that can be loaded into any campaign stage or inbox reply."""
    __tablename__ = "templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    channel = Column(String(20), nullable=False, default="email")
    subject = Column(Text)
    body = Column(Text)
    meta = Column(JSONDict)                             # e.g. WhatsApp template name / language
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
