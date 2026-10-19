"""One-off migration from the pre-stages schema (body1/body2/body3 columns on contacts)
to campaign steps + per-contact messages. Idempotent: guarded by a settings flag."""
from datetime import datetime, time

from sqlalchemy import func

from app import settings
from app.models import Campaign, CampaignStep, Contact, Event, Message
from app.services import recompute_contact, steps_of

FLAG = "legacy_migrated_v1"


def migrate_legacy(session):
    if settings.get_raw(session, FLAG) == "1":
        return
    try:
        campaigns = session.query(Campaign).all()
        for camp in campaigns:
            if camp.status is None:
                camp.status = "active" if camp.is_active else "paused"
            if session.query(CampaignStep).filter_by(campaign_id=camp.id).count():
                continue
            contacts = session.query(Contact).filter(Contact.campaign_id == camp.id).all()
            n_steps = 0
            for c in contacts:
                if c.body3 and c.body3.strip():
                    n_steps = 3
                elif c.body2 and c.body2.strip():
                    n_steps = max(n_steps, 2)
                elif c.body1 and c.body1.strip():
                    n_steps = max(n_steps, 1)
            if not n_steps:
                continue
            names = ["Email 1", "Follow-up 1", "Follow-up 2"]
            delays = [0, 48, 72]
            steps = []
            for i in range(n_steps):
                st = CampaignStep(campaign_id=camp.id, position=i + 1, name=names[i], delay_hours=delays[i])
                session.add(st)
                steps.append(st)
            session.flush()

            sent_at = {}
            for cid, stage, first in (session.query(Event.contact_id, Event.stage, func.min(Event.created_at))
                                      .filter(Event.type == "sent").group_by(Event.contact_id, Event.stage)):
                sent_at[(cid, stage)] = first

            for c in contacts:
                legacy = [(c.subject, c.body1, None), (None, c.body2, c.body2_date), (None, c.body3, c.body3_date)]
                for i, st in enumerate(steps):
                    subj, body, due = legacy[i]
                    has_body = bool(body and body.strip())
                    done = (c.stage or 0) >= st.position
                    if not has_body and not done and not (subj and subj.strip()):
                        continue
                    m = Message(
                        contact_id=c.id, step_id=st.id, position=st.position, subject=subj or None,
                        body=body or None,
                        due_at=datetime.combine(due, time(0, 0)) if due and i > 0 else None,
                        status="sent" if done else "pending",
                        sent_at=sent_at.get((c.id, st.position)),
                        message_id=c.message_id if st.position == 1 else None,
                        queued=bool(c.queued) and st.position == (c.stage or 0) + 1,
                    )
                    session.add(m)
                last = [sent_at[(c.id, s.position)] for s in steps if (c.id, s.position) in sent_at]
                c.last_step_at = max(last) if last else None
            session.flush()
            for c in contacts:
                recompute_contact(session, c, steps, camp)
        settings.put(session, FLAG, "1")
        session.commit()
    except Exception:
        session.rollback()
        raise
