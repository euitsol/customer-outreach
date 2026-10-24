"""Shared inbox: every email / WhatsApp thread with a person, in one place."""
from flask import Blueprint, redirect, render_template, request, url_for
from sqlalchemy.orm import selectinload

from app import mailer
from app.channels import whatsapp
from app.crm import conversations as conv_svc
from app.models import Conversation, ConversationMessage, Template, User
from app.views.common import actor, current_user, db, err, get_or_404, int_arg, ok, paginate, settings_now

bp = Blueprint("inbox", __name__)

FILTERS = [("open", "Open"), ("unread", "Unread"), ("pending", "Pending"), ("closed", "Closed"), ("all", "All")]


@bp.route("/inbox")
def index():
    sess = db()
    status = request.args.get("status", "open")
    channel = request.args.get("channel") or None
    me = current_user()
    mine = request.args.get("mine") == "1" and me is not None
    q = conv_svc.inbox_query(sess, status, channel, me.id if mine else None, (request.args.get("q") or "").strip())
    q = q.options(selectinload(Conversation.person), selectinload(Conversation.assignee))
    page = paginate(q, 50)
    current = sess.get(Conversation, int_arg("c", 0) or 0)
    if current is None and page.items:
        current = page.items[0]
    thread = []
    if current is not None:
        thread = (sess.query(ConversationMessage).filter(ConversationMessage.conversation_id == current.id)
                  .options(selectinload(ConversationMessage.sequence_message), selectinload(ConversationMessage.user))
                  .order_by(ConversationMessage.id).all())
        if current.unread_count:
            conv_svc.mark_read(current)
            sess.commit()
    last = {}
    ids = [c.id for c in page.items]
    if ids:
        sub = (sess.query(ConversationMessage.conversation_id, ConversationMessage.id)
               .filter(ConversationMessage.conversation_id.in_(ids)).order_by(ConversationMessage.id.desc()))
        seen = set()
        for cid, mid in sub:
            if cid not in seen:
                seen.add(cid)
                last[cid] = mid
        msgs = {m.id: m for m in sess.query(ConversationMessage).filter(ConversationMessage.id.in_(list(last.values())))}
        last = {cid: msgs.get(mid) for cid, mid in last.items()}
    return render_template(
        "inbox/index.html", page=page, current=current, thread=thread, last=last, status=status, channel=channel,
        filters=FILTERS, mine=mine, users=sess.query(User).filter(User.is_active == True).all(),  # noqa: E712
        wa_window=conv_svc.whatsapp_window_open(current) if current is not None and current.channel == "whatsapp" else None,
        channels={"email": mailer.smtp_configured(), "whatsapp": whatsapp.configured()},
        templates=sess.query(Template).order_by(Template.name).all())


@bp.route("/inbox/<int:conversation_id>/reply", methods=["POST"])
def reply(conversation_id):
    sess = db()
    conv = get_or_404(Conversation, conversation_id)
    template = (request.form.get("wa_template") or "").strip() or None
    params = [x.strip() for x in (request.form.get("wa_params") or "").split("|") if x.strip()]
    try:
        conv_svc.send(sess, conv.person, conv.channel, request.form.get("body"), subject=request.form.get("subject"),
                      conversation=conv, user=current_user(), actor=actor(), template=template,
                      template_language=(request.form.get("wa_language") or "en_US").strip(),
                      template_params=params, settings=settings_now())
        if request.form.get("close") == "1":
            conv_svc.set_status(sess, conv, "closed", actor())
        sess.commit()
        ok("Sent.")
    except conv_svc.SendBlocked as e:
        sess.rollback()
        err(str(e))
    except (mailer.SendError, whatsapp.WhatsAppError) as e:
        sess.commit()
        err(f"Not sent: {e}")
    return redirect(url_for("inbox.index", c=conversation_id, status=request.form.get("status") or "open"))


@bp.route("/inbox/<int:conversation_id>/status", methods=["POST"])
def set_status(conversation_id):
    sess = db()
    conv = get_or_404(Conversation, conversation_id)
    if "assignee_id" in request.form:
        conv.assignee_id = int_arg("assignee_id", None, request.form)
        ok("Assigned.")
    if request.form.get("status"):
        conv_svc.set_status(sess, conv, request.form["status"], actor())
        ok(f"Marked {request.form['status']}.")
    sess.commit()
    return redirect(url_for("inbox.index", c=conversation_id, status=request.args.get("back", "open")))
