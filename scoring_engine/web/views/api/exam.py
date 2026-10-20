"""Private exam score views and white-team flag provisioning."""

import hashlib
import re

from flask import jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from scoring_engine.db import db
from scoring_engine.models.exam_credential import ExamCredential
from scoring_engine.models.round import Round
from scoring_engine.models.service_score_snapshot import ServiceScoreSnapshot
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


@mod.route("/api/exam/summary")
@login_required
def exam_summary():
    if not current_user.is_blue_team:
        return jsonify({"error": "forbidden"}), 403
    from scoring_engine.models.exam_credential import ExamParticipation

    points = _own_score(current_user.team_id)
    participation = db.session.query(ExamParticipation).filter_by(team_id=current_user.team_id).one_or_none()
    points["participation_awarded"] = participation is not None
    points["participation_points"] = participation.score_adjustment.points if participation else 0
    return jsonify(points)


@mod.route("/api/exam/stats")
@login_required
def exam_stats():
    if not current_user.is_blue_team:
        return jsonify({"error": "forbidden"}), 403
    scoring = get_sla_config()
    rows = db.session.query(
        ServiceScoreSnapshot.round_number, ServiceScoreSnapshot.service_points
    ).filter(ServiceScoreSnapshot.team_id == current_user.team_id).order_by(
        ServiceScoreSnapshot.round_number
    ).all()
    state_by_round = dict(rows)
    rounds = []
    points = 0
    for number in range(1, Round.get_last_round_num() + 1):
        points = int(state_by_round.get(number, points) or 0)
        rounds.append({"round": number, "points": points})
    # Expose the student's own awarded attendance credit and total only.
    from scoring_engine.models.exam_credential import ExamParticipation

    score = _own_score(current_user.team_id)
    participation = db.session.query(ExamParticipation).filter_by(team_id=current_user.team_id).one_or_none()
    score["participation_points"] = participation.score_adjustment.points if participation else 0
    return jsonify({"rounds": rounds, "score": score})


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
