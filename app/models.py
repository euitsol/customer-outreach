from sqlalchemy import (
    Column, Integer, String, Boolean, Text, DateTime, Date, ForeignKey
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db import Base

class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    contacts = relationship("Contact", back_populates="campaign")

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False)
    company = Column(String(300))
    first_name = Column(String(150))
    last_name = Column(String(150))
    email = Column(String(300), nullable=False)
    priority = Column(String(50))

    send_flag = Column(Boolean, default=True)
    needs_verification = Column(Boolean, default=False)

    subject = Column(Text)
    body1 = Column(Text)
    body2 = Column(Text)
    body2_date = Column(Date, nullable=True)
    body3 = Column(Text)
    body3_date = Column(Date, nullable=True)

    stage = Column(Integer, default=0)         # 0=not sent,1=email1 sent,2=fu1 sent,3=fu2 sent
    message_id = Column(String(300), nullable=True)  # thread root, for In-Reply-To

    unsubscribed = Column(Boolean, default=False)
    replied = Column(Boolean, default=False)

    created_at = Column(DateTime, server_default=func.now())
    campaign = relationship("Campaign", back_populates="contacts")

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    stage = Column(Integer)
    type = Column(String(30))  # sent, failed, opened, clicked
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

class ClickLink(Base):
    __tablename__ = "click_links"
    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    stage = Column(Integer)
    target_url = Column(Text, nullable=False)