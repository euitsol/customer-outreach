"""Deals: pipeline board, list, detail."""
from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app import config
from app.clock import utcnow
from app.crm import activities as activities_svc, custom_fields as cf_svc, deals as deals_svc
from app.models import Company, Deal, Person, Pipeline, PipelineStage, User, deal_people
from app.views.common import actor, current_user, db, err, form_date, get_or_404, int_arg, ok, paginate

bp = Blueprint("deals", __name__)


def _users(sess):
    return sess.query(User).filter(User.is_active == True).order_by(User.name, User.email).all()  # noqa: E712


def _pipeline(sess):
    pid = int_arg("pipeline")
    p = sess.get(Pipeline, pid) if pid else None
    return p or deals_svc.default_pipeline(sess)


@bp.route("/deals")
def index():
    sess = db()
    pipeline = _pipeline(sess)
    if pipeline is None:
        return render_template("deals/index.html", pipeline=None, pipelines=[], columns=[], summary=None,
                               view="board", users=[])
    view = request.args.get("view", "board")
    filters = {"owner_id": int_arg("owner"), "q": (request.args.get("q") or "").strip()}
    ctx = dict(pipeline=pipeline, pipelines=sess.query(Pipeline).order_by(Pipeline.position, Pipeline.id).all(),
               summary=deals_svc.summary(sess, pipeline), view=view, users=_users(sess), filters=filters,
               currency=config.DEFAULT_CURRENCY)
    if view == "list":
        q = (sess.query(Deal).filter(Deal.pipeline_id == pipeline.id)
             .options(selectinload(Deal.company), selectinload(Deal.person), selectinload(Deal.stage),
                      selectinload(Deal.owner)))
        status = request.args.get("status")
        if status in ("open", "won", "lost"):
            q = q.filter(Deal.status == status)
        if filters["owner_id"]:
            q = q.filter(Deal.owner_id == filters["owner_id"])
        if filters["q"]:
            q = q.filter(func.lower(Deal.title).like(f"%{filters['q'].lower()}%"))
        sort = request.args.get("sort", "updated")
        order = {"amount": (Deal.amount.desc().nullslast(),), "close": (Deal.expected_close_date.asc().nullslast(),),
                 "title": (func.lower(Deal.title),)}.get(sort, (Deal.updated_at.desc().nullslast(), Deal.id.desc()))
        ctx["page"] = paginate(q.order_by(*order))
    else:
        ctx["columns"] = deals_svc.board(sess, pipeline, filters)
    return render_template("deals/index.html", **ctx)


@bp.route("/deals/new", methods=["GET", "POST"])
def new():
    sess = db()
    f = request.form if request.method == "POST" else request.args
    pipeline = sess.get(Pipeline, int_arg("pipeline_id", 0, f) or 0) or deals_svc.default_pipeline(sess)
    if request.method == "POST":
        try:
            person = sess.get(Person, int_arg("person_id", 0, f) or 0)
            company = sess.get(Company, int_arg("company_id", 0, f) or 0) or (person.company if person else None)
            deal = deals_svc.create(
                sess, title=f.get("title"), pipeline_id=pipeline.id if pipeline else None,
                stage_id=int_arg("stage_id", None, f), company_id=company.id if company else None,
                person_id=person.id if person else None,
                owner_id=int_arg("owner_id", None, f) or (current_user().id if current_user() else None),
                amount=f.get("amount"), currency=f.get("currency"), expected_close_date=form_date("expected_close_date"),
                source=(f.get("source") or "").strip() or None,
                attributes=cf_svc.values_from_form(sess, "deal", f), actor=actor())
            sess.commit()
            ok("Deal created.")
            return redirect(url_for("deals.detail", deal_id=deal.id))
        except ValueError as e:
            sess.rollback()
            err(str(e))
    return render_template("deals/new.html", form=f, pipeline=pipeline,
                           pipelines=sess.query(Pipeline).order_by(Pipeline.position).all(),
                           person=sess.get(Person, int_arg("person_id", 0, f) or 0),
                           company=sess.get(Company, int_arg("company_id", 0, f) or 0),
                           fields=cf_svc.definitions(sess, "deal"), users=_users(sess),
                           currency=config.DEFAULT_CURRENCY)


@bp.route("/deals/<int:deal_id>")
def detail(deal_id):
    sess = db()
    d = get_or_404(Deal, deal_id)
    roles = {pid: role for pid, role in sess.query(deal_people.c.person_id, deal_people.c.role)
             .filter(deal_people.c.deal_id == d.id)}
    return render_template("deals/detail.html", d=d, roles=roles, activities=activities_svc.deal_activities(sess, d),
                           tasks=activities_svc.task_query(sess, "open", deal_id=d.id).all(),
                           fields=cf_svc.definitions(sess, "deal"), users=_users(sess),
                           activity_kinds=activities_svc.ACTIVITY_KINDS, now=utcnow(),
                           pipelines=sess.query(Pipeline).order_by(Pipeline.position).all())


@bp.route("/deals/<int:deal_id>/edit", methods=["POST"])
def edit(deal_id):
    sess = db()
    d = get_or_404(Deal, deal_id)
    f = request.form
    try:
        data = {k: f.get(k) for k in ("title", "amount", "currency", "source", "lost_reason") if k in f}
        if "expected_close_date" in f:
            data["expected_close_date"] = form_date("expected_close_date")
        for k in ("owner_id", "company_id", "person_id", "stage_id"):
            if k in f:
                data[k] = int_arg(k, None, f)
        data["attributes"] = cf_svc.values_from_form(sess, "deal", f)
        deals_svc.update(sess, d, data, actor())
        sess.commit()
        ok("Saved.")
    except ValueError as e:
        sess.rollback()
        err(str(e))
    return redirect(url_for("deals.detail", deal_id=deal_id))


@bp.route("/deals/<int:deal_id>/move", methods=["POST"])
def move(deal_id):
    """Board drag & drop (JSON) and the stage buttons on the deal page (form)."""
    sess = db()
    d = get_or_404(Deal, deal_id)
    stage = get_or_404(PipelineStage, int_arg("stage_id", 0, request.form))
    deals_svc.move(sess, d, stage, request.form.get("lost_reason"), actor())
    sess.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, status=d.status, stage=stage.name)
    ok(f"Moved to {stage.name}.")
    return redirect(request.form.get("next") or url_for("deals.detail", deal_id=deal_id))


@bp.route("/deals/<int:deal_id>/delete", methods=["POST"])
def delete(deal_id):
    sess = db()
    d = get_or_404(Deal, deal_id)
    pid = d.pipeline_id
    deals_svc.delete(sess, d, actor())
    sess.commit()
    ok("Deal deleted.")
    return redirect(url_for("deals.index", pipeline=pid))


@bp.route("/deals/<int:deal_id>/people", methods=["POST"])
def people(deal_id):
    sess = db()
    d = get_or_404(Deal, deal_id)
    pid = int_arg("person_id", 0, request.form)
    person = get_or_404(Person, pid)
    if request.form.get("action") == "remove":
        sess.execute(deal_people.delete().where(deal_people.c.deal_id == d.id, deal_people.c.person_id == pid))
        if d.person_id == pid:
            d.person_id = None
    else:
        role = (request.form.get("role") or "").strip()[:50] or None
        if not sess.query(deal_people).filter_by(deal_id=d.id, person_id=pid).first():
            sess.execute(deal_people.insert().values(deal_id=d.id, person_id=pid, role=role))
        else:
            sess.execute(deal_people.update().where(deal_people.c.deal_id == d.id, deal_people.c.person_id == pid)
                         .values(role=role))
        if not d.person_id:
            d.person_id = person.id
    sess.commit()
    ok("Updated.")
    return redirect(url_for("deals.detail", deal_id=deal_id))


@bp.route("/deals/<int:deal_id>/log", methods=["POST"])
def log_activity(deal_id):
    sess = db()
    d = get_or_404(Deal, deal_id)
    try:
        activities_svc.log(sess, kind=request.form.get("kind") or "note", subject=request.form.get("subject"),
                           body=request.form.get("body"), deal_id=d.id, person_id=d.person_id,
                           user_id=actor().user_id, actor=actor())
        sess.commit()
        ok("Logged.")
    except ValueError as e:
        sess.rollback()
        err(str(e))
    return redirect(url_for("deals.detail", deal_id=deal_id))


@bp.route("/deals/stages.json")
def stages_json():
    sess = db()
    p = sess.get(Pipeline, int_arg("pipeline", 0) or 0)
    if p is None:
        abort(404)
    return jsonify([{"id": s.id, "name": s.name, "kind": s.kind} for s in p.stages])
