"""REST API v1. Authenticate with `Authorization: Bearer <api key>` (Settings > API keys).

Scopes: read (GET), write (create / update / delete), send (email / WhatsApp people).
Errors are JSON: {"error": "...", "message": "..."}.
"""
from functools import wraps

from flask import Blueprint, abort, g, jsonify, request

from app.crm import actions, apikeys
from app.views.common import db

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _key_from_request():
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def endpoint(scope="read"):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            sess = db()
            key = apikeys.verify(sess, _key_from_request())
            if key is None:
                abort(401, "Missing, invalid, expired or revoked API key.")
            g.api_key = key
            g.api_actor = apikeys.actor_for(key, "api")
            try:
                actions.require(key.scopes, scope)
                result = fn(sess, *args, **kwargs)
                if request.method != "GET" or scope != "read":
                    sess.commit()
                return jsonify(result) if not isinstance(result, tuple) else (jsonify(result[0]), result[1])
            except PermissionError as e:
                sess.rollback()
                return jsonify(error="forbidden", message=str(e)), 403
            except actions.NotFound as e:
                sess.rollback()
                return jsonify(error="not_found", message=str(e)), 404
            except ValueError as e:
                sess.rollback()
                return jsonify(error="invalid", message=str(e)), 400
        return wrapper
    return deco


def body():
    data = request.get_json(silent=True)
    if data is None and request.data:
        abort(400, "Body must be JSON.")
    return data or {}


def arg(name, default=None):
    return request.args.get(name, default)


# ------------------------------------------------------------------ meta

@bp.route("/me")
@endpoint()
def me(sess):
    k = g.api_key
    return {"key": k.name, "prefix": k.prefix, "scopes": k.scopes}


@bp.route("/overview")
@endpoint()
def overview(sess):
    return actions.overview(sess)


@bp.route("/search")
@endpoint()
def search(sess):
    return actions.search(sess, arg("q"), arg("limit", 10))


# ------------------------------------------------------------------ people

@bp.route("/people", methods=["GET"])
@endpoint()
def people_list(sess):
    return actions.list_people(sess, arg("q"), arg("lifecycle_stage"), arg("tag"), arg("company_id"),
                               arg("campaign_id"), arg("owner_id"), arg("limit", 25), arg("offset", 0))


@bp.route("/people", methods=["POST"])
@endpoint("write")
def people_create(sess):
    return actions.create_person(sess, g.api_actor, body()), 201


@bp.route("/people/<int:pid>", methods=["GET"])
@endpoint()
def people_get(sess, pid):
    return actions.get_person(sess, pid, timeline_limit=arg("timeline", 25))


@bp.route("/people/<int:pid>", methods=["PATCH"])
@endpoint("write")
def people_update(sess, pid):
    return actions.update_person(sess, g.api_actor, pid, body())


@bp.route("/people/<int:pid>", methods=["DELETE"])
@endpoint("write")
def people_delete(sess, pid):
    return actions.delete_person(sess, g.api_actor, pid)


@bp.route("/people/<int:pid>/merge", methods=["POST"])
@endpoint("write")
def people_merge(sess, pid):
    return actions.merge_people(sess, g.api_actor, pid, body().get("other_id"))


@bp.route("/people/<int:pid>/messages", methods=["POST"])
@endpoint("send")
def people_message(sess, pid):
    b = body()
    return actions.send_message(sess, g.api_actor, pid, b.get("channel", "email"), b.get("body"), b.get("subject"),
                                b.get("conversation_id"), b.get("template"), b.get("template_language", "en_US"),
                                b.get("template_params")), 201


# ------------------------------------------------------------------ companies

@bp.route("/companies", methods=["GET"])
@endpoint()
def companies_list(sess):
    return actions.list_companies(sess, arg("q"), arg("limit", 25), arg("offset", 0))


@bp.route("/companies", methods=["POST"])
@endpoint("write")
def companies_create(sess):
    return actions.create_company(sess, g.api_actor, body()), 201


@bp.route("/companies/<int:cid>", methods=["GET"])
@endpoint()
def companies_get(sess, cid):
    return actions.get_company(sess, cid)


@bp.route("/companies/<int:cid>", methods=["PATCH"])
@endpoint("write")
def companies_update(sess, cid):
    return actions.update_company(sess, g.api_actor, cid, body())


# ------------------------------------------------------------------ deals

@bp.route("/pipelines")
@endpoint()
def pipelines(sess):
    return {"pipelines": actions.list_pipelines(sess)}


@bp.route("/deals", methods=["GET"])
@endpoint()
def deals_list(sess):
    return actions.list_deals(sess, arg("status"), arg("pipeline_id"), arg("person_id"), arg("company_id"), arg("q"),
                              arg("limit", 25), arg("offset", 0))


@bp.route("/deals", methods=["POST"])
@endpoint("write")
def deals_create(sess):
    return actions.create_deal(sess, g.api_actor, body()), 201


@bp.route("/deals/<int:did>", methods=["PATCH"])
@endpoint("write")
def deals_update(sess, did):
    return actions.update_deal(sess, g.api_actor, did, body())


# ------------------------------------------------------------------ activities & tasks

@bp.route("/activities", methods=["POST"])
@endpoint("write")
def activities_create(sess):
    b = body()
    return actions.log_activity(sess, g.api_actor, b.get("kind", "note"), b.get("body"), b.get("subject"),
                                b.get("person_id"), b.get("company_id"), b.get("deal_id"), b.get("occurred_at")), 201


@bp.route("/tasks", methods=["GET"])
@endpoint()
def tasks_list(sess):
    return actions.list_tasks(sess, arg("view", "open"), arg("person_id"), arg("company_id"), arg("deal_id"),
                              arg("limit", 50))


@bp.route("/tasks", methods=["POST"])
@endpoint("write")
def tasks_create(sess):
    b = body()
    return actions.create_task(sess, g.api_actor, b.get("title"), b.get("due_at"), b.get("priority", "normal"),
                               b.get("notes"), b.get("person_id"), b.get("company_id"), b.get("deal_id"),
                               b.get("assignee_id")), 201


@bp.route("/tasks/<int:tid>", methods=["PATCH"])
@endpoint("write")
def tasks_update(sess, tid):
    return actions.update_task(sess, g.api_actor, tid, body())


# ------------------------------------------------------------------ campaigns

@bp.route("/campaigns")
@endpoint()
def campaigns_list(sess):
    return {"campaigns": actions.list_campaigns(sess, arg("archived") == "1")}


@bp.route("/campaigns/<int:cid>")
@endpoint()
def campaigns_get(sess, cid):
    return actions.get_campaign(sess, cid)


@bp.route("/campaigns/import", methods=["POST"])
@endpoint("write")
def campaigns_import(sess):
    b = body()
    return actions.import_campaign(sess, g.api_actor, b.get("name"), b.get("csv"), b.get("mapping"),
                                   bool(b.get("dry_run"))), 201


@bp.route("/campaigns/<int:cid>/enrollments", methods=["POST"])
@endpoint("write")
def campaigns_enroll(sess, cid):
    b = body()
    return actions.enroll_in_campaign(sess, g.api_actor, cid, b.get("person_ids"), b.get("emails"))


@bp.route("/campaigns/<int:cid>/status", methods=["POST"])
@endpoint("send")
def campaigns_status(sess, cid):
    return actions.set_campaign_state(sess, g.api_actor, cid, body().get("status"))


# ------------------------------------------------------------------ conversations

@bp.route("/conversations")
@endpoint()
def conversations_list(sess):
    return actions.list_conversations(sess, arg("status", "open"), arg("channel"), arg("person_id"), arg("limit", 25))


@bp.route("/conversations/<int:cid>")
@endpoint()
def conversations_get(sess, cid):
    return actions.get_conversation(sess, cid, arg("limit", 50))


@bp.route("/conversations/<int:cid>/messages", methods=["POST"])
@endpoint("send")
def conversations_reply(sess, cid):
    from app.models import Conversation
    conv = sess.get(Conversation, cid)
    if conv is None:
        raise actions.NotFound(f"Conversation {cid} not found.")
    b = body()
    return actions.send_message(sess, g.api_actor, conv.person_id, conv.channel, b.get("body"), b.get("subject"),
                                cid, b.get("template"), b.get("template_language", "en_US"),
                                b.get("template_params")), 201
