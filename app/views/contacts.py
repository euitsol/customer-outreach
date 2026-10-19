import io
from datetime import datetime
from urllib.parse import parse_qsl

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for
from sqlalchemy import func
from werkzeug.datastructures import MultiDict

from app import settings as app_settings
from app.importer import export_csv
from app.merge import slug
from app.models import Campaign, Contact, Event, Message, Tag, contact_tags
from app.queries import STATUS_LABELS, contact_query, contact_status, metrics_for, sort_query
from app.sender import render_for
from app.services import (
    EMAIL_RE, add_suppression, add_tags, cancel_queued, delete_contacts, get_message, get_or_create_message,
    queue_next, queue_stage, recompute_contact, remove_tags, reset_contact, retry_failed, set_tags, skip_stage,
    steps_of,
)
from app.views.common import (
    back, db, err, form_bool, form_ids, get_or_404, int_arg, ok, paginate, settings_now, warn,
)

bp = Blueprint("contacts", __name__)


def parse_custom(text):
    out = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        for sep in (":", "="):
            if sep in line:
                k, v = line.split(sep, 1)
                break
        else:
            continue
        k = slug(k)
        if k and v.strip():
            out[k] = v.strip()
    return out


def custom_to_text(contact):
    return "\n".join(f"{k}: {v}" for k, v in contact.custom_fields.items())


def apply_profile(contact, form):
    for f in ("company", "first_name", "last_name", "phone", "priority", "notes"):
        if f in form:
            setattr(contact, f, (form.get(f) or "").strip() or None)
    if "custom" in form:
        contact.custom_fields = parse_custom(form.get("custom"))


# ------------------------------------------------------------------ list

def _list_context(campaign=None):
    sess = db()
    q = contact_query(sess, request.args, campaign.id if campaign else None)
    q = sort_query(q, request.args.get("sort"))
    page = paginate(q)
    ids = [c.id for c in page.items]
    metrics = metrics_for(sess, ids)
    steps_by_campaign = {}
    for c in page.items:
        steps_by_campaign.setdefault(c.campaign_id, len(steps_of(sess, c.campaign_id)))
    camp_names = {i: n for i, n in sess.query(Campaign.id, Campaign.name)}
    tag_rows = {}
    if ids:
        for cid, name in (sess.query(contact_tags.c.contact_id, Tag.name)
                          .join(Tag, Tag.id == contact_tags.c.tag_id).filter(contact_tags.c.contact_id.in_(ids))):
            tag_rows.setdefault(cid, []).append(name)
    all_tags = [t for (t,) in sess.query(Tag.name).order_by(Tag.name)]
    priorities = [p for (p,) in sess.query(func.lower(Contact.priority)).filter(Contact.priority.isnot(None))
                  .distinct().all() if p]
    ctx = dict(page=page, metrics=metrics, camp_names=camp_names, tag_rows=tag_rows, all_tags=all_tags,
               priorities=sorted(priorities), nsteps=steps_by_campaign, contact_status=contact_status,
               campaigns=sess.query(Campaign).order_by(Campaign.name).all(), s=settings_now(),
               query_string=request.query_string.decode(), sort=request.args.get("sort") or "newest")
    if campaign:
        ctx.update(c=campaign, steps=steps_of(sess, campaign.id), tab="contacts")
    return ctx


@bp.route("/contacts")
def index():
    return render_template("contacts/index.html", scoped=False, **_list_context())


@bp.route("/campaigns/<int:cid>/contacts")
def in_campaign(cid):
    camp = get_or_404(Campaign, cid)
    return render_template("contacts/index.html", scoped=True, **_list_context(camp))


# ------------------------------------------------------------------ create / edit / delete

@bp.route("/contacts/new", methods=["GET", "POST"])
def new():
    sess = db()
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        camp = sess.get(Campaign, int_arg("campaign_id", 0, request.form) or 0)
        if not EMAIL_RE.match(email):
            err("Enter a valid email address.")
        elif not camp:
            err("Choose a campaign.")
        elif sess.query(Contact).filter(Contact.campaign_id == camp.id, func.lower(Contact.email) == email).first():
            err("That email already exists in this campaign.")
        else:
            c = Contact(campaign_id=camp.id, email=email, stage=0, send_flag=True)
            apply_profile(c, request.form)
            sess.add(c)
            sess.flush()
            set_tags(sess, c, request.form.get("tags", ""))
            recompute_contact(sess, c, campaign=camp)
            sess.commit()
            ok("Contact added.")
            return redirect(url_for("contacts.detail", contact_id=c.id))
    return render_template("contacts/new.html",
                           campaigns=sess.query(Campaign).filter(Campaign.status != "archived").order_by(Campaign.name).all(),
                           form=request.form, default_campaign=int_arg("campaign"))


@bp.route("/contacts/<int:contact_id>")
def detail(contact_id):
    sess = db()
    c = get_or_404(Contact, contact_id)
    camp = c.campaign
    s = settings_now()
    steps = steps_of(sess, camp.id)
    msgs = {m.step_id: m for m in c.messages}
    now = datetime.utcnow()
    rows = []
    for st in steps:
        m = msgs.get(st.id)
        subject, body = render_for(sess, c, st, steps, m, camp, s)
        is_next = st.position == (c.stage or 0) + 1
        if m and m.status == "sent":
            state = "sent"
        elif m and m.status == "skipped":
            state = "skipped"
        elif m and m.status == "failed":
            state = "failed"
        elif m and m.queued:
            state = "queued"
        elif is_next:
            state = "due" if (c.next_due_at is None or c.next_due_at <= now) else "scheduled"
        else:
            state = "upcoming"
        rows.append(dict(step=st, msg=m, state=state, is_next=is_next, subject=subject, body=body,
                         custom=bool(m and ((m.body or "").strip() or (m.subject or "").strip())),
                         due=(m.due_at if m and m.due_at else (c.next_due_at if is_next else None))))
    events = (sess.query(Event).filter(Event.contact_id == c.id,
                                       Event.type.in_(("sent", "failed", "opened", "clicked", "replied", "bounced", "unsubscribed")))
              .order_by(Event.id.desc()).limit(80).all())
    return render_template(
        "contacts/detail.html", c=c, camp=camp, rows=rows, events=events, tags=", ".join(sorted(t.name for t in c.tags)),
        custom_text=custom_to_text(c), status=contact_status(c, len(steps)), metrics=metrics_for(sess, [c.id])[c.id],
        campaigns=sess.query(Campaign).order_by(Campaign.name).all(), s=s, due_input=app_settings.to_local_input,
        nsteps=len(steps))


@bp.route("/contacts/<int:contact_id>/edit", methods=["POST"])
def edit(contact_id):
    sess = db()
    c = get_or_404(Contact, contact_id)
    email = (request.form.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        err("Enter a valid email address.")
        return redirect(url_for("contacts.detail", contact_id=contact_id))
    clash = sess.query(Contact).filter(Contact.campaign_id == c.campaign_id, func.lower(Contact.email) == email,
                                       Contact.id != c.id).first()
    if clash:
        err("Another contact in this campaign already has that email.")
        return redirect(url_for("contacts.detail", contact_id=contact_id))
    c.email = email
    apply_profile(c, request.form)
    c.send_flag = form_bool("send_flag")
    c.needs_verification = form_bool("needs_verification")
    set_tags(sess, c, request.form.get("tags", ""))
    sess.commit()
    ok("Contact saved.")
    return redirect(url_for("contacts.detail", contact_id=contact_id))


@bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
def delete(contact_id):
    sess = db()
    c = get_or_404(Contact, contact_id)
    cid = c.campaign_id
    delete_contacts(sess, [contact_id])
    sess.commit()
    ok("Contact deleted.")
    return redirect(url_for("contacts.in_campaign", cid=cid))


@bp.route("/contacts/<int:contact_id>/flag", methods=["POST"])
def flag(contact_id):
    sess = db()
    c = get_or_404(Contact, contact_id)
    name, value = request.form.get("name"), request.form.get("value") == "1"
    if name not in ("replied", "unsubscribed", "bounced"):
        abort(400)
    setattr(c, name, value)
    if name == "unsubscribed" and value:
        add_suppression(sess, c.email, "Marked unsubscribed manually")
        cancel_queued(sess, [c.id])
    sess.commit()
    ok(f"Marked {name}." if value else f"Cleared {name}.")
    return redirect(url_for("contacts.detail", contact_id=contact_id))


# ------------------------------------------------------------------ per-stage content for one person

@bp.route("/contacts/<int:contact_id>/stage/<int:step_id>", methods=["POST"])
def stage_save(contact_id, step_id):
    sess = db()
    c = get_or_404(Contact, contact_id)
    steps = steps_of(sess, c.campaign_id)
    st = next((x for x in steps if x.id == step_id), None)
    if not st:
        abort(404)
    m = get_or_create_message(sess, c, st)
    if m.status != "sent":
        m.subject = (request.form.get("subject") or "").strip() or None
        m.body = (request.form.get("body") or "").rstrip() or None
    due = request.form.get("due_at")
    if due is not None and m.status != "sent":
        m.due_at = app_settings.parse_local_input(due, settings_now())
    recompute_contact(sess, c, steps)
    sess.commit()
    ok(f"Stage {st.position} saved for {c.email}.")
    return redirect(url_for("contacts.detail", contact_id=contact_id) + f"#stage-{st.position}")


@bp.route("/contacts/<int:contact_id>/stage/<int:step_id>/action", methods=["POST"])
def stage_action(contact_id, step_id):
    sess = db()
    c = get_or_404(Contact, contact_id)
    steps = steps_of(sess, c.campaign_id)
    st = next((x for x in steps if x.id == step_id), None)
    if not st:
        abort(404)
    action = request.form.get("action")
    m = get_message(sess, c.id, st.id)
    if action in ("send_now", "retry", "resend"):
        if c.bounced or c.unsubscribed:
            err("This contact is bounced or unsubscribed; clear that flag first.")
        elif queue_stage(sess, c, st):
            sess.commit()
            ok(f"Stage {st.position} queued. The worker sends it within a minute or two "
               f"(subject to the daily limit and send window).")
        else:
            err("Could not queue this stage.")
    elif action == "skip":
        skip_stage(sess, c, st)
        sess.commit()
        ok(f"Stage {st.position} skipped.")
    elif action == "cancel":
        if m:
            m.queued = False
        sess.commit()
        ok("Cancelled.")
    elif action == "clear_custom":
        if m and m.status != "sent":
            m.subject = m.body = m.due_at = None
        recompute_contact(sess, c, steps)
        sess.commit()
        ok("Custom content removed; the campaign template will be used.")
    elif action == "reopen":
        # undo a skip / failure so the worker will pick the stage up normally again
        if m:
            m.status, m.attempts, m.next_attempt_at, m.last_error = "pending", 0, None, None
        if st.position <= (c.stage or 0) and m and not m.sent_at:
            c.stage = st.position - 1
        recompute_contact(sess, c, steps)
        sess.commit()
        ok("Stage re-opened.")
    else:
        abort(400)
    return redirect(url_for("contacts.detail", contact_id=contact_id) + f"#stage-{st.position}")


@bp.route("/contacts/<int:contact_id>/reset", methods=["POST"])
def reset(contact_id):
    sess = db()
    c = get_or_404(Contact, contact_id)
    reset_contact(sess, c)
    sess.commit()
    ok("Sequence restarted from stage 1. Previously sent history stays in the log.")
    return redirect(url_for("contacts.detail", contact_id=contact_id))


# ------------------------------------------------------------------ bulk

def selected_ids(sess):
    """Explicit ids, or every contact matching the filters when 'select all matching' was chosen."""
    if request.form.get("select_all") == "1":
        args = MultiDict(parse_qsl(request.form.get("qs", "")))
        scope = int_arg("scope_campaign", None, request.form)
        return [i for (i,) in contact_query(sess, args, scope).with_entities(Contact.id).all()]
    return form_ids("ids")


def move_contacts(sess, contacts, target):
    steps = {s.position: s for s in steps_of(sess, target.id)}
    existing = {e.lower() for (e,) in sess.query(Contact.email).filter(Contact.campaign_id == target.id)}
    moved = skipped = 0
    for c in contacts:
        if c.campaign_id == target.id:
            continue
        if c.email.lower() in existing:
            skipped += 1
            continue
        for m in list(c.messages):
            ts = steps.get(m.position)
            if ts is None:
                sess.delete(m)
                continue
            m.step_id = ts.id
            m.status, m.queued, m.attempts, m.last_error, m.next_attempt_at = "pending", False, 0, None, None
            m.sent_at = m.message_id = m.sent_subject = m.sent_body = None
        c.campaign_id = target.id
        c.stage, c.message_id, c.last_step_at = 0, None, None
        existing.add(c.email.lower())
        moved += 1
    sess.flush()
    for c in contacts:
        if c.campaign_id == target.id:
            recompute_contact(sess, c, list(steps.values()), target)
    return moved, skipped


@bp.route("/contacts/bulk", methods=["POST"])
def bulk():
    sess = db()
    action = request.form.get("action")
    ids = selected_ids(sess)
    if not ids:
        warn("No contacts selected.")
        return back("contacts.index")
    contacts = sess.query(Contact).filter(Contact.id.in_(ids)).all()
    n = skipped = 0
    msg = None

    if action == "export":
        steps = steps_of(sess, contacts[0].campaign_id) if len({c.campaign_id for c in contacts}) == 1 else []
        data = export_csv(sess, contacts, steps)
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=contacts_selection.csv"})
    if action == "delete":
        n = delete_contacts(sess, ids)
        msg = f"Deleted {n} contact(s)."
    elif action == "queue":
        cache = {}
        for c in contacts:
            steps = cache.setdefault(c.campaign_id, steps_of(sess, c.campaign_id))
            if queue_next(sess, c, steps):
                n += 1
            else:
                skipped += 1
        msg = f"{n} contact(s) queued to send their next email." + (
            f" {skipped} skipped (replied, unsubscribed, paused, nothing left to send or no content)." if skipped else "")
    elif action == "unqueue":
        n = cancel_queued(sess, ids)
        msg = f"Cancelled {n} queued send(s)."
    elif action == "retry":
        n = retry_failed(sess, contact_ids=ids)
        msg = f"{n} failed email(s) queued for retry."
    elif action in ("add_tags", "remove_tags"):
        tags = request.form.get("tags", "")
        if not tags.strip():
            warn("Type at least one tag.")
            return back("contacts.index")
        for c in contacts:
            (add_tags if action == "add_tags" else remove_tags)(sess, c, tags)
            n += 1
        msg = f"Updated tags on {n} contact(s)."
    elif action == "move":
        target = sess.get(Campaign, int_arg("campaign_id", 0, request.form) or 0)
        if not target:
            warn("Choose a campaign to move to.")
            return back("contacts.index")
        n, skipped = move_contacts(sess, contacts, target)
        msg = f"Moved {n} contact(s) to '{target.name}' (sequence restarted)." + (
            f" {skipped} skipped: already in that campaign." if skipped else "")
    elif action in ("enable", "disable"):
        for c in contacts:
            c.send_flag = action == "enable"
            n += 1
        msg = f"{'Enabled' if action == 'enable' else 'Paused'} sending for {n} contact(s)."
    elif action in ("verify", "unverify"):
        for c in contacts:
            c.needs_verification = action == "unverify"
            n += 1
        msg = f"Updated {n} contact(s)."
    elif action == "reset":
        for c in contacts:
            reset_contact(sess, c)
            n += 1
        msg = f"Restarted the sequence for {n} contact(s)."
    elif action in ("unsubscribe", "resubscribe"):
        for c in contacts:
            c.unsubscribed = action == "unsubscribe"
            if action == "unsubscribe":
                add_suppression(sess, c.email, "Marked unsubscribed manually")
            n += 1
        msg = f"Updated {n} contact(s)."
    elif action == "suppress":
        for c in contacts:
            add_suppression(sess, c.email, "Added from contact list")
            n += 1
        msg = f"Added {n} address(es) to the do-not-contact list."
    elif action == "reschedule":
        when = app_settings.parse_local_input(request.form.get("due_at"), settings_now())
        cache = {}
        for c in contacts:
            steps = cache.setdefault(c.campaign_id, steps_of(sess, c.campaign_id))
            nxt = next((x for x in steps if x.position == (c.stage or 0) + 1), None)
            if not nxt:
                skipped += 1
                continue
            get_or_create_message(sess, c, nxt).due_at = when
            recompute_contact(sess, c, steps)
            n += 1
        msg = (f"Next email set to {request.form.get('due_at')} for {n} contact(s)." if when
               else f"Cleared the custom send date for {n} contact(s).")
        if skipped:
            msg += f" {skipped} had no further stage."
    else:
        abort(400)
    sess.commit()
    ok(msg)
    return back("contacts.index")


@bp.route("/contacts/export")
def export():
    sess = db()
    q = sort_query(contact_query(sess, request.args, int_arg("campaign")), request.args.get("sort"))
    contacts = q.all()
    steps = steps_of(sess, contacts[0].campaign_id) if contacts and len({c.campaign_id for c in contacts}) == 1 else []
    return Response(export_csv(sess, contacts, steps), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=contacts_export.csv"})
