"""Practical exam grades: attendance plus up/down checks, scored out of 100.

Attendance is a white-team setting. The remaining ``100 - attendance`` points
are split across a team's checks, with each threat weighted twice as much as
each named service. A check earns its share only while its latest completed
result passes.
"""

from sqlalchemy import select
from sqlalchemy.sql import func

from scoring_engine.config import config
from scoring_engine.models.check import Check
from scoring_engine.models.exam_credential import ExamParticipation
from scoring_engine.models.score_adjustment import ScoreAdjustment
from scoring_engine.models.service import Service
from scoring_engine.models.setting import Setting
from scoring_engine.models.team import Team

ATTENDANCE_SETTING = "exam_participation_points"
TOTAL_POINTS = 100
SERVICE_WEIGHT = 1
THREAT_WEIGHT = 2


def clamp_attendance(value):
    """Return attendance as an integer from 0 to TOTAL_POINTS; unparseable values become 0."""
    try:
        points = int(value)
    except (TypeError, ValueError):
        return 0
    return min(TOTAL_POINTS, max(0, points))


def get_attendance_points():
    setting = Setting.get_setting(ATTENDANCE_SETTING)
    return clamp_attendance(setting.value if setting else config.exam_participation_points)


def check_category(exam_display):
    return "service" if exam_display == "name" else "threat"


def split_check_points(attendance, categories):
    """Split ``TOTAL_POINTS - attendance`` across checks, in order.

    Uses largest-remainder rounding, so the integer shares always add up to the
    remaining points. Ties go to the earlier check.
    """
    remaining = TOTAL_POINTS - clamp_attendance(attendance)
    weights = [SERVICE_WEIGHT if category == "service" else THREAT_WEIGHT for category in categories]
    total_weight = sum(weights)
    if total_weight == 0 or remaining <= 0:
        return [0] * len(weights)
    numerators = [remaining * weight for weight in weights]
    points = [numerator // total_weight for numerator in numerators]
    leftover = remaining - sum(points)
    by_remainder = sorted(range(len(weights)), key=lambda index: (-(numerators[index] % total_weight), index))
    for index in by_remainder[:leftover]:
        points[index] += 1
    return points


def _latest_check_rows(session, team_ids):
    latest_checks = (
        session.query(Check.service_id, func.max(Check.id).label("check_id"))
        .filter(Check.completed.is_(True))
        .group_by(Check.service_id)
        .subquery()
    )
    return (
        session.query(Service.id, Service.team_id, Service.name, Service.exam_display, Service.port, Check.result)
        .select_from(Service)
        .outerjoin(latest_checks, latest_checks.c.service_id == Service.id)
        .outerjoin(Check, Check.id == latest_checks.c.check_id)
        .filter(Service.team_id.in_(team_ids))
        .order_by(Service.team_id, Service.id)
        .all()
    )


def team_grades(session, team_ids):
    """Return ``{team_id: grade}`` for the given teams.

    Each grade has ``attendance`` (``present``, ``points_earned``, ``points_max``),
    ``checks`` in service order, ``total``, and ``total_max``. A check's
    ``passing`` is True, False, or None when no check has completed.
    """
    team_ids = list(team_ids)
    if not team_ids:
        return {}
    attendance = get_attendance_points()
    present = {
        team_id
        for (team_id,) in session.query(ExamParticipation.team_id).filter(ExamParticipation.team_id.in_(team_ids))
    }

    checks_by_team = {team_id: [] for team_id in team_ids}
    for service_id, team_id, name, exam_display, port, result in _latest_check_rows(session, team_ids):
        if exam_display not in ("name", "finding", "threat"):
            exam_display = "finding"
        checks_by_team[team_id].append(
            {
                "service_id": service_id,
                "name": name,
                "exam_display": exam_display,
                "category": check_category(exam_display),
                "port": port,
                "passing": None if result is None else bool(result),
            }
        )

    grades = {}
    for team_id in team_ids:
        checks = checks_by_team[team_id]
        shares = split_check_points(attendance, [check["category"] for check in checks])
        for check, share in zip(checks, shares):
            check["points_max"] = share
            check["points_earned"] = share if check["passing"] is True else 0
        attendance_earned = attendance if team_id in present else 0
        grades[team_id] = {
            "attendance": {"present": team_id in present, "points_earned": attendance_earned, "points_max": attendance},
            "checks": checks,
            "total": attendance_earned + sum(check["points_earned"] for check in checks),
            "total_max": attendance + sum(check["points_max"] for check in checks),
        }
    return grades


def _up_down(passing):
    if passing is None:
        return ""
    return "Up" if passing else "Down"


def grade_sheet(session):
    """Return the white-team grid: real check names, Up/Down values, and totals."""
    teams = session.query(Team).filter(Team.color == "Blue").order_by(Team.id).all()
    grades = team_grades(session, [team.id for team in teams])

    columns = []
    for team in teams:
        for check in grades[team.id]["checks"]:
            if check["name"] not in columns:
                columns.append(check["name"])

    rows = []
    threat_shares = set()
    service_shares = set()
    for team in teams:
        grade = grades[team.id]
        values = {}
        for check in grade["checks"]:
            values[check["name"]] = _up_down(check["passing"])
            (service_shares if check["category"] == "service" else threat_shares).add(check["points_max"])
        rows.append(
            {
                "team": team.name,
                "attendance": "Up" if grade["attendance"]["present"] else "Down",
                "checks": values,
                "total": grade["total"],
                "total_max": grade["total_max"],
            }
        )

    return {
        "attendance": get_attendance_points(),
        "threat_points": sorted(threat_shares),
        "service_points": sorted(service_shares),
        "columns": columns,
        "rows": rows,
    }


def set_attendance_points(session, points):
    """Save attendance and update the credit already awarded to teams that logged in."""
    points = clamp_attendance(points)
    setting = Setting.get_setting(ATTENDANCE_SETTING)
    if setting is None:
        setting = Setting(name=ATTENDANCE_SETTING, value=str(points))
    else:
        setting.value = str(points)
    session.add(setting)
    session.query(ScoreAdjustment).filter(ScoreAdjustment.id.in_(select(ExamParticipation.score_adjustment_id))).update(
        {ScoreAdjustment.points: points}, synchronize_session=False
    )
    session.commit()
    Setting.clear_cache(ATTENDANCE_SETTING)
    return points
