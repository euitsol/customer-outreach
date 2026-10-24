"""Workspace settings: users, API keys, integrations (email, WhatsApp, Claude/MCP), custom fields,
pipelines, audit log."""
from flask import Blueprint, abort, redirect, render_template, request, url_for
from sqlalchemy import func
from werkzeug.security import generate_password_hash

from app import config, mailer
from app.channels import status as channel_status, whatsapp
from app.crm import apikeys, audit, custom_fields as cf_svc, normalize
from app.models import (
    ApiKey, AuditLog, ChannelAccount, CustomField, Deal, Pipeline, PipelineStage, User, WebhookEvent,
)
from app.views.common import actor, current_user, db, err, form_bool, get_or_404, int_arg, ok, paginate, warn

bp = Blueprint("workspace", __name__)

ROLES = [("owner", "Owner"), ("admin", "Admin"), ("member", "Member")]


def _require_admin():
    u = current_user()
    if u is not None and u.role not in ("owner", "admin"):
        abort(403)


def public_base():
    return config.PUBLIC_BASE_URL or config.TRACK_BASE_URL or request.host_url.rstrip("/")


# ------------------------------------------------------------------ users

@bp.route("/settings/users", methods=["GET", "POST"])
def users():
    sess = db()
    if request.method == "POST":
        _require_admin()
        act = request.form.get("action")
        try:
            if act == "create":
                email = normalize.email(request.form.get("email"))
                password = request.form.get("password") or ""
                if not email:
                    raise ValueError("Enter a valid email address.")
                if sess.query(User).filter(func.lower(User.email) == email).first():
                    raise ValueError("A user with that email already exists.")
                if len(password) < 10:
                    raise ValueError("Use a password of at least 10 characters.")
                role = request.form.get("role") if request.form.get("role") in dict(ROLES) else "member"
                if not sess.query(User).first():
                    role = "owner"
                u = User(email=email, name=(request.form.get("name") or "").strip() or None, role=role,
                         password_hash=generate_password_hash(password), is_active=True)
                sess.add(u)
                sess.flush()
                audit.record(sess, actor(), "create", "user", u.id, {"email": email, "role": role})
                ok(f"User {email} added. They sign in with their email and password.")
            else:
                u = get_or_404(User, int_arg("user_id", 0, request.form))
                if act == "update":
                    u.name = (request.form.get("name") or "").strip() or None
                    if request.form.get("role") in dict(ROLES):
                        u.role = request.form["role"]
                    u.is_active = form_bool("is_active")
                    if not sess.query(User).filter(User.role == "owner", User.is_active == True).count():  # noqa: E712
                        raise ValueError("Keep at least one active owner.")
                    ok("User saved.")
                elif act == "password":
                    password = request.form.get("password") or ""
                    if len(password) < 10:
                        raise ValueError("Use a password of at least 10 characters.")
                    u.password_hash = generate_password_hash(password)
                    ok("Password changed.")
                elif act == "delete":
                    if current_user() is not None and u.id == current_user().id:
                        raise ValueError("You can't delete yourself.")
                    sess.delete(u)
                    ok("User removed. Their records are kept (now unassigned).")
                audit.record(sess, actor(), act, "user", u.id, {"email": u.email})
            sess.commit()
        except ValueError as e:
            sess.rollback()
            err(str(e))
        return redirect(url_for("workspace.users"))
    return render_template("workspace/users.html", rows=sess.query(User).order_by(User.id).all(), roles=ROLES,
                           env_admin=bool(config.ADMIN_PASSWORD), env_user=config.ADMIN_USER)


# ------------------------------------------------------------------ API keys

@bp.route("/settings/api-keys", methods=["GET", "POST"])
def api_keys():
    sess = db()
    new_key = None
    if request.method == "POST":
        _require_admin()
        act = request.form.get("action")
        if act == "create":
            scopes = [s for s in apikeys.SCOPES if request.form.get(f"scope_{s}") == "1"]
            new_key, row = apikeys.create(sess, request.form.get("name"), scopes,
                                          current_user().id if current_user() else None,
                                          int_arg("expires_days", None, request.form), actor())
            sess.commit()
            ok("Key created. Copy it now: it is shown only once.")
        elif act == "revoke":
            key = get_or_404(ApiKey, int_arg("key_id", 0, request.form))
            apikeys.revoke(sess, key, actor())
            sess.commit()
            ok("Key revoked.")
            return redirect(url_for("workspace.api_keys"))
    return render_template("workspace/api_keys.html", rows=sess.query(ApiKey).order_by(ApiKey.id.desc()).all(),
                           new_key=new_key, scopes=apikeys.SCOPES, base=public_base(),
                           mcp_port=config.MCP_PORT)


# ------------------------------------------------------------------ integrations

@bp.route("/settings/integrations", methods=["GET", "POST"])
def integrations():
    sess = db()
    if request.method == "POST":
        act = request.form.get("action")
        if act == "wa_test":
            to = normalize.whatsapp_id(request.form.get("to"))
            if not to:
                err("Enter a phone number in international format, e.g. +353 87 123 4567.")
            else:
                try:
                    name = (request.form.get("template") or "hello_world").strip()
                    wamid = whatsapp.send_template(to, name, (request.form.get("language") or "en_US").strip())
                    ok(f"WhatsApp template '{name}' sent (id {wamid[:24]}...).")
                except whatsapp.WhatsAppError as e:
                    err(f"WhatsApp test failed: {e}")
        elif act == "account":
            acct = get_or_404(ChannelAccount, int_arg("account_id", 0, request.form))
            acct.name = (request.form.get("name") or acct.name).strip()[:200]
            acct.status = request.form.get("status") if request.form.get("status") in ("active", "disabled") else acct.status
            sess.commit()
            ok("Channel saved.")
        return redirect(url_for("workspace.integrations"))
    recent_hooks = sess.query(WebhookEvent).order_by(WebhookEvent.id.desc()).limit(15).all()
    return render_template("workspace/integrations.html", channels=channel_status(sess),
                           accounts=sess.query(ChannelAccount).order_by(ChannelAccount.kind, ChannelAccount.id).all(),
                           base=public_base(), config=config, smtp_ok=mailer.smtp_configured(),
                           wa_ok=whatsapp.configured(), recent_hooks=recent_hooks, mcp_port=config.MCP_PORT,
                           keys=sess.query(func.count(ApiKey.id)).filter(ApiKey.revoked_at.is_(None)).scalar())


# ------------------------------------------------------------------ custom fields

@bp.route("/settings/fields", methods=["GET", "POST"])
def fields():
    sess = db()
    if request.method == "POST":
        _require_admin()
        act = request.form.get("action")
        try:
            if act == "create":
                opts = [o for o in (request.form.get("options") or "").replace("\r", "").split("\n")]
                cf_svc.create(sess, request.form.get("entity"), request.form.get("label") or "",
                              request.form.get("field_type") or "text", opts, form_bool("is_required"), actor())
                ok("Field added.")
            else:
                f = get_or_404(CustomField, int_arg("field_id", 0, request.form))
                if act == "update":
                    f.label = (request.form.get("label") or f.label).strip()[:200]
                    f.is_required = form_bool("is_required")
                    if f.field_type in ("select", "multiselect"):
                        f.options = [o.strip() for o in (request.form.get("options") or "").split("\n") if o.strip()]
                    ok("Field saved.")
                elif act == "delete":
                    sess.delete(f)
                    ok("Field removed. Stored values stay on the records but are no longer shown.")
                elif act in ("up", "down"):
                    defs = cf_svc.definitions(sess, f.entity)
                    i = defs.index(f)
                    j = i - 1 if act == "up" else i + 1
                    if 0 <= j < len(defs):
                        defs[i], defs[j] = defs[j], defs[i]
                        for pos, d in enumerate(defs):
                            d.position = pos
            sess.commit()
        except ValueError as e:
            sess.rollback()
            err(str(e))
        return redirect(url_for("workspace.fields"))
    groups = [(k, label, cf_svc.definitions(sess, k)) for k, label in cf_svc.ENTITIES]
    return render_template("workspace/fields.html", groups=groups, types=cf_svc.FIELD_TYPES)


# ------------------------------------------------------------------ pipelines

@bp.route("/settings/pipelines", methods=["GET", "POST"])
def pipelines():
    sess = db()
    if request.method == "POST":
        _require_admin()
        act = request.form.get("action")
        try:
            if act == "create_pipeline":
                name = (request.form.get("name") or "").strip()
                if not name:
                    raise ValueError("Name the pipeline.")
                p = Pipeline(name=name[:200], position=sess.query(Pipeline).count(),
                             is_default=not sess.query(Pipeline).first())
                sess.add(p)
                sess.flush()
                for i, (n, prob, kind) in enumerate([("New", 10, "open"), ("Won", 100, "won"), ("Lost", 0, "lost")], 1):
                    sess.add(PipelineStage(pipeline_id=p.id, name=n, position=i, probability=prob, kind=kind))
                ok("Pipeline created.")
            elif act == "pipeline":
                p = get_or_404(Pipeline, int_arg("pipeline_id", 0, request.form))
                p.name = (request.form.get("name") or p.name).strip()[:200]
                if form_bool("is_default"):
                    for other in sess.query(Pipeline):
                        other.is_default = other.id == p.id
                ok("Pipeline saved.")
            elif act == "delete_pipeline":
                p = get_or_404(Pipeline, int_arg("pipeline_id", 0, request.form))
                if sess.query(Deal).filter(Deal.pipeline_id == p.id).count():
                    raise ValueError("Move or delete this pipeline's deals first.")
                if sess.query(Pipeline).count() == 1:
                    raise ValueError("Keep at least one pipeline.")
                sess.delete(p)
                ok("Pipeline deleted.")
            elif act == "add_stage":
                p = get_or_404(Pipeline, int_arg("pipeline_id", 0, request.form))
                name = (request.form.get("name") or "").strip()
                if not name:
                    raise ValueError("Name the stage.")
                opens = [s for s in p.stages if s.kind == "open"]
                pos = (max(s.position for s in opens) + 1) if opens else 1
                for s in p.stages:
                    if s.position >= pos:
                        s.position += 1
                sess.add(PipelineStage(pipeline_id=p.id, name=name[:200], position=pos,
                                       probability=max(0, min(100, int_arg("probability", 50, request.form))),
                                       kind="open"))
                ok("Stage added.")
            elif act in ("stage", "stage_up", "stage_down", "delete_stage"):
                st = get_or_404(PipelineStage, int_arg("stage_id", 0, request.form))
                stages = list(st.pipeline.stages)
                if act == "stage":
                    st.name = (request.form.get("name") or st.name).strip()[:200]
                    st.probability = max(0, min(100, int_arg("probability", st.probability, request.form)))
                    ok("Stage saved.")
                elif act == "delete_stage":
                    if sess.query(Deal).filter(Deal.stage_id == st.id).count():
                        raise ValueError("Deals are in this stage; move them first.")
                    if st.kind != "open" and len([s for s in stages if s.kind == st.kind]) == 1:
                        raise ValueError(f"A pipeline needs a {st.kind} stage.")
                    sess.delete(st)
                    ok("Stage removed.")
                else:
                    i = stages.index(st)
                    j = i - 1 if act == "stage_up" else i + 1
                    if 0 <= j < len(stages):
                        stages[i].position, stages[j].position = stages[j].position, stages[i].position
            sess.commit()
        except ValueError as e:
            sess.rollback()
            err(str(e))
        return redirect(url_for("workspace.pipelines"))
    counts = dict(sess.query(Deal.stage_id, func.count(Deal.id)).group_by(Deal.stage_id).all())
    return render_template("workspace/pipelines.html",
                           pipelines=sess.query(Pipeline).order_by(Pipeline.position, Pipeline.id).all(), counts=counts)


# ------------------------------------------------------------------ audit log

@bp.route("/settings/audit")
def audit_log():
    sess = db()
    q = sess.query(AuditLog)
    for key, col in (("entity", AuditLog.entity_type), ("actor_type", AuditLog.actor_type),
                     ("action", AuditLog.action)):
        if request.args.get(key):
            q = q.filter(col == request.args[key])
    if request.args.get("entity_id"):
        q = q.filter(AuditLog.entity_id == request.args["entity_id"])
    page = paginate(q.order_by(AuditLog.id.desc()), 50)
    entities = [e for (e,) in sess.query(AuditLog.entity_type).distinct().order_by(AuditLog.entity_type)]
    return render_template("workspace/audit.html", page=page, entities=entities)


@bp.route("/settings/me", methods=["GET", "POST"])
def me():
    sess = db()
    u = current_user()
    if u is None:
        warn("You are signed in with the administrator account from the server's .env file. "
             "Create a personal user under Settings > Users.")
        return redirect(url_for("workspace.users"))
    if request.method == "POST":
        u.name = (request.form.get("name") or "").strip() or None
        pw = request.form.get("password") or ""
        if pw:
            if len(pw) < 10:
                err("Use a password of at least 10 characters.")
                return redirect(url_for("workspace.me"))
            u.password_hash = generate_password_hash(pw)
        sess.commit()
        ok("Profile saved.")
        return redirect(url_for("workspace.me"))
    return render_template("workspace/me.html", u=u)
