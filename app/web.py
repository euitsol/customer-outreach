import base64
from flask import Flask, Response, redirect, abort, render_template_string

from app.db import SessionLocal, init_db
from app.models import Event, ClickLink, Contact, Campaign

app = Flask(__name__)
init_db()

PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
)

@app.route("/t/o/<token>.gif")
def track_open(token):
    session = SessionLocal()
    ev = session.query(Event).filter(Event.type == "open_token", Event.detail == token).first()
    if ev:
        session.add(Event(contact_id=ev.contact_id, stage=ev.stage, type="opened"))
        session.commit()
    session.close()
    return Response(PIXEL_GIF, mimetype="image/gif")

@app.route("/t/c/<token>")
def track_click(token):
    session = SessionLocal()
    link = session.query(ClickLink).filter(ClickLink.token == token).first()
    if not link:
        session.close()
        abort(404)
    session.add(Event(contact_id=link.contact_id, stage=link.stage, type="clicked"))
    session.commit()
    target = link.target_url
    session.close()
    return redirect(target, code=302)

@app.route("/t/u/<token>")
def unsubscribe(token):
    session = SessionLocal()
    ev = session.query(Event).filter(Event.type == "unsub_token", Event.detail == token).first()
    if not ev:
        session.close()
        abort(404)
    contact = session.query(Contact).get(ev.contact_id)
    contact.unsubscribed = True
    session.commit()
    session.close()
    return "You've been unsubscribed and won't receive further emails from this sequence."

@app.route("/")
def dashboard():
    session = SessionLocal()
    campaigns = session.query(Campaign).all()
    rows = []
    for camp in campaigns:
        contacts = camp.contacts
        total = len(contacts)
        sent = sum(1 for c in contacts if c.stage >= 1)
        opened_ids = {e.contact_id for e in session.query(Event).filter(Event.type == "opened").all()}
        clicked_ids = {e.contact_id for e in session.query(Event).filter(Event.type == "clicked").all()}
        replied = sum(1 for c in contacts if c.replied)
        rows.append({
            "name": camp.name, "id": camp.id, "total": total, "sent": sent,
            "opened": sum(1 for c in contacts if c.id in opened_ids),
            "clicked": sum(1 for c in contacts if c.id in clicked_ids),
            "replied": replied,
        })
    session.close()
    tpl = """
    <h2>Campaigns</h2>
    <table border=1 cellpadding=6>
      <tr><th>Name</th><th>Total</th><th>Sent</th><th>Opened</th><th>Clicked</th><th>Replied</th></tr>
      {% for r in rows %}
      <tr>
        <td>{{r.name}}</td><td>{{r.total}}</td><td>{{r.sent}}</td>
        <td>{{r.opened}}</td><td>{{r.clicked}}</td><td>{{r.replied}}</td>
      </tr>
      {% endfor %}
    </table>
    """
    return render_template_string(tpl, rows=rows)