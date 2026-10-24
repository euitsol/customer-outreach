"""People (CRM contacts)."""
import csv
import io
from urllib.parse import parse_qsl

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for
from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import selectinload
from werkzeug.datastructures import MultiDict

from app import mailer
from app.channels import whatsapp
from app.clock import utcnow
from app.crm import (
    activities as activities_svc, conversations as conv_svc, custom_fields as cf_svc, people as people_svc,
)
from app.models import (
    Campaign, Company, Consent, ContactPoint, Conversation, Deal, Enrollment, Person, Tag, Template, User,
    person_tags,
)
from app.services import EnrollError, enroll, enroll_people
from app.views.common import (
    actor, back, current_user, db, err, form_ids, get_or_404, int_arg, ok, paginate, settings_now, warn,
)

bp = Blueprint("people", __name__)

SORTS = {
    "recent": (Person.id.desc(),),
    "name": (func.lower(func.coalesce(Person.first_name, "")), func.lower(func.coalesce(Person.last_name, "")),
             Person.id),
    "email": (func.lower(func.coalesce(Person.email, "")), Person.id),
    "company": (func.lower(func.coalesce(Company.name, "")), Person.id),
    "activity": (Person.last_activity_at.desc().nullslast(), Person.id.desc()),
    "contacted": (Person.last_contacted_at.desc().nullslast(), Person.id.desc()),
}


def people_query(sess, args, load=True):
    q = people_svc.search_query(sess, args.get("q"))
    if not (args.get("q") or "").strip():
        q = q.outerjoin(Company, Company.id == Person.company_id)
    if load:
        q = q.options(selectinload(Person.company), selectinload(Person.tags), selectinload(Person.owner))
    if args.get("lifecycle"):
        q = q.filter(Person.lifecycle_stage == args["lifecycle"])
    if args.get("lead_status"):
        q = q.filter(Person.lead_status == args["lead_status"])
    owner = args.get("owner")
    if owner == "none":
        q = q.filter(Person.owner_id.is_(None))
    elif owner and owner.isdigit():
        q = q.filter(Person.owner_id == int(owner))
    tag = (args.get("tag") or "").strip().lower()
    if tag == "__none__":
        q = q.filter(~exists().where(person_tags.c.person_id == Person.id))
    elif tag:
        q = q.filter(exists().where(and_(person_tags.c.person_id == Person.id, person_tags.c.tag_id == Tag.id,
                                         Tag.name == tag)))
    company_id = args.get("company")
    if company_id and str(company_id).isdigit():
        q = q.filter(Person.company_id == int(company_id))
    has = args.get("has")
    if has == "email":
        q = q.filter(Person.email.isnot(None))
    elif has == "phone":
        q = q.filter(Person.phone.isnot(None))
    elif has == "no_email":
        q = q.filter(Person.email.is_(None))
    camp = args.get("campaign")
    if camp == "none":
        q = q.filter(~exists().where(Enrollment.person_id == Person.id))
    elif camp and str(camp).isdigit():
        q = q.filter(exists().where(and_(Enrollment.person_id == Person.id, Enrollment.campaign_id == int(camp))))
    if args.get("dnc") == "1":
        q = q.filter(or_(Person.do_not_contact == True,  # noqa: E712
                         exists().where(and_(Consent.person_id == Person.id, Consent.status == "revoked"))))
    if args.get("engaged") == "replied":
        q = q.filter(exists().where(and_(Enrollment.person_id == Person.id, Enrollment.replied == True)))  # noqa: E712
    return q


def sorted_people(q, key):
    return q.order_by(*SORTS.get(key, SORTS["recent"]))


def _choices(sess):
    return dict(
        lifecycle_stages=people_svc.LIFECYCLE_STAGES, lead_statuses=people_svc.LEAD_STATUSES,
        users=sess.query(User).filter(User.is_active == True).order_by(User.name, User.email).all(),  # noqa: E712
        all_tags=[t for (t,) in sess.query(Tag.name).order_by(Tag.name)],
        campaigns=sess.query(Campaign).filter(Campaign.status != "archived").order_by(Campaign.name).all(),
    )


# ------------------------------------------------------------------ list

@bp.route("/people")
def index():
    sess = db()
    q = sorted_people(people_query(sess, request.args), request.args.get("sort"))
    page = paginate(q)
    ids = [p.id for p in page.items]
    enrolled = {}
    if ids:
        for pid, n in (sess.query(Enrollment.person_id, func.count(Enrollment.id))
                       .filter(Enrollment.person_id.in_(ids)).group_by(Enrollment.person_id)):
            enrolled[pid] = n
    return render_template("people/index.html", page=page, enrolled=enrolled, sort=request.args.get("sort") or "recent",
                           query_string=request.query_string.decode(), **_choices(sess))


def selected_people(sess):
    if request.form.get("select_all") == "1":
        args = MultiDict(parse_qsl(request.form.get("qs", "")))
        return [i for (i,) in people_query(sess, args, load=False).with_entities(Person.id).all()]
    return form_ids("ids")


@bp.route("/people/bulk", methods=["POST"])
def bulk():
    sess = db()
    action = request.form.get("action")
    ids = selected_people(sess)
    if not ids:
        warn("Nothing selected.")
        return back("people.index")
    rows = sess.query(Person).filter(Person.id.in_(ids)).all()
    who = actor()
    if action == "export":
        return _csv_response(rows, "people_selection.csv")
    if action in ("add_tags", "remove_tags"):
        tags = request.form.get("tags", "")
        if not tags.strip():
            warn("Type at least one tag.")
            return back("people.index")
        for p in rows:
            (people_svc.add_tags if action == "add_tags" else people_svc.remove_tags)(sess, p, tags)
        msg = f"Updated tags on {len(rows)} person(s)."
    elif action == "lifecycle":
        stage = request.form.get("lifecycle_stage")
        if stage not in people_svc.LIFECYCLE_KEYS:
            warn("Choose a lifecycle stage.")
            return back("people.index")
        for p in rows:
            people_svc.update(sess, p, {"lifecycle_stage": stage}, who)
        msg = f"Moved {len(rows)} person(s) to {dict(people_svc.LIFECYCLE_STAGES)[stage]}."
    elif action == "owner":
        owner_id = int_arg("owner_id", None, request.form)
        for p in rows:
            people_svc.update(sess, p, {"owner_id": owner_id}, who)
        msg = f"Owner set for {len(rows)} person(s)."
    elif action == "enroll":
        camp = sess.get(Campaign, int_arg("campaign_id", 0, request.form) or 0)
        if not camp:
            warn("Choose a campaign.")
            return back("people.index")
        st = enroll_people(sess, camp, rows, who)
        msg = (f"Enrolled {st['enrolled']} in '{camp.name}'."
               + (f" {st['already']} already there." if st["already"] else "")
               + (f" {st['no_email']} without email." if st["no_email"] else "")
               + (f" {st['blocked']} blocked (do-not-contact / suppressed)." if st["blocked"] else ""))
    elif action == "merge":
        if len(rows) != 2:
            warn("Select exactly two people to merge.")
            return back("people.index")
        a, b = sorted(rows, key=lambda p: p.id)
        return redirect(url_for("people.merge", person_id=a.id, other=b.id))
    elif action == "delete":
        for p in rows:
            people_svc.delete(sess, p, who)
        msg = f"Deleted {len(rows)} person(s) and their history."
    else:
        abort(400)
    sess.commit()
    ok(msg)
    return back("people.index")


def _csv_response(rows, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    keys = []
    for p in rows:
        for k in (p.attributes or {}):
            if k not in keys:
                keys.append(k)
    w.writerow(["id", "first_name", "last_name", "email", "phone", "job_title", "company", "company_website",
                "lifecycle_stage", "lead_status", "owner", "tags", "source", "do_not_contact", "created_at"] + keys)
    for p in rows:
        w.writerow([p.id, p.first_name, p.last_name, p.email, p.phone, p.job_title,
                    p.company.name if p.company else "", p.company.website if p.company else "",
                    p.lifecycle_stage, p.lead_status, p.owner.label if p.owner else "",
                    ",".join(t.name for t in p.tags), p.source, "yes" if p.do_not_contact else "",
                    p.created_at.date().isoformat() if p.created_at else ""]
                   + [(", ".join(map(str, v)) if isinstance(v, list) else v) for v in
                      ((p.attributes or {}).get(k, "") for k in keys)])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.route("/people/export.csv")
def export():
    sess = db()
    rows = sorted_people(people_query(sess, request.args), request.args.get("sort")).limit(100000).all()
    return _csv_response(rows, "people.csv")


# ------------------------------------------------------------------ create

@bp.route("/people/new", methods=["GET", "POST"])
def new():
    sess = db()
    form = request.form
    if request.method == "POST":
        email = (form.get("email") or "").strip()
        if not email and not (form.get("phone") or "").strip() and not (form.get("first_name") or "").strip():
            err("Give at least a name, an email or a phone number.")
        elif email and people_svc.find_by_email(sess, email):
            existing = people_svc.find_by_email(sess, email)
            warn("That email already belongs to someone; opened their record.")
            return redirect(url_for("people.detail", person_id=existing.id))
        else:
            try:
                attrs = cf_svc.values_from_form(sess, "person", form)
                person, _ = people_svc.upsert(
                    sess, email=email or None, phone=form.get("phone"), first_name=form.get("first_name"),
                    last_name=form.get("last_name"), job_title=form.get("job_title"),
                    company_name=form.get("company"), company_website=form.get("company_website"),
                    source=(form.get("source") or "manual").strip(), lifecycle_stage=form.get("lifecycle_stage"),
                    owner_id=int_arg("owner_id", None, form) or (current_user().id if current_user() else None),
                    attributes=attrs, actor=actor())
                if form.get("lifecycle_stage") in people_svc.LIFECYCLE_KEYS:
                    person.lifecycle_stage = form["lifecycle_stage"]
                people_svc.add_tags(sess, person, form.get("tags", ""))
                camp = sess.get(Campaign, int_arg("campaign_id", 0, form) or 0)
                if camp and person.email:
                    enroll(sess, camp, person, actor=actor())
                sess.commit()
                ok("Person added.")
                return redirect(url_for("people.detail", person_id=person.id))
            except (ValueError, EnrollError) as e:
                sess.rollback()
                err(str(e))
    return render_template("people/new.html", form=form, fields=cf_svc.definitions(sess, "person"),
                           company=sess.get(Company, int_arg("company", 0) or 0), **_choices(sess))


# ------------------------------------------------------------------ detail

@bp.route("/people/<int:person_id>")
def detail(person_id):
    sess = db()
    p = get_or_404(Person, person_id)
    s = settings_now()
    timeline = people_svc.timeline(sess, p, 150)
    conversations = (sess.query(Conversation).filter(Conversation.person_id == p.id)
                     .order_by(Conversation.last_message_at.desc().nullslast()).all())
    wa_conv = next((c for c in conversations if c.channel == "whatsapp"), None)
    deals = sess.query(Deal).filter(or_(Deal.person_id == p.id, Deal.people.any(Person.id == p.id))) \
        .order_by(Deal.status, Deal.id.desc()).all()
    tasks = activities_svc.task_query(sess, "open", person_id=p.id).all()
    consents = {c.channel: c for c in p.consents}
    enrolled_ids = {e.campaign_id for e in p.enrollments}
    return render_template(
        "people/detail.html", p=p, timeline=timeline, now=utcnow(), conversations=conversations, deals=deals, tasks=tasks,
        consents=consents, fields=cf_svc.definitions(sess, "person"), s=s,
        activity_kinds=activities_svc.ACTIVITY_KINDS, enrolled_ids=enrolled_ids,
        wa_window=conv_svc.whatsapp_window_open(wa_conv) if wa_conv else False,
        channels={"email": mailer.smtp_configured(), "whatsapp": whatsapp.configured()},
        wa_templates=sess.query(Template).filter(Template.channel == "whatsapp").order_by(Template.name).all(),
        email_templates=sess.query(Template).filter(Template.channel == "email").order_by(Template.name).all(),
        tags=", ".join(t.name for t in p.tags), **_choices(sess))


@bp.route("/people/<int:person_id>/edit", methods=["POST"])
def edit(person_id):
    sess = db()
    p = get_or_404(Person, person_id)
    f = request.form
    try:
        data = {k: f.get(k) for k in ("first_name", "last_name", "job_title", "email", "phone", "lifecycle_stage",
                                      "lead_status", "source") if k in f}
        if "owner_id" in f:
            data["owner_id"] = int_arg("owner_id", None, f)
        if "company" in f:
            data["company"] = f.get("company")
            data["company_website"] = f.get("company_website")
        data["do_not_contact"] = f.get("do_not_contact") == "1"
        data["attributes"] = cf_svc.values_from_form(sess, "person", f)
        people_svc.update(sess, p, data, actor())
        if "tags" in f:
            people_svc.set_tags(sess, p, f.get("tags", ""))
        sess.commit()
        ok("Saved.")
    except ValueError as e:
        sess.rollback()
        err(str(e))
    return redirect(url_for("people.detail", person_id=person_id))


@bp.route("/people/<int:person_id>/delete", methods=["POST"])
def delete(person_id):
    sess = db()
    p = get_or_404(Person, person_id)
    name = p.display_name
    people_svc.delete(sess, p, actor())
    sess.commit()
    ok(f"Deleted {name}.")
    return redirect(url_for("people.index"))


@bp.route("/people/<int:person_id>/points", methods=["POST"])
def points(person_id):
    sess = db()
    p = get_or_404(Person, person_id)
    action = request.form.get("action", "add")
    try:
        if action == "add":
            people_svc.add_contact_point(sess, p, request.form.get("kind") or "email", request.form.get("value"),
                                         label=(request.form.get("label") or "").strip() or None,
                                         primary=request.form.get("primary") == "1")
            ok("Added.")
        else:
            cp = get_or_404(ContactPoint, int_arg("point_id", 0, request.form))
            if cp.person_id != p.id:
                abort(404)
            if action == "primary":
                people_svc.add_contact_point(sess, p, cp.kind, cp.value, primary=True)
                ok("Primary updated.")
            elif action == "delete":
                people_svc.remove_contact_point(sess, p, cp)
                ok("Removed.")
            elif action == "status":
                cp.status = request.form.get("status") if request.form.get("status") in (
                    "active", "unverified", "bounced", "invalid") else cp.status
                ok("Status updated.")
        sess.commit()
    except ValueError as e:
        sess.rollback()
        err(str(e))
    return redirect(url_for("people.detail", person_id=person_id) + "#identities")


@bp.route("/people/<int:person_id>/consent", methods=["POST"])
def consent(person_id):
    sess = db()
    p = get_or_404(Person, person_id)
    channel, status = request.form.get("channel"), request.form.get("status")
    if channel not in ("email", "whatsapp", "sms") or status not in ("granted", "revoked"):
        abort(400)
    people_svc.set_consent(sess, p, channel, status, (request.form.get("source") or "Set in panel").strip(), actor())
    if channel == "email" and status == "revoked":
        from app.services import cancel_queued
        for e in p.enrollments:
            e.unsubscribed = True
        cancel_queued(sess, [e.id for e in p.enrollments])
    sess.commit()
    ok(f"{channel.capitalize()} consent: {status}.")
    return redirect(url_for("people.detail", person_id=person_id))


@bp.route("/people/<int:person_id>/log", methods=["POST"])
def log_activity(person_id):
    sess = db()
    p = get_or_404(Person, person_id)
    from app import settings as app_settings
    try:
        when = app_settings.parse_local_input(request.form.get("occurred_at"), settings_now())
        activities_svc.log(sess, kind=request.form.get("kind") or "note", subject=request.form.get("subject"),
                           body=request.form.get("body"), person_id=p.id, user_id=actor().user_id,
                           occurred_at=when, duration_minutes=int_arg("duration", None, request.form),
                           outcome=request.form.get("outcome"), actor=actor())
        sess.commit()
        ok("Logged.")
    except ValueError as e:
        sess.rollback()
        err(str(e))
    return redirect(url_for("people.detail", person_id=person_id) + "#timeline")


@bp.route("/people/<int:person_id>/enroll", methods=["POST"])
def enroll_one(person_id):
    sess = db()
    p = get_or_404(Person, person_id)
    camp = get_or_404(Campaign, int_arg("campaign_id", 0, request.form) or 0)
    try:
        e, created = enroll(sess, camp, p, email=request.form.get("email") or None, actor=actor())
        sess.commit()
        if created:
            ok(f"Enrolled in '{camp.name}'.")
        else:
            warn(f"Already in '{camp.name}'.")
        return redirect(url_for("enrollments.detail", enrollment_id=e.id))
    except EnrollError as ex:
        sess.rollback()
        err(str(ex))
        return redirect(url_for("people.detail", person_id=person_id))


@bp.route("/people/<int:person_id>/message", methods=["POST"])
def message(person_id):
    """Send a one-to-one email / WhatsApp from the person page."""
    sess = db()
    p = get_or_404(Person, person_id)
    channel = request.form.get("channel") or "email"
    template = (request.form.get("wa_template") or "").strip() or None
    params = [x.strip() for x in (request.form.get("wa_params") or "").split("|") if x.strip()]
    try:
        m = conv_svc.send(sess, p, channel, request.form.get("body"), subject=request.form.get("subject"),
                          user=current_user(), actor=actor(), template=template,
                          template_language=(request.form.get("wa_language") or "en_US").strip(),
                          template_params=params, settings=settings_now())
        sess.commit()
        ok(f"{channel.capitalize()} sent.")
        return redirect(url_for("inbox.index", c=m.conversation_id))
    except conv_svc.SendBlocked as e:
        sess.rollback()
        err(str(e))
    except (mailer.SendError, whatsapp.WhatsAppError) as e:
        sess.commit()            # keep the failed attempt in the thread
        err(f"Not sent: {e}")
    return redirect(url_for("people.detail", person_id=person_id) + "#compose")


@bp.route("/people/<int:person_id>/merge", methods=["GET", "POST"])
def merge(person_id):
    sess = db()
    keep = get_or_404(Person, person_id)
    other = sess.get(Person, int_arg("other", 0) or int_arg("other", 0, request.form) or 0)
    if request.method == "POST":
        if other is None:
            abort(400)
        if request.form.get("swap") == "1":
            keep, other = other, keep
        people_svc.merge(sess, keep, other, actor())
        sess.commit()
        ok("Merged. Identities, campaigns, history, deals and tags now live on this record.")
        return redirect(url_for("people.detail", person_id=keep.id))
    candidates = []
    if other is None:
        conds = []
        if keep.email:
            conds.append(Person.email.like("%@" + keep.email.split("@")[1]))
        if keep.last_name:
            conds.append(func.lower(Person.last_name) == keep.last_name.lower())
        if keep.phone:
            conds.append(Person.phone == keep.phone)
        if keep.company_id:
            conds.append(Person.company_id == keep.company_id)
        if conds:
            candidates = (sess.query(Person).filter(Person.id != keep.id, or_(*conds)).limit(30).all())
    return render_template("people/merge.html", keep=keep, other=other, candidates=candidates)


@bp.route("/people/lookup.json")
def lookup():
    """Typeahead for pickers (deal contacts, task links)."""
    from flask import jsonify
    sess = db()
    q = people_svc.search_query(sess, request.args.get("q")).limit(15).all()
    return jsonify([{"id": p.id, "label": p.display_name, "email": p.email,
                     "company": p.company.name if p.company else None} for p in q])
