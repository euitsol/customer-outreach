from flask import Blueprint, Response, redirect, render_template, request, url_for
from sqlalchemy import func

from app import importer
from app.models import Campaign
from app.services import create_default_steps, steps_of
from app.views.common import db, err, get_or_404, int_arg, ok, settings_now, warn

bp = Blueprint("imports", __name__)


CRM_ONLY = "crm"


def _campaign_from_form(sess, form):
    """Existing campaign (campaign_id) or a new one (new_name). Returns (campaign, created)."""
    if form.get("campaign_id") == CRM_ONLY:
        return None, False
    cid = int_arg("campaign_id", None, form)
    if cid:
        return get_or_404(Campaign, cid), False
    name = (form.get("new_name") or "").strip()
    if not name:
        return None, False
    existing = sess.query(Campaign).filter(func.lower(Campaign.name) == name.lower()).first()
    if existing:
        return existing, False
    camp = Campaign(name=name, status="draft", is_active=False)
    return camp, True


@bp.route("/import", methods=["GET", "POST"])
def upload():
    sess = db()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            err("Choose a CSV or Excel file.")
        else:
            try:
                raw = file.read()
                importer.read_table(file.filename, raw)  # validate now so errors show on this page
                token = importer.save_upload(file.filename, raw)
            except ValueError as e:
                err(str(e))
            except Exception as e:  # corrupt xlsx etc.
                err(f"Could not read that file: {e}")
            else:
                return redirect(url_for("imports.mapping", token=token,
                                        campaign_id=request.form.get("campaign_id") or "",
                                        new_name=request.form.get("new_name") or ""))
    selected = request.args.get("campaign")
    campaigns = sess.query(Campaign).filter(Campaign.status != "archived").order_by(Campaign.name).all()
    return render_template("import/upload.html", campaigns=campaigns, crm_only=(selected or request.form.get("campaign_id")) == CRM_ONLY,
                           selected=int_arg("campaign"), form=request.form)


@bp.route("/import/<token>", methods=["GET", "POST"])
def mapping(token):
    sess = db()
    try:
        filename, raw = importer.load_upload(token)
        headers, rows = importer.read_table(filename, raw)
    except ValueError as e:
        err(str(e))
        return redirect(url_for("imports.upload"))

    values = request.form if request.method == "POST" else request.args
    crm_only = values.get("campaign_id") == CRM_ONLY
    camp, created = _campaign_from_form(sess, values)
    if request.method == "POST":
        mapping_ = [values.get(f"map_{i}", "ignore") for i in range(len(headers))]
        extra_as_custom = values.get("extra_custom", "1") == "1"
    else:
        mapping_ = importer.guess_mapping(headers)
        extra_as_custom = True
    mode = values.get("mode", "upsert")
    if mode not in ("upsert", "create_only", "update_only"):
        mode = "upsert"

    targets = importer.BASE_TARGETS + ([] if crm_only else
                                       importer.step_targets(len(steps_of(sess, camp.id)) if camp and camp.id else 3))
    valid_targets = {k for k, _ in targets}
    mapping_ = [m if m in valid_targets else "ignore" for m in mapping_]

    problems, preview = [], None
    if "email" not in mapping_:
        problems.append("Map one column to Email.")
    if camp is None and not crm_only:
        problems.append("Choose a campaign, name a new one, or import into the CRM only.")

    if not problems:
        parsed, errors = importer.parse_rows(rows, headers, mapping_, extra_as_custom)
        if crm_only:
            plan, stats = importer.analyse_people(sess, parsed, mode)
        else:
            plan, stats = importer.analyse(sess, parsed, camp, mode)
        preview = dict(stats=stats, errors=errors[:100], error_count=len(errors), total=len(rows),
                       sample=[(r["email"], r.get("first_name"), r.get("company"),
                                sorted(r["steps"]), existing is not None) for r, existing in plan[:8]])
        if request.method == "POST" and request.form.get("confirm") == "1":
            if not plan:
                warn("Nothing to import with these settings.")
            elif crm_only:
                n_new, n_upd = importer.apply_people(sess, plan)
                sess.commit()
                ok(f"Imported into the CRM: {n_new} new people, {n_upd} updated.")
                return redirect(url_for("people.index", sort="recent"))
            else:
                try:
                    if created:
                        sess.add(camp)
                        sess.flush()
                        create_default_steps(sess, camp)
                    n_new, n_upd, locked = importer.apply_plan(sess, camp, plan, settings_now())
                    sess.commit()
                except ValueError as e:
                    sess.rollback()
                    err(str(e))
                else:
                    msg = f"Imported into '{camp.name}': {n_new} created, {n_upd} updated."
                    if locked:
                        msg += f" {locked} stage(s) already sent were left untouched."
                    ok(msg)
                    return redirect(url_for("campaigns.overview", cid=camp.id))

    return render_template(
        "import/mapping.html", token=token, filename=filename, headers=headers, rows=rows[:5],
        mapping=mapping_, targets=targets, camp=camp, created=created, mode=mode, extra_custom=extra_as_custom,
        crm_only=crm_only,
        preview=preview, problems=problems, total_rows=len(rows),
        campaigns=sess.query(Campaign).filter(Campaign.status != "archived").order_by(Campaign.name).all(),
        new_name=values.get("new_name", ""))


@bp.route("/import/template.csv")
def template():
    sess = db()
    cid = int_arg("campaign")
    steps = steps_of(sess, cid) if cid else []
    return Response(importer.template_csv(steps), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=import_template.csv"})
