"""CLI: seed the cleaned Ireland CSV, then wipe it again."""
from pathlib import Path

from app import manage
from app.db import SessionLocal
from app.models import Campaign, Enrollment, Message, Person

CSV = Path(__file__).resolve().parent.parent / "seeds" / "ireland_hospitality_resend.csv"
ORIGINAL = next(Path(__file__).resolve().parent.parent.glob("Ireland_Leads*.csv"), None)


def test_seed_replace_and_reset(capsys):
    for _ in range(2):                                    # second run proves --replace is repeatable
        assert manage.main(["seed", str(CSV), "--campaign", "Resend", "--replace"]) == 0
    s = SessionLocal()
    camp = s.query(Campaign).filter_by(name="Resend").one()
    assert camp.status == "draft"                         # never auto-started
    contacts = s.query(Enrollment).filter_by(campaign_id=camp.id).all()
    assert len(contacts) == 18                            # 17 Send + 1 "Verify email first"; 2 "Do not send" skipped
    assert sum(c.needs_verification for c in contacts) == 1
    jack = s.query(Enrollment).filter_by(campaign_id=camp.id, email="jack@sproutfoodco.com").one()
    m1 = s.query(Message).filter_by(enrollment_id=jack.id, position=1).one()
    assert m1.subject == "Following up: More repeat office orders for Sprout"
    assert "sending it once more" in m1.body and "\n- 1,500+ projects" in m1.body
    assert jack.person.company.website == "https://sproutfoodco.com"
    assert jack.person.company.domain == "sproutfoodco.com"
    assert "offer-b" in [t.name for t in jack.person.tags]
    assert s.query(Message).filter_by(enrollment_id=jack.id, position=2).one().due_at is None
    assert s.query(Person).count() >= 18                  # every enrollment has a person
    s.close()

    assert manage.main(["reset"]) == 0                    # without --yes nothing is deleted
    s = SessionLocal()
    assert s.query(Enrollment).count() >= 18
    s.close()
    assert manage.main(["reset", "--yes"]) == 0
    s = SessionLocal()
    assert s.query(Enrollment).count() == 0 and s.query(Campaign).count() == 0 and s.query(Person).count() == 0
    s.close()


def test_original_messy_sheet_still_imports():
    if ORIGINAL is None:
        return
    assert manage.main(["seed", str(ORIGINAL), "--campaign", "Original sheet", "--replace"]) == 0
    s = SessionLocal()
    assert s.query(Enrollment).filter_by(campaign_id=s.query(Campaign).filter_by(name="Original sheet").one().id).count() == 18
    s.close()
