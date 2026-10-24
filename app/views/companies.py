"""Companies (accounts)."""
from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import selectinload

from app.clock import utcnow
from app.crm import (
    activities as activities_svc, companies as companies_svc, custom_fields as cf_svc, people as people_svc,
)
from app.models import Company, Deal, Person, Tag, User, company_tags
from app.views.common import actor, db, err, form_ids, get_or_404, int_arg, ok, paginate, warn

bp = Blueprint("companies", __name__)

SORTS = {
    "name": (func.lower(Company.name), Company.id),
    "recent": (Company.id.desc(),),
    "people": (func.count(Person.id).desc(), Company.id),
}


def _users(sess):
    return sess.query(User).filter(User.is_active == True).order_by(User.name, User.email).all()  # noqa: E712


@bp.route("/companies")
def index():
    sess = db()
    term = (request.args.get("q") or "").strip().lower()
    q = (sess.query(Company, func.count(Person.id).label("n"))
         .outerjoin(Person, Person.company_id == Company.id).group_by(Company.id)
         .options(selectinload(Company.tags), selectinload(Company.owner)))
    if term:
        like = f"%{term}%"
        q = q.filter(or_(func.lower(Company.name).like(like), func.lower(func.coalesce(Company.domain, "")).like(like),
                         func.lower(func.coalesce(Company.city, "")).like(like),
                         func.lower(func.coalesce(Company.industry, "")).like(like)))
    if request.args.get("industry"):
        q = q.filter(Company.industry == request.args["industry"])
    tag = (request.args.get("tag") or "").strip().lower()
    if tag:
        q = q.filter(exists().where(and_(company_tags.c.company_id == Company.id, company_tags.c.tag_id == Tag.id,
                                         Tag.name == tag)))
    owner = request.args.get("owner")
    if owner and owner.isdigit():
        q = q.filter(Company.owner_id == int(owner))
    sort = request.args.get("sort") or "name"
    q = q.order_by(*SORTS.get(sort, SORTS["name"]))
    page = paginate(q)
    ids = [c.id for c, _ in page.items]
    open_deals = {}
    if ids:
        for cid, n, total in (sess.query(Deal.company_id, func.count(Deal.id), func.coalesce(func.sum(Deal.amount), 0))
                              .filter(Deal.company_id.in_(ids), Deal.status == "open").group_by(Deal.company_id)):
            open_deals[cid] = (n, total)
    industries = [i for (i,) in sess.query(Company.industry).filter(Company.industry.isnot(None))
                  .distinct().order_by(Company.industry)]
    return render_template("companies/index.html", page=page, open_deals=open_deals, sort=sort, industries=industries,
                           users=_users(sess), all_tags=[t for (t,) in sess.query(Tag.name).order_by(Tag.name)])


@bp.route("/companies/new", methods=["GET", "POST"])
def new():
    sess = db()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            err("Company name is required.")
        else:
            try:
                company, created = companies_svc.get_or_create(sess, name, request.form.get("website"), actor=actor())
                if not created:
                    warn("That company already exists; opened it.")
                    return redirect(url_for("companies.detail", company_id=company.id))
                data = {k: request.form.get(k) for k in companies_svc.FIELDS if k in request.form and k != "name"}
                data["owner_id"] = int_arg("owner_id", None, request.form)
                data["attributes"] = cf_svc.values_from_form(sess, "company", request.form)
                companies_svc.update(sess, company, data, actor())
                people_svc.add_tags(sess, company, request.form.get("tags", ""))
                sess.commit()
                ok("Company added.")
                return redirect(url_for("companies.detail", company_id=company.id))
            except ValueError as e:
                sess.rollback()
                err(str(e))
    return render_template("companies/new.html", form=request.form, fields=cf_svc.definitions(sess, "company"),
                           users=_users(sess), sizes=companies_svc.SIZES)


@bp.route("/companies/<int:company_id>")
def detail(company_id):
    sess = db()
    c = get_or_404(Company, company_id)
    people = (sess.query(Person).filter(Person.company_id == c.id).options(selectinload(Person.tags))
              .order_by(Person.first_name, Person.last_name).all())
    deals = sess.query(Deal).filter(Deal.company_id == c.id).order_by(Deal.status, Deal.id.desc()).all()
    return render_template(
        "companies/detail.html", c=c, people=people, deals=deals,
        activities=activities_svc.company_activities(sess, c),
        tasks=activities_svc.task_query(sess, "open", company_id=c.id).all(),
        fields=cf_svc.definitions(sess, "company"), users=_users(sess), sizes=companies_svc.SIZES,
        activity_kinds=activities_svc.ACTIVITY_KINDS, tags=", ".join(t.name for t in c.tags), now=utcnow())


@bp.route("/companies/<int:company_id>/edit", methods=["POST"])
def edit(company_id):
    sess = db()
    c = get_or_404(Company, company_id)
    try:
        data = {k: request.form.get(k) for k in companies_svc.FIELDS if k in request.form}
        if "owner_id" in request.form:
            data["owner_id"] = int_arg("owner_id", None, request.form)
        data["attributes"] = cf_svc.values_from_form(sess, "company", request.form)
        companies_svc.update(sess, c, data, actor())
        if "tags" in request.form:
            people_svc.set_tags(sess, c, request.form.get("tags", ""))
        sess.commit()
        ok("Saved.")
    except ValueError as e:
        sess.rollback()
        err(str(e))
    return redirect(url_for("companies.detail", company_id=company_id))


@bp.route("/companies/<int:company_id>/delete", methods=["POST"])
def delete(company_id):
    sess = db()
    c = get_or_404(Company, company_id)
    name = c.name
    companies_svc.delete(sess, c, actor())
    sess.commit()
    ok(f"Deleted {name}. Its people were kept (now without a company).")
    return redirect(url_for("companies.index"))


@bp.route("/companies/<int:company_id>/log", methods=["POST"])
def log_activity(company_id):
    sess = db()
    c = get_or_404(Company, company_id)
    try:
        activities_svc.log(sess, kind=request.form.get("kind") or "note", subject=request.form.get("subject"),
                           body=request.form.get("body"), company_id=c.id, user_id=actor().user_id, actor=actor())
        sess.commit()
        ok("Logged.")
    except ValueError as e:
        sess.rollback()
        err(str(e))
    return redirect(url_for("companies.detail", company_id=company_id))


@bp.route("/companies/<int:company_id>/people", methods=["POST"])
def attach_people(company_id):
    """Attach existing people (by id) or detach them."""
    sess = db()
    c = get_or_404(Company, company_id)
    ids = form_ids("ids")
    action = request.form.get("action", "attach")
    if not ids and request.form.get("person_id"):
        ids = [int_arg("person_id", 0, request.form)]
    for p in sess.query(Person).filter(Person.id.in_(ids)):
        people_svc.update(sess, p, {"company_id": c.id if action == "attach" else None}, actor())
    sess.commit()
    ok("Updated.")
    return redirect(url_for("companies.detail", company_id=company_id))


@bp.route("/companies/lookup.json")
def lookup():
    sess = db()
    term = (request.args.get("q") or "").strip().lower()
    q = sess.query(Company)
    if term:
        q = q.filter(func.lower(Company.name).like(f"%{term}%"))
    return jsonify([{"id": c.id, "label": c.name, "domain": c.domain} for c in q.order_by(Company.name).limit(15)])


@bp.route("/companies/merge", methods=["POST"])
def merge():
    """Fold one company into another (people, deals, activities, tasks, tags)."""
    from app.crm import audit
    from app.models import Activity, Task
    sess = db()
    keep = get_or_404(Company, int_arg("keep", 0, request.form))
    other = get_or_404(Company, int_arg("other", 0, request.form))
    if keep.id == other.id:
        abort(400)
    for model in (Person, Deal, Activity, Task):
        sess.query(model).filter(model.company_id == other.id).update({model.company_id: keep.id},
                                                                      synchronize_session=False)
    for f in ("domain", "website", "industry", "size", "phone", "country", "city", "address", "description",
              "owner_id"):
        if not getattr(keep, f) and getattr(other, f):
            value = getattr(other, f)
            if f == "domain":
                other.domain = None
                sess.flush()
            setattr(keep, f, value)
    keep.attributes = {**(other.attributes or {}), **(keep.attributes or {})}
    for t in other.tags:
        if t not in keep.tags:
            keep.tags.append(t)
    other.tags = []
    audit.record(sess, actor(), "merge", "company", keep.id, {"merged_company_id": other.id, "name": other.name})
    sess.flush()
    sess.expire(other)
    sess.delete(other)
    sess.commit()
    ok(f"Merged '{other.name}' into '{keep.name}'.")
    return redirect(url_for("companies.detail", company_id=keep.id))
