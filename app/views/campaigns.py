from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app import merge, settings as app_settings
from app.mailer import SendError, smtp_configured
from app.models import Campaign, CampaignStep, Company, CustomField, Enrollment, Message, Person, Template
from app.queries import campaign_summaries, daily_activity, engagement_stats, stage_stats, status_counts
from app.sender import render_for, send_test
from app.services import (
    cancel_queued, create_default_steps, delete_campaign, duplicate_campaign, queue_next,
    recompute_campaign, renumber_steps, retry_failed, set_campaign_status, steps_of,
)
from app.views.common import back, db, err, form_bool, get_or_404, int_arg, ok, settings_now, warn

bp = Blueprint("campaigns", __name__)

STATUS_ORDER = ["active", "paused", "draft", "archived"]


def campaign_vars(session, campaign_id):
    """Extra merge fields available in this campaign: imported columns + person/company custom fields."""
    keys = []
    rows = (session.query(Enrollment.variables)
            .filter(Enrollment.campaign_id == campaign_id, Enrollment.variables.isnot(None)).limit(500).all())
    for (data,) in rows:
        for k in (data or {}):
            if k not in keys:
                keys.append(k)
    for (k,) in session.query(CustomField.key).filter(CustomField.entity.in_(("person", "company"))):
        if k not in keys:
            keys.append(k)
    if "website" not in keys:
        keys.append("website")
    return keys


def sample_enrollment(session, cid, enrollment_id=None):
    """A real enrollment of this campaign for previews, or a made-up one."""
    e = session.get(Enrollment, enrollment_id) if enrollment_id else None
    if e is not None and e.campaign_id == cid:
        return e
    company = Company(name="Example Ltd", website="https://example.com", domain="example.com", attributes={})
    person = Person(first_name="Alex", last_name="Sample", email="alex@example.com", phone="+353 1 000 0000",
                    job_title="Owner", company=company, attributes={})
    return Enrollment(campaign_id=cid, email="alex@example.com", person=person, variables=None)


def _form_int(name, minimum=0):
    v = (request.form.get(name) or "").strip()
    if not v:
        return None
    try:
        return max(minimum, int(v))
    except ValueError:
        return None


# ------------------------------------------------------------------ list / create

@bp.route("/campaigns")
def index():
    sess = db()
    q = sess.query(Campaign)
    status = request.args.get("status") or ""
    term = (request.args.get("q") or "").strip()
    if status:
        q = q.filter(Campaign.status == status)
    elif request.args.get("all") != "1":
        q = q.filter(Campaign.status != "archived")
    if term:
        q = q.filter(func.lower(Campaign.name).like(f"%{term.lower()}%"))
    campaigns = q.order_by(Campaign.id.desc()).all()
    counts = dict(sess.query(Campaign.status, func.count(Campaign.id)).group_by(Campaign.status).all())
    return render_template("campaigns/index.html", campaigns=campaigns, summaries=campaign_summaries(sess),
                           status=status, term=term, counts=counts, STATUS_ORDER=STATUS_ORDER)


@bp.route("/campaigns/new", methods=["GET", "POST"])
def new():
    sess = db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            err("Give the campaign a name.")
        elif sess.query(Campaign).filter(func.lower(Campaign.name) == name.lower()).first():
            err("A campaign with that name already exists.")
        else:
            c = Campaign(name=name, description=(request.form.get("description") or "").strip() or None,
                         status="draft", is_active=False)
            sess.add(c)
            sess.flush()
            clone_from = int_arg("clone_from", None, request.form)
            if clone_from and sess.get(Campaign, clone_from):
                for st in steps_of(sess, clone_from):
                    sess.add(CampaignStep(campaign_id=c.id, position=st.position, name=st.name,
                                          subject=st.subject, body=st.body, delay_hours=st.delay_hours))
            else:
                create_default_steps(sess, c)
            sess.commit()
            ok("Campaign created. Write your emails, then import contacts.")
            return redirect(url_for("campaigns.sequence", cid=c.id))
    others = sess.query(Campaign).order_by(Campaign.name).all()
    return render_template("campaigns/new.html", others=others, form=request.form)


# ------------------------------------------------------------------ overview

def _ctx(cid, tab):
    c = get_or_404(Campaign, cid)
    return c, steps_of(db(), c.id), tab


@bp.route("/campaigns/<int:cid>")
def overview(cid):
    sess = db()
    c, steps, _ = _ctx(cid, "overview")
    counts = status_counts(sess, cid)
    eng = engagement_stats(sess, cid)
    stages = stage_stats(sess, c, steps)
    activity = daily_activity(sess, 14, cid)
    peak = max([max(d["sent"], d["opened"], d["clicked"], d["replied"]) for d in activity] + [1])
    # contacts that would fail because nothing is written for their next stage
    first = steps[0] if steps else None
    no_content = 0
    if first:
        has_custom = (sess.query(Message.enrollment_id).filter(
            Message.step_id == first.id, func.coalesce(Message.body, "") != ""))
        q = sess.query(func.count(Enrollment.id)).filter(Enrollment.campaign_id == cid, Enrollment.stage == 0,
                                                          ~Enrollment.id.in_(has_custom))
        no_content = q.scalar() if not (first.body or "").strip() else 0
    return render_template("campaigns/overview.html", c=c, steps=steps, tab="overview", counts=counts, eng=eng,
                           stages=stages, activity=activity, peak=peak, no_content=no_content,
                           s=settings_now())


@bp.route("/campaigns/<int:cid>/status", methods=["POST"])
def set_status(cid):
    sess = db()
    c = get_or_404(Campaign, cid)
    action = request.form.get("action")
    if action == "start":
        steps = steps_of(sess, cid)
        if not steps:
            err("Add at least one email stage before starting.")
            return back("campaigns.sequence", cid=cid)
        if not smtp_configured():
            warn("SMTP is not configured on the server - emails will fail until it is.")
        set_campaign_status(c, "active")
        recompute_campaign(sess, c)
        ok(f"'{c.name}' is live. The worker sends within the limits set in Settings.")
    elif action == "pause":
        set_campaign_status(c, "paused")
        ok("Paused. Nothing goes out automatically (manual 'send now' still works).")
    elif action == "archive":
        set_campaign_status(c, "archived")
        ok("Campaign archived.")
    elif action == "restore":
        set_campaign_status(c, "paused")
        ok("Campaign restored (paused).")
    else:
        abort(400)
    sess.commit()
    return back("campaigns.overview", cid=cid)


@bp.route("/campaigns/<int:cid>/edit", methods=["GET", "POST"])
def edit(cid):
    sess = db()
    c = get_or_404(Campaign, cid)
    s = settings_now()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        clash = sess.query(Campaign).filter(func.lower(Campaign.name) == name.lower(), Campaign.id != cid).first()
        if not name:
            err("Name is required.")
        elif clash:
            err("Another campaign already uses that name.")
        else:
            c.name = name
            c.description = (request.form.get("description") or "").strip() or None
            new_start = app_settings.parse_local_input(request.form.get("start_at"), s)
            start_changed = new_start != c.start_at
            c.start_at = new_start
            c.daily_limit = _form_int("daily_limit", 1)
            c.min_delay, c.max_delay = _form_int("min_delay"), _form_int("max_delay")
            if c.min_delay is not None and c.max_delay is not None and c.max_delay < c.min_delay:
                c.max_delay = c.min_delay
            c.track_opens = form_bool("track_opens")
            c.track_clicks = form_bool("track_clicks")
            c.sender_name = (request.form.get("sender_name") or "").strip() or None
            c.reply_to = (request.form.get("reply_to") or "").strip() or None
            if start_changed:
                recompute_campaign(sess, c)
            sess.commit()
            ok("Campaign settings saved.")
            return redirect(url_for("campaigns.edit", cid=cid))
    n_contacts = sess.query(func.count(Enrollment.id)).filter(Enrollment.campaign_id == cid).scalar() or 0
    return render_template("campaigns/edit.html", c=c, steps=steps_of(sess, cid), tab="settings", s=s,
                           start_value=app_settings.to_local_input(c.start_at, s), n_contacts=n_contacts)


@bp.route("/campaigns/<int:cid>/delete", methods=["POST"])
def delete(cid):
    sess = db()
    c = get_or_404(Campaign, cid)
    if (request.form.get("confirm_name") or "").strip() != c.name:
        err("Type the campaign name exactly to confirm deletion.")
        return redirect(url_for("campaigns.edit", cid=cid))
    name = c.name
    n = delete_campaign(sess, c)
    sess.commit()
    ok(f"Deleted '{name}' and {n} contact(s).")
    return redirect(url_for("campaigns.index"))


@bp.route("/campaigns/<int:cid>/duplicate", methods=["POST"])
def duplicate(cid):
    sess = db()
    c = get_or_404(Campaign, cid)
    base = f"{c.name} (copy)"
    name, i = base, 2
    while sess.query(Campaign).filter(func.lower(Campaign.name) == name.lower()).first():
        name, i = f"{base} {i}", i + 1
    new = duplicate_campaign(sess, c, name)
    sess.commit()
    ok("Duplicated the campaign's emails and settings (contacts are not copied).")
    return redirect(url_for("campaigns.sequence", cid=new.id))


# ------------------------------------------------------------------ sequence editor

@bp.route("/campaigns/<int:cid>/sequence")
def sequence(cid):
    sess = db()
    c, steps, _ = _ctx(cid, "sequence")
    sent_by_step = dict(sess.query(Message.step_id, func.count(Message.id))
                        .filter(Message.status == "sent", Message.step_id.in_([x.id for x in steps] or [0]))
                        .group_by(Message.step_id).all())
    custom_keys = campaign_vars(sess, cid)
    variables = merge.BUILTIN_VARS + [k for k in custom_keys if k not in merge.BUILTIN_VARS]
    sample = (sess.query(Enrollment).filter(Enrollment.campaign_id == cid)
              .options(selectinload(Enrollment.person)).order_by(Enrollment.id).limit(20).all())
    templates = sess.query(Template).order_by(Template.name).all()
    unknown = {}
    for st in steps:
        bad = merge.unknown_vars((st.subject or "") + " " + (st.body or ""), set(variables))
        if bad:
            unknown[st.id] = bad
    return render_template("campaigns/sequence.html", c=c, steps=steps, tab="sequence", sent_by_step=sent_by_step,
                           variables=variables, sample=sample, templates=templates, unknown=unknown,
                           smtp_ok=smtp_configured(), s=settings_now())


@bp.route("/campaigns/<int:cid>/steps/add", methods=["POST"])
def step_add(cid):
    sess = db()
    c = get_or_404(Campaign, cid)
    steps = steps_of(sess, cid)
    if len(steps) >= 12:
        err("A campaign can have at most 12 stages.")
        return redirect(url_for("campaigns.sequence", cid=cid))
    n = len(steps) + 1
    tpl = sess.get(Template, int_arg("template_id", None, request.form)) if request.form.get("template_id") else None
    sess.add(CampaignStep(campaign_id=cid, position=n, name="Email 1" if n == 1 else f"Follow-up {n - 1}",
                          delay_hours=0 if n == 1 else 72,
                          subject=tpl.subject if tpl else None, body=tpl.body if tpl else None))
    sess.commit()
    recompute_campaign(sess, c)
    sess.commit()
    ok(f"Stage {n} added.")
    return redirect(url_for("campaigns.sequence", cid=cid) + f"#stage-{n}")


@bp.route("/campaigns/<int:cid>/steps/<int:sid>/save", methods=["POST"])
def step_save(cid, sid):
    sess = db()
    c = get_or_404(Campaign, cid)
    st = get_or_404(CampaignStep, sid)
    if st.campaign_id != cid:
        abort(404)
    st.name = (request.form.get("name") or "").strip() or f"Stage {st.position}"
    st.subject = (request.form.get("subject") or "").strip() or None
    st.body = (request.form.get("body") or "").rstrip() or None
    days, hours = _form_int("delay_days") or 0, _form_int("delay_hours") or 0
    new_delay = days * 24 + hours
    delay_changed = new_delay != (st.delay_hours or 0)
    st.delay_hours = new_delay
    if delay_changed:
        recompute_campaign(sess, c)
    sess.commit()
    known = set(merge.BUILTIN_VARS) | set(campaign_vars(sess, cid))
    bad = merge.unknown_vars((st.subject or "") + " " + (st.body or ""), known)
    if bad:
        warn("Saved. Unknown merge field(s) will be blank: " + ", ".join("{{" + b + "}}" for b in bad))
    else:
        ok(f"Stage {st.position} saved.")
    return redirect(url_for("campaigns.sequence", cid=cid) + f"#stage-{st.position}")


@bp.route("/campaigns/<int:cid>/steps/<int:sid>/delete", methods=["POST"])
def step_delete(cid, sid):
    sess = db()
    c = get_or_404(Campaign, cid)
    st = get_or_404(CampaignStep, sid)
    if st.campaign_id != cid:
        abort(404)
    if sess.query(Message).filter(Message.step_id == sid, Message.status == "sent").count():
        err("Emails were already sent at this stage, so it can't be deleted. Clear its text instead.")
        return redirect(url_for("campaigns.sequence", cid=cid))
    sess.query(Message).filter(Message.step_id == sid).delete()
    sess.delete(st)
    sess.flush()
    renumber_steps(sess, cid)
    sess.flush()
    recompute_campaign(sess, c)
    sess.commit()
    ok("Stage removed.")
    return redirect(url_for("campaigns.sequence", cid=cid))


@bp.route("/campaigns/<int:cid>/steps/<int:sid>/move", methods=["POST"])
def step_move(cid, sid):
    sess = db()
    c = get_or_404(Campaign, cid)
    steps = steps_of(sess, cid)
    idx = next((i for i, x in enumerate(steps) if x.id == sid), None)
    if idx is None:
        abort(404)
    j = idx - 1 if request.form.get("dir") == "up" else idx + 1
    if j < 0 or j >= len(steps):
        return redirect(url_for("campaigns.sequence", cid=cid))
    a, b = steps[idx], steps[j]
    if sess.query(Message).filter(Message.step_id.in_((a.id, b.id)), Message.status == "sent").count():
        err("Those stages have already been sent to some contacts, so their order is locked.")
        return redirect(url_for("campaigns.sequence", cid=cid))
    a.position, b.position = b.position, a.position
    for m in sess.query(Message).filter(Message.step_id == a.id):
        m.position = a.position
    for m in sess.query(Message).filter(Message.step_id == b.id):
        m.position = b.position
    sess.flush()
    recompute_campaign(sess, c)
    sess.commit()
    return redirect(url_for("campaigns.sequence", cid=cid) + f"#stage-{a.position}")


@bp.route("/campaigns/<int:cid>/steps/<int:sid>/preview")
def step_preview(cid, sid):
    sess = db()
    c = get_or_404(Campaign, cid)
    st = get_or_404(CampaignStep, sid)
    if st.campaign_id != cid:
        abort(404)
    steps = steps_of(sess, cid)
    enrollment = sample_enrollment(sess, cid, int_arg("enrollment_id") or int_arg("contact_id"))
    # allow previewing unsaved edits from the form
    over_subject, over_body = request.args.get("subject"), request.args.get("body")
    if over_subject is not None or over_body is not None:
        probe = CampaignStep(campaign_id=cid, position=st.position,
                             subject=over_subject if over_subject is not None else st.subject,
                             body=over_body if over_body is not None else st.body)
        steps = [probe if x.id == st.id else x for x in steps]
        st = probe
    with sess.no_autoflush:
        subject, body = render_for(sess, enrollment, st, steps, None if not enrollment.id else
                                   sess.query(Message).filter_by(enrollment_id=enrollment.id, step_id=sid).first(),
                                   c, settings_now())
    return jsonify({"subject": subject, "body": body, "to": enrollment.email})


@bp.route("/campaigns/<int:cid>/steps/<int:sid>/test", methods=["POST"])
def step_test(cid, sid):
    sess = db()
    c = get_or_404(Campaign, cid)
    st = get_or_404(CampaignStep, sid)
    to = (request.form.get("to") or "").strip()
    if "@" not in to:
        err("Enter an email address to send the test to.")
        return redirect(url_for("campaigns.sequence", cid=cid) + f"#stage-{st.position}")
    enrollment = sample_enrollment(sess, cid, int_arg("enrollment_id", 0, request.form)
                                   or int_arg("contact_id", 0, request.form))
    try:
        with sess.no_autoflush:
            send_test(sess, st, steps_of(sess, cid), c, enrollment, to, settings_now())
        ok(f"Test email sent to {to} (tracking disabled).")
    except SendError as e:
        err(f"Test failed: {e}")
    return redirect(url_for("campaigns.sequence", cid=cid) + f"#stage-{st.position}")


# ------------------------------------------------------------------ stage-level actions

@bp.route("/campaigns/<int:cid>/steps/<int:sid>/action", methods=["POST"])
def step_action(cid, sid):
    """Bulk operations on one stage: retry failed, send now to everyone waiting, cancel queued."""
    sess = db()
    c = get_or_404(Campaign, cid)
    st = get_or_404(CampaignStep, sid)
    if st.campaign_id != cid:
        abort(404)
    action = request.form.get("action")
    if action == "retry_failed":
        n = retry_failed(sess, step_id=sid)
        sess.commit()
        ok(f"{n} failed email(s) at stage {st.position} queued for retry." if n else "Nothing failed at this stage.")
    elif action == "send_now":
        steps = steps_of(sess, cid)
        n = skipped = 0
        for e in sess.query(Enrollment).filter(Enrollment.campaign_id == cid, Enrollment.stage == st.position - 1):
            if queue_next(sess, e, steps):
                n += 1
            else:
                skipped += 1
        sess.commit()
        ok(f"{n} contact(s) queued for stage {st.position}." + (f" {skipped} skipped (paused, replied, unsubscribed or no content)." if skipped else ""))
    elif action == "cancel_queued":
        ids = [i for (i,) in sess.query(Message.enrollment_id).filter(Message.step_id == sid, Message.queued == True)]  # noqa: E712
        n = cancel_queued(sess, ids) if ids else 0
        sess.commit()
        ok(f"Cancelled {n} queued send(s).")
    else:
        abort(400)
    return back("campaigns.overview", cid=cid)
