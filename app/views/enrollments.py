"""Campaign enrollments: who is in which sequence, per-person email text, bulk operations."""
from urllib.parse import parse_qsl

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for
from sqlalchemy import func
from werkzeug.datastructures import MultiDict

from app import settings as app_settings
from app.clock import utcnow
from app.crm import people as people_svc
from app.importer import export_csv
from app.merge import slug
from app.models import Campaign, Enrollment, Event, Person, Tag
from app.queries import enrollment_query, enrollment_status, metrics_for, sort_query
from app.sender import mark_bounced, render_for
from app.services import (
    EMAIL_RE, EnrollError, add_suppression, cancel_queued, delete_enrollments, enroll, get_message,
    get_or_create_message, mark_unsubscribed, queue_next, queue_stage, recompute_enrollment, reset_enrollment,
    retry_failed, skip_stage, steps_of,
)
from app.views.common import (
    actor, back, db, err, form_bool, form_ids, get_or_404, int_arg, ok, paginate, settings_now, warn,
)

bp = Blueprint("enrollments", __name__)


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


def custom_to_text(enrollment):
    return "\n".join(f"{k}: {v}" for k, v in enrollment.custom_fields.items())


def apply_person_profile(sess, person, form):
    """The enrollment page edits the most-used person fields too (they live on the person)."""
    data = {k: form.get(k) for k in ("first_name", "last_name", "job_title", "phone") if k in form}
    if "company" in form:
        data["company"] = form.get("company")
    people_svc.update(sess, person, data, actor())


# ------------------------------------------------------------------ old URLs

@bp.route("/contacts")
def legacy_index():
    return redirect(url_for("enrollments.index", **request.args))


@bp.route("/contacts/<int:enrollment_id>")
def legacy_detail(enrollment_id):
    return redirect(url_for("enrollments.detail", enrollment_id=enrollment_id))


@bp.route("/campaigns/<int:cid>/contacts")
def legacy_in_campaign(cid):
    return redirect(url_for("enrollments.in_campaign", cid=cid, **request.args))


# ------------------------------------------------------------------ list

def _list_context(campaign=None):
    sess = db()
    q = enrollment_query(sess, request.args, campaign.id if campaign else None)
    q = sort_query(q, request.args.get("sort"))
    page = paginate(q)
    ids = [e.id for e in page.items]
    metrics = metrics_for(sess, ids)
    steps_by_campaign = {}
    for e in page.items:
        steps_by_campaign.setdefault(e.campaign_id, len(steps_of(sess, e.campaign_id)))
    camp_names = {i: n for i, n in sess.query(Campaign.id, Campaign.name)}
    all_tags = [t for (t,) in sess.query(Tag.name).order_by(Tag.name)]
    priorities = [p for (p,) in sess.query(func.lower(Enrollment.priority)).filter(Enrollment.priority.isnot(None))
                  .distinct().all() if p]
    ctx = dict(page=page, metrics=metrics, camp_names=camp_names, all_tags=all_tags,
               priorities=sorted(priorities), nsteps=steps_by_campaign, enrollment_status=enrollment_status,
               campaigns=sess.query(Campaign).order_by(Campaign.name).all(), s=settings_now(),
               query_string=request.query_string.decode(), sort=request.args.get("sort") or "newest")
    if campaign:
        ctx.update(c=campaign, steps=steps_of(sess, campaign.id), tab="enrollments")
    return ctx


@bp.route("/enrollments")
def index():
    return render_template("enrollments/index.html", scoped=False, **_list_context())


@bp.route("/campaigns/<int:cid>/enrollments")
def in_campaign(cid):
    camp = get_or_404(Campaign, cid)
    return render_template("enrollments/index.html", scoped=True, **_list_context(camp))


# ------------------------------------------------------------------ create / edit / delete

@bp.route("/enrollments/new", methods=["GET", "POST"])
def new():
    """Add one person to a campaign (creating the person if the email is new)."""
    sess = db()
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        camp = sess.get(Campaign, int_arg("campaign_id", 0, request.form) or 0)
        if not EMAIL_RE.match(email):
            err("Enter a valid email address.")
        elif not camp:
            err("Choose a campaign.")
        else:
            try:
                person, _ = people_svc.upsert(
                    sess, email=email, first_name=request.form.get("first_name"),
                    last_name=request.form.get("last_name"), company_name=request.form.get("company"),
                    phone=request.form.get("phone"), job_title=request.form.get("job_title"), source="manual",
                    actor=actor())
                e, created = enroll(sess, camp, person, email=email, priority=request.form.get("priority") or None,
                                    variables=parse_custom(request.form.get("custom")), actor=actor())
                if not created:
                    raise EnrollError("That person is already in this campaign.")
                people_svc.add_tags(sess, person, request.form.get("tags", ""))
                if (request.form.get("notes") or "").strip():
                    from app.crm import activities
                    activities.log(sess, kind="note", body=request.form["notes"], person_id=person.id,
                                   user_id=actor().user_id, actor=actor())
                sess.commit()
                ok("Added to the campaign.")
                return redirect(url_for("enrollments.detail", enrollment_id=e.id))
            except (EnrollError, ValueError) as ex:
                sess.rollback()
                err(str(ex))
    return render_template("enrollments/new.html",
                           campaigns=sess.query(Campaign).filter(Campaign.status != "archived").order_by(Campaign.name).all(),
                           form=request.form, default_campaign=int_arg("campaign"),
                           person=sess.get(Person, int_arg("person", 0) or 0))


@bp.route("/enrollments/<int:enrollment_id>")
def detail(enrollment_id):
    sess = db()
    e = get_or_404(Enrollment, enrollment_id)
    camp = e.campaign
    s = settings_now()
    steps = steps_of(sess, camp.id)
    msgs = {m.step_id: m for m in e.messages}
    now = utcnow()
    rows = []
    for st in steps:
        m = msgs.get(st.id)
        subject, body = render_for(sess, e, st, steps, m, camp, s)
        is_next = st.position == (e.stage or 0) + 1
        if m and m.status == "sent":
            state = "sent"
        elif m and m.status == "skipped":
            state = "skipped"
        elif m and m.status == "failed":
            state = "failed"
        elif m and m.queued:
            state = "queued"
        elif is_next:
            state = "due" if (e.next_due_at is None or e.next_due_at <= now) else "scheduled"
        else:
            state = "upcoming"
        rows.append(dict(step=st, msg=m, state=state, is_next=is_next, subject=subject, body=body,
                         custom=bool(m and ((m.body or "").strip() or (m.subject or "").strip())),
                         due=(m.due_at if m and m.due_at else (e.next_due_at if is_next else None))))
    events = (sess.query(Event).filter(Event.enrollment_id == e.id,
                                       Event.type.in_(("sent", "failed", "opened", "clicked", "replied", "bounced", "unsubscribed")))
              .order_by(Event.id.desc()).limit(80).all())
    others = [x for x in e.person.enrollments if x.id != e.id] if e.person else []
    return render_template(
        "enrollments/detail.html", c=e, person=e.person, camp=camp, rows=rows, events=events,
        tags=", ".join(t.name for t in e.tags), custom_text=custom_to_text(e), status=enrollment_status(e, len(steps)),
        metrics=metrics_for(sess, [e.id])[e.id], others=others,
        campaigns=sess.query(Campaign).order_by(Campaign.name).all(), s=s, due_input=app_settings.to_local_input,
        nsteps=len(steps))


@bp.route("/enrollments/<int:enrollment_id>/edit", methods=["POST"])
def edit(enrollment_id):
    sess = db()
    e = get_or_404(Enrollment, enrollment_id)
    email = (request.form.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        err("Enter a valid email address.")
        return redirect(url_for("enrollments.detail", enrollment_id=enrollment_id))
    clash = sess.query(Enrollment).filter(Enrollment.campaign_id == e.campaign_id, func.lower(Enrollment.email) == email,
                                          Enrollment.id != e.id).first()
    if clash:
        err("Another person in this campaign already uses that email.")
        return redirect(url_for("enrollments.detail", enrollment_id=enrollment_id))
    try:
        if email != e.email:
            people_svc.add_contact_point(sess, e.person, "email", email, primary=not e.person.email)
        e.email = email
        apply_person_profile(sess, e.person, request.form)
    except ValueError as ex:
        sess.rollback()
        err(str(ex))
        return redirect(url_for("enrollments.detail", enrollment_id=enrollment_id))
    e.priority = (request.form.get("priority") or "").strip() or None
    if "custom" in request.form:
        e.custom_fields = parse_custom(request.form.get("custom"))
    e.send_flag = form_bool("send_flag")
    e.needs_verification = form_bool("needs_verification")
    if "tags" in request.form:
        people_svc.set_tags(sess, e.person, request.form.get("tags", ""))
    sess.commit()
    ok("Saved.")
    return redirect(url_for("enrollments.detail", enrollment_id=enrollment_id))


@bp.route("/enrollments/<int:enrollment_id>/delete", methods=["POST"])
def delete(enrollment_id):
    sess = db()
    e = get_or_404(Enrollment, enrollment_id)
    cid = e.campaign_id
    delete_enrollments(sess, [enrollment_id])
    sess.commit()
    ok("Removed from the campaign (the person stays in your CRM).")
    return redirect(url_for("enrollments.in_campaign", cid=cid))


@bp.route("/enrollments/<int:enrollment_id>/flag", methods=["POST"])
def flag(enrollment_id):
    sess = db()
    e = get_or_404(Enrollment, enrollment_id)
    name, value = request.form.get("name"), request.form.get("value") == "1"
    if name not in ("replied", "unsubscribed", "bounced"):
        abort(400)
    if name == "unsubscribed" and value:
        mark_unsubscribed(sess, e, "Marked unsubscribed manually", actor())
    elif name == "bounced" and value:
        mark_bounced(sess, e, "Marked bounced manually", "Marked bounced manually")
    else:
        setattr(e, name, value)
    sess.commit()
    ok(f"Marked {name}." if value else f"Cleared {name}.")
    return redirect(url_for("enrollments.detail", enrollment_id=enrollment_id))


# ------------------------------------------------------------------ per-stage content for one person

@bp.route("/enrollments/<int:enrollment_id>/stage/<int:step_id>", methods=["POST"])
def stage_save(enrollment_id, step_id):
    sess = db()
    e = get_or_404(Enrollment, enrollment_id)
    steps = steps_of(sess, e.campaign_id)
    st = next((x for x in steps if x.id == step_id), None)
    if not st:
        abort(404)
    m = get_or_create_message(sess, e, st)
    if m.status != "sent":
        m.subject = (request.form.get("subject") or "").strip() or None
        m.body = (request.form.get("body") or "").rstrip() or None
    due = request.form.get("due_at")
    if due is not None and m.status != "sent":
        m.due_at = app_settings.parse_local_input(due, settings_now())
    recompute_enrollment(sess, e, steps)
    sess.commit()
    ok(f"Stage {st.position} saved for {e.email}.")
    return redirect(url_for("enrollments.detail", enrollment_id=enrollment_id) + f"#stage-{st.position}")


@bp.route("/enrollments/<int:enrollment_id>/stage/<int:step_id>/action", methods=["POST"])
def stage_action(enrollment_id, step_id):
    sess = db()
    e = get_or_404(Enrollment, enrollment_id)
    steps = steps_of(sess, e.campaign_id)
    st = next((x for x in steps if x.id == step_id), None)
    if not st:
        abort(404)
    action = request.form.get("action")
    m = get_message(sess, e.id, st.id)
    if action in ("send_now", "retry", "resend"):
        if e.bounced or e.unsubscribed:
            err("This address is bounced or unsubscribed; clear that flag first.")
        elif queue_stage(sess, e, st):
            sess.commit()
            ok(f"Stage {st.position} queued. The worker sends it within a minute or two "
               f"(subject to the daily limit and send window).")
        else:
            err("Could not queue this stage.")
    elif action == "skip":
        skip_stage(sess, e, st)
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
        recompute_enrollment(sess, e, steps)
        sess.commit()
        ok("Custom content removed; the campaign template will be used.")
    elif action == "reopen":
        # undo a skip / failure so the worker will pick the stage up normally again
        if m:
            m.status, m.attempts, m.next_attempt_at, m.last_error = "pending", 0, None, None
        if st.position <= (e.stage or 0) and m and not m.sent_at:
            e.stage = st.position - 1
        recompute_enrollment(sess, e, steps)
        sess.commit()
        ok("Stage re-opened.")
    else:
        abort(400)
    return redirect(url_for("enrollments.detail", enrollment_id=enrollment_id) + f"#stage-{st.position}")


@bp.route("/enrollments/<int:enrollment_id>/reset", methods=["POST"])
def reset(enrollment_id):
    sess = db()
    e = get_or_404(Enrollment, enrollment_id)
    reset_enrollment(sess, e)
    sess.commit()
    ok("Sequence restarted from stage 1. Previously sent history stays in the log.")
    return redirect(url_for("enrollments.detail", enrollment_id=enrollment_id))


# ------------------------------------------------------------------ bulk

def selected_ids(sess):
    """Explicit ids, or every enrollment matching the filters when 'select all matching' was chosen."""
    if request.form.get("select_all") == "1":
        args = MultiDict(parse_qsl(request.form.get("qs", "")))
        scope = int_arg("scope_campaign", None, request.form)
        return [i for (i,) in enrollment_query(sess, args, scope, load=False).with_entities(Enrollment.id).all()]
    return form_ids("ids")


def move_enrollments(sess, enrollments, target):
    steps = {s.position: s for s in steps_of(sess, target.id)}
    existing = {p for (p,) in sess.query(Enrollment.person_id).filter(Enrollment.campaign_id == target.id)}
    taken = {e.lower() for (e,) in sess.query(Enrollment.email).filter(Enrollment.campaign_id == target.id)}
    moved = skipped = 0
    for e in enrollments:
        if e.campaign_id == target.id:
            continue
        if e.person_id in existing or e.email.lower() in taken:
            skipped += 1
            continue
        for m in list(e.messages):
            ts = steps.get(m.position)
            if ts is None:
                sess.delete(m)
                continue
            m.step_id = ts.id
            m.status, m.queued, m.attempts, m.last_error, m.next_attempt_at = "pending", False, 0, None, None
            m.sent_at = m.message_id = m.sent_subject = m.sent_body = None
        e.campaign_id = target.id
        e.stage, e.message_id, e.last_step_at = 0, None, None
        existing.add(e.person_id)
        taken.add(e.email.lower())
        moved += 1
    sess.flush()
    for e in enrollments:
        if e.campaign_id == target.id:
            sess.expire(e, ["campaign"])
            recompute_enrollment(sess, e, list(steps.values()), target)
    return moved, skipped


@bp.route("/enrollments/bulk", methods=["POST"])
def bulk():
    sess = db()
    action = request.form.get("action")
    ids = selected_ids(sess)
    if not ids:
        warn("Nothing selected.")
        return back("enrollments.index")
    rows = sess.query(Enrollment).filter(Enrollment.id.in_(ids)).all()
    n = skipped = 0
    msg = None

    if action == "export":
        steps = steps_of(sess, rows[0].campaign_id) if len({e.campaign_id for e in rows}) == 1 else []
        data = export_csv(sess, rows, steps)
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=enrollments_selection.csv"})
    if action == "delete":
        n = delete_enrollments(sess, ids)
        msg = f"Removed {n} enrollment(s). The people stay in your CRM."
    elif action == "queue":
        cache = {}
        for e in rows:
            steps = cache.setdefault(e.campaign_id, steps_of(sess, e.campaign_id))
            if queue_next(sess, e, steps):
                n += 1
            else:
                skipped += 1
        msg = f"{n} queued to send their next email." + (
            f" {skipped} skipped (replied, unsubscribed, paused, nothing left to send or no content)." if skipped else "")
    elif action == "unqueue":
        n = cancel_queued(sess, ids)
        msg = f"Cancelled {n} queued send(s)."
    elif action == "retry":
        n = retry_failed(sess, enrollment_ids=ids)
        msg = f"{n} failed email(s) queued for retry."
    elif action in ("add_tags", "remove_tags"):
        tags = request.form.get("tags", "")
        if not tags.strip():
            warn("Type at least one tag.")
            return back("enrollments.index")
        for person in {e.person for e in rows if e.person}:
            (people_svc.add_tags if action == "add_tags" else people_svc.remove_tags)(sess, person, tags)
            n += 1
        msg = f"Updated tags on {n} person(s)."
    elif action == "move":
        target = sess.get(Campaign, int_arg("campaign_id", 0, request.form) or 0)
        if not target:
            warn("Choose a campaign to move to.")
            return back("enrollments.index")
        n, skipped = move_enrollments(sess, rows, target)
        msg = f"Moved {n} to '{target.name}' (sequence restarted)." + (
            f" {skipped} skipped: already in that campaign." if skipped else "")
    elif action in ("enable", "disable"):
        for e in rows:
            e.send_flag = action == "enable"
            n += 1
        msg = f"{'Enabled' if action == 'enable' else 'Paused'} sending for {n} enrollment(s)."
    elif action in ("verify", "unverify"):
        for e in rows:
            e.needs_verification = action == "unverify"
            n += 1
        msg = f"Updated {n} enrollment(s)."
    elif action == "reset":
        for e in rows:
            reset_enrollment(sess, e)
            n += 1
        msg = f"Restarted the sequence for {n} enrollment(s)."
    elif action in ("unsubscribe", "resubscribe"):
        for e in rows:
            if action == "unsubscribe":
                mark_unsubscribed(sess, e, "Marked unsubscribed manually", actor())
            else:
                e.unsubscribed = False
            n += 1
        msg = f"Updated {n} enrollment(s)."
    elif action == "suppress":
        for e in rows:
            add_suppression(sess, e.email, "Added from enrollment list")
            n += 1
        msg = f"Added {n} address(es) to the do-not-contact list."
    elif action == "reschedule":
        when = app_settings.parse_local_input(request.form.get("due_at"), settings_now())
        cache = {}
        for e in rows:
            steps = cache.setdefault(e.campaign_id, steps_of(sess, e.campaign_id))
            nxt = next((x for x in steps if x.position == (e.stage or 0) + 1), None)
            if not nxt:
                skipped += 1
                continue
            get_or_create_message(sess, e, nxt).due_at = when
            recompute_enrollment(sess, e, steps)
            n += 1
        msg = (f"Next email set to {request.form.get('due_at')} for {n} enrollment(s)." if when
               else f"Cleared the custom send date for {n} enrollment(s).")
        if skipped:
            msg += f" {skipped} had no further stage."
    else:
        abort(400)
    sess.commit()
    ok(msg)
    return back("enrollments.index")


@bp.route("/enrollments/export")
def export():
    sess = db()
    q = sort_query(enrollment_query(sess, request.args, int_arg("campaign")), request.args.get("sort"))
    rows = q.all()
    steps = steps_of(sess, rows[0].campaign_id) if rows and len({e.campaign_id for e in rows}) == 1 else []
    return Response(export_csv(sess, rows, steps), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=enrollments_export.csv"})

