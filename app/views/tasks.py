"""Follow-up tasks, plus deleting logged activities."""
from flask import Blueprint, redirect, render_template, request, url_for
from sqlalchemy.orm import selectinload

from app import settings as app_settings
from app.clock import utcnow
from app.crm import activities as activities_svc
from app.models import Activity, Task, User
from app.views.common import (
    actor, back, current_user, db, err, get_or_404, int_arg, ok, paginate, settings_now,
)

bp = Blueprint("tasks", __name__)

VIEWS = [("today", "Due today"), ("overdue", "Overdue"), ("open", "All open"), ("done", "Done")]


@bp.route("/tasks")
def index():
    sess = db()
    view = request.args.get("view", "open")
    if view not in dict(VIEWS):
        view = "open"
    me = current_user()
    who = request.args.get("who", "me" if me else "all")
    q = activities_svc.task_query(sess, view, assignee_id=me.id if (who == "me" and me) else None)
    q = q.options(selectinload(Task.person), selectinload(Task.company), selectinload(Task.deal),
                  selectinload(Task.assignee))
    now = utcnow()
    counts = {v: activities_svc.task_query(sess, v, assignee_id=me.id if (who == "me" and me) else None,
                                           now=now).count() for v, _ in VIEWS}
    return render_template("tasks/index.html", page=paginate(q), view=view, views=VIEWS, counts=counts, who=who,
                           now=now, users=sess.query(User).filter(User.is_active == True).all(),  # noqa: E712
                           s=settings_now(), due_input=app_settings.to_local_input)


@bp.route("/tasks/new", methods=["POST"])
def new():
    sess = db()
    f = request.form
    try:
        due = app_settings.parse_local_input(f.get("due_at"), settings_now())
        me = current_user()
        activities_svc.create_task(
            sess, title=f.get("title"), due_at=due, priority=f.get("priority") or "normal", notes=f.get("notes"),
            assignee_id=int_arg("assignee_id", None, f) or (me.id if me else None),
            person_id=int_arg("person_id", None, f), company_id=int_arg("company_id", None, f),
            deal_id=int_arg("deal_id", None, f), created_by_id=me.id if me else None, actor=actor())
        sess.commit()
        ok("Task added.")
    except ValueError as e:
        sess.rollback()
        err(str(e))
    return back("tasks.index")


@bp.route("/tasks/<int:task_id>/status", methods=["POST"])
def status(task_id):
    sess = db()
    t = get_or_404(Task, task_id)
    activities_svc.set_task_status(sess, t, request.form.get("status", "done"), actor())
    sess.commit()
    ok("Task completed." if t.status == "done" else "Task updated.")
    return back("tasks.index")


@bp.route("/tasks/<int:task_id>/edit", methods=["POST"])
def edit(task_id):
    sess = db()
    t = get_or_404(Task, task_id)
    f = request.form
    try:
        data = {k: f.get(k) for k in ("title", "notes", "priority") if k in f}
        if "due_at" in f:
            data["due_at"] = app_settings.parse_local_input(f.get("due_at"), settings_now())
        if "assignee_id" in f:
            data["assignee_id"] = int_arg("assignee_id", None, f)
        activities_svc.update_task(sess, t, data, actor())
        sess.commit()
        ok("Task saved.")
    except ValueError as e:
        sess.rollback()
        err(str(e))
    return back("tasks.index")


@bp.route("/activities/<int:activity_id>/delete", methods=["POST"])
def delete_activity(activity_id):
    sess = db()
    a = get_or_404(Activity, activity_id)
    activities_svc.delete_activity(sess, a, actor())
    sess.commit()
    ok("Removed from the timeline.")
    return back("dashboard.index")


@bp.route("/tasks/<int:task_id>")
def detail(task_id):
    t = get_or_404(Task, task_id)
    if t.person_id:
        return redirect(url_for("people.detail", person_id=t.person_id) + "#tasks")
    if t.deal_id:
        return redirect(url_for("deals.detail", deal_id=t.deal_id))
    if t.company_id:
        return redirect(url_for("companies.detail", company_id=t.company_id))
    return redirect(url_for("tasks.index"))
