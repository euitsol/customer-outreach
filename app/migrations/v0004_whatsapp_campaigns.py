"""WhatsApp campaigns.

* `campaign_steps.wa_template`: the Twilio Content SID a WhatsApp campaign stage sends.
"""
from app.migrations import ops

VERSION = 4
NAME = "whatsapp_campaigns"


def upgrade(conn):
    ops.add_column(conn, "campaign_steps", "wa_template", "VARCHAR(100)")
