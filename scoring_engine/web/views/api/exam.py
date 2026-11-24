"""Private exam score views, white-team grade sheet, and flag provisioning."""

import csv
import hashlib
import io
import re

from flask import Response, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from scoring_engine.db import db
from scoring_engine.exam_grades import TOTAL_POINTS, grade_sheet, set_attendance_points, team_grades
from scoring_engine.models.exam_credential import ExamCredential
from scoring_engine.models.team import Team
from scoring_engine.models.user import User

from . import mod


def _finding_guidance(category, passing, port):
    if passing is None:
        return "The engine has not completed a check yet."
    if passing:
        if category == "service":
            return "The service passed its availability check."
        return "The check did not detect the threat condition."
    if category == "service":
        port_text = f" on port {port}" if port else ""
        return f"The service check failed. Verify it is running and reachable{port_text}."
    return "The threat condition is still present; remove it before the next check."


def _own_exam_summary(team_id):
    grade = team_grades(db.session, [team_id])[team_id]
    label_counts = {"finding": 0, "threat": 0}
    category_scores = {
        "service": {"earned": 0, "max": 0},
        "threat": {"earned": 0, "max": 0},
    }
    findings = []
    for check in grade["checks"]:
        category = check["category"]
        if check["exam_display"] == "name":
            label = check["name"]
        else:
            label_counts[check["exam_display"]] += 1
            label = f"{check['exam_display'].title()} {label_counts[check['exam_display']]}"
        category_scores[category]["max"] += check["points_max"]
        category_scores[category]["earned"] += check["points_earned"]
        finding = {
            "name": label,
            "category": category,
            "category_label": "Service" if category == "service" else "Threat",
            "passing": check["passing"],
            "points_earned": check["points_earned"],
            "points_max": check["points_max"],
            "guidance": _finding_guidance(category, check["passing"], check["port"]),
        }
        if category == "service":
            finding["penalty"] = check["penalty"]
        findings.append(finding)
    return {
        "total": grade["total"],
        "total_max": grade["total_max"],
        "participation_points": grade["attendance"]["points_earned"],
        "attendance_max": grade["attendance"]["points_max"],
        "service_points": category_scores["service"],
        "threat_points": category_scores["threat"],
        "findings": findings,
        "findings_passing": sum(finding["passing"] is True for finding in findings),
    }


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


@mod.route("/api/admin/exam/grades")
@login_required
def admin_exam_grades():
    if not current_user.is_white_team:
        return jsonify({"error": "forbidden"}), 403
    return jsonify(grade_sheet(db.session))


def _csv_cell(value):
    # Spreadsheet apps run cells that start with these characters as formulas.
    text = str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@mod.route("/api/admin/exam/grades.csv")
@login_required
def admin_exam_grades_csv():
    if not current_user.is_white_team:
        return jsonify({"error": "forbidden"}), 403
    sheet = grade_sheet(db.session)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["Team", "Attendance", *[_csv_cell(name) for name in sheet["columns"]], "Total", "Max"])
    for row in sheet["rows"]:
        writer.writerow(
            [
                _csv_cell(row["team"]),
                row["attendance"],
                *[row["checks"].get(name, "") for name in sheet["columns"]],
                row["total"],
                row["total_max"],
            ]
        )
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=exam-grades.csv"},
    )


@mod.route("/api/admin/exam/attendance", methods=["POST"])
@login_required
def admin_set_exam_attendance():
    if not current_user.is_white_team:
        return jsonify({"error": "forbidden"}), 403
    payload = request.get_json(silent=True) or {}
    raw = payload.get("points")
    if isinstance(raw, bool):
        raw = None
    try:
        points = int(raw)
    except (TypeError, ValueError):
        points = None
    if points is None or str(raw).strip() != str(points) or not 0 <= points <= TOTAL_POINTS:
        return jsonify({"error": f"points must be a whole number from 0 to {TOTAL_POINTS}"}), 400

    saved = set_attendance_points(db.session, points)

    from scoring_engine.cache_helper import update_overview_data, update_scoreboard_data, update_stats
    from scoring_engine.events import publish_event

    update_overview_data()
    update_scoreboard_data()
    update_stats()
    publish_event("settings_changed", {"setting": "exam_participation_points"})
    return jsonify({"attendance": saved})


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
