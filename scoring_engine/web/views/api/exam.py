"""Private exam score views and white-team flag provisioning."""

import hashlib
import re

from flask import jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import func

from scoring_engine.config import config
from scoring_engine.db import db
from scoring_engine.models.check import Check
from scoring_engine.models.exam_credential import ExamCredential
from scoring_engine.models.service import Service
from scoring_engine.models.setting import Setting
from scoring_engine.models.team import Team
from scoring_engine.models.user import User
from scoring_engine.sla import get_sla_config

from . import mod


def _own_score(team_id):
    from scoring_engine.scores import team_adjustment_totals, team_service_scores

    scoring = get_sla_config()
    service = team_service_scores(db.session, scoring).get(team_id, 0)
    adjustment = team_adjustment_totals(db.session).get(team_id, 0)

    inject = 0
    visible = Setting.get_setting("inject_scores_visible")
    if visible and visible.value:
        from sqlalchemy.sql import func

        from scoring_engine.models.inject import Inject, InjectRubricScore

        inject = db.session.query(func.coalesce(func.sum(InjectRubricScore.score), 0)).join(Inject).filter(
            Inject.team_id == team_id, Inject.status == "Graded"
        ).scalar() or 0

    if scoring.weighted_scoring_enabled:
        inject = int(round(inject * scoring.inject_weight))
    total = service + inject + adjustment
    if not scoring.allow_negative:
        total = max(0, total)
    return {"total": int(total), "service": int(service), "inject": int(inject),
            "adjustments": int(adjustment), "sla_penalty": 0}


def _own_findings(team_id):
    """Return latest check status and score breakdown for one team's services."""
    latest_checks = (
        db.session.query(Check.service_id, func.max(Check.id).label("check_id"))
        .filter(Check.completed.is_(True))
        .group_by(Check.service_id)
        .subquery()
    )
    rows = (
        db.session.query(Service.name, Service.exam_display, Service.port, Service.points, Check.result)
        .select_from(Service)
        .outerjoin(latest_checks, latest_checks.c.service_id == Service.id)
        .outerjoin(Check, Check.id == latest_checks.c.check_id)
        .filter(Service.team_id == team_id)
        .order_by(Service.id)
        .all()
    )
    label_counts = {"finding": 0, "threat": 0}
    score_totals = {
        "service": {"earned": 0, "max": 0},
        "threat": {"earned": 0, "max": 0},
    }
    findings = []
    for service_name, exam_display, port, service_points, result in rows:
        if exam_display not in label_counts and exam_display != "name":
            exam_display = "finding"

        category = "service" if exam_display == "name" else "threat"
        category_label = "Service" if category == "service" else "Threat"
        if exam_display == "name":
            label = service_name
        else:
            label_counts[exam_display] += 1
            label = f"{exam_display.title()} {label_counts[exam_display]}"

        passing = None if result is None else bool(result)
        if passing is None:
            guidance = "The engine has not completed a check yet."
        elif passing:
            guidance = (
                "The service passed its availability check."
                if category == "service"
                else "The check did not detect the threat condition."
            )
        elif category == "service":
            port_text = f" on port {port}" if port else ""
            guidance = f"The service check failed. Verify it is running and reachable{port_text}."
        else:
            guidance = "The threat condition is still present; remove it before the next check."

        maximum = max(0, int(service_points or 0))
        earned = maximum if passing is True else 0
        score_totals[category]["max"] += maximum
        score_totals[category]["earned"] += earned
        findings.append(
            {
                "name": label,
                "category": category,
                "category_label": category_label,
                "passing": passing,
                "points_earned": earned,
                "points_max": maximum,
                "guidance": guidance,
            }
        )
    return findings, sum(finding["passing"] is True for finding in findings), score_totals


def _own_exam_summary(team_id):
    from scoring_engine.models.exam_credential import ExamParticipation

    points = _own_score(team_id)
    participation = db.session.query(ExamParticipation).filter_by(team_id=team_id).one_or_none()
    participation_points = int(participation.score_adjustment.points) if participation else 0
    findings, passing, category_scores = _own_findings(team_id)
    attendance_max = max(0, int(config.exam_participation_points))
    points.update(
        {
            "participation_points": participation_points,
            "attendance_max": attendance_max,
            "total_max": category_scores["service"]["max"] + category_scores["threat"]["max"] + attendance_max,
            "service_points": category_scores["service"],
            "threat_points": category_scores["threat"],
            "findings": findings,
            "findings_passing": passing,
        }
    )
    return points


@mod.route("/api/exam/summary")
@login_required
def exam_summary():
    if not current_user.is_blue_team:
        return jsonify({"error": "forbidden"}), 403
    return jsonify(_own_exam_summary(current_user.team_id))


@mod.route("/api/exam/stats")
@login_required
def exam_stats():
    if not current_user.is_blue_team:
        return jsonify({"error": "forbidden"}), 403
    return jsonify(_own_exam_summary(current_user.team_id))


@mod.route("/api/admin/exam/credentials", methods=["POST"])
@login_required
def provision_exam_credentials():
    if not current_user.is_white_team:
        return jsonify({"error": "forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    credentials = payload.get("credentials")
    rotate = payload.get("rotate") is True
    if not isinstance(credentials, list) or len(credentials) > 1000:
        return jsonify({"error": "credentials must be a list of at most 1000 entries"}), 400

    prepared = []
    seen = set()
    for entry in credentials:
        if not isinstance(entry, dict) or not isinstance(entry.get("team_id"), int):
            return jsonify({"error": "each entry requires an integer team_id and token"}), 400
        team_id, token = entry["team_id"], entry.get("token")
        if team_id in seen or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{30,256}", token):
            return jsonify({"error": "duplicate team or malformed token"}), 400
        seen.add(team_id)
        team = db.session.get(Team, team_id)
        if team is None or not team.is_blue_team:
            return jsonify({"error": "all team_id values must refer to Blue teams"}), 400
        user = db.session.query(User).filter_by(team_id=team_id).order_by(User.id).first()
        if user is None:
            return jsonify({"error": "every Blue team must have a user"}), 400
        prepared.append((team_id, user.id, hashlib.sha256(token.encode("utf-8")).hexdigest()))

    try:
        for team_id, user_id, token_hash in prepared:
            existing = db.session.query(ExamCredential).filter_by(team_id=team_id).one_or_none()
            if existing is not None:
                if existing.token_hash == token_hash:
                    continue  # idempotent rerun; no plaintext token is returned
                if not rotate:
                    db.session.rollback()
                    return jsonify({"error": "credential exists; use rotate=true to replace it"}), 409
                existing.user_id = user_id
                existing.token_hash = token_hash
            else:
                db.session.add(ExamCredential(team_id=team_id, user_id=user_id, token_hash=token_hash))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "credential conflict; no changes were saved"}), 409
    return jsonify({"registered": len(prepared), "rotated": rotate})
