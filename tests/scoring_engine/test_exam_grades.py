import pytest

from scoring_engine.db import db
from scoring_engine.exam_grades import (
    ATTENDANCE_SETTING,
    clamp_attendance,
    get_attendance_points,
    grade_sheet,
    service_penalty,
    set_attendance_points,
    split_threat_points,
    team_grades,
)
from scoring_engine.models.exam_credential import ExamParticipation
from scoring_engine.models.score_adjustment import ScoreAdjustment
from scoring_engine.models.service import Service
from scoring_engine.models.setting import Setting
from scoring_engine.models.team import Team
from tests.scoring_engine.factories import make_check, make_round

THREAT_NAMES = ("Threat A", "Threat B", "Threat C", "Threat D", "Threat E")


def _add_participation(team, points):
    adjustment = ScoreAdjustment(team_id=team.id, points=points, reason="Exam participation")
    db.session.add(adjustment)
    db.session.flush()
    db.session.add(ExamParticipation(team_id=team.id, score_adjustment_id=adjustment.id))
    db.session.commit()
    return adjustment


def _exam_team(name="Team1", service_names=("SSH", "HTTP"), threat_names=THREAT_NAMES):
    team = Team(name=name, color="Blue")
    db.session.add(team)
    services = [Service(name=n, exam_display="name", team=team, check_name="SSHCheck", host="h") for n in service_names]
    threats = [Service(name=n, exam_display="threat", team=team, check_name="SSHCheck", host="h") for n in threat_names]
    db.session.add_all(services + threats)
    db.session.commit()
    return team, services, threats


class TestPointSplit:
    def test_attendance_50_five_threats(self):
        assert split_threat_points(50, 5) == [10, 10, 10, 10, 10]
        assert service_penalty(50, 5) == 5

    def test_uneven_split_still_sums_to_remaining(self):
        assert split_threat_points(50, 3) == [17, 17, 16]
        # Half of 16.67 is 8.33, so a down service costs 8.
        assert service_penalty(50, 3) == 8

    def test_penalty_rounds_half_up(self):
        # 50 over four threats is 12.5 each; half is 6.25.
        assert service_penalty(50, 4) == 6
        # 30 over four threats is 7.5 each; half is 3.75.
        assert service_penalty(70, 4) == 4

    def test_no_threats_or_full_attendance(self):
        assert split_threat_points(50, 0) == []
        assert service_penalty(50, 0) == 0
        assert split_threat_points(100, 2) == [0, 0]
        assert service_penalty(100, 2) == 0

    @pytest.mark.parametrize("value, expected", [(-5, 0), (150, 100), ("40", 40), ("abc", 0), (None, 0)])
    def test_clamp_attendance(self, value, expected):
        assert clamp_attendance(value) == expected


class TestAttendanceSetting:
    def test_falls_back_to_config_without_setting(self):
        assert db.session.query(Setting).filter_by(name=ATTENDANCE_SETTING).count() == 0
        assert get_attendance_points() == 100

    def test_set_attendance_updates_existing_award(self):
        team, _, _ = _exam_team()
        adjustment = _add_participation(team, 100)
        other = ScoreAdjustment(team_id=team.id, points=25, reason="Manual bonus")
        db.session.add(other)
        db.session.commit()

        assert set_attendance_points(db.session, 50) == 50

        db.session.expire_all()
        assert get_attendance_points() == 50
        assert db.session.get(ScoreAdjustment, adjustment.id).points == 50
        assert db.session.get(ScoreAdjustment, other.id).points == 25
        grade = team_grades(db.session, [team.id])[team.id]
        assert grade["attendance"] == {"present": True, "points_earned": 50, "points_max": 50}

    def test_saving_twice_updates_one_setting_row(self):
        set_attendance_points(db.session, 40)
        set_attendance_points(db.session, 60)
        assert db.session.query(Setting).filter_by(name=ATTENDANCE_SETTING).count() == 1
        assert get_attendance_points() == 60


class TestTeamGrades:
    def test_threats_earn_and_down_services_subtract(self):
        set_attendance_points(db.session, 50)
        team, services, threats = _exam_team()
        _add_participation(team, 50)
        first = make_round(number=1)
        second = make_round(number=2)
        make_check(service=services[0], round_obj=first, result=False)
        make_check(service=services[0], round_obj=second, result=True)
        make_check(service=services[1], round_obj=first, result=True)
        make_check(service=services[1], round_obj=second, result=False)
        make_check(service=threats[0], round_obj=second, result=True)
        make_check(service=threats[1], round_obj=second, result=False)

        grade = team_grades(db.session, [team.id])[team.id]

        assert [(c["name"], c["passing"], c["points_earned"], c["points_max"]) for c in grade["checks"]] == [
            ("SSH", True, 0, 0),
            ("HTTP", False, -5, 0),
            ("Threat A", True, 10, 10),
            ("Threat B", False, 0, 10),
            ("Threat C", None, 0, 10),
            ("Threat D", None, 0, 10),
            ("Threat E", None, 0, 10),
        ]
        assert [c["penalty"] for c in grade["checks"][:2]] == [5, 5]
        assert grade["total"] == 50 + 10 - 5
        assert grade["total_max"] == 100

    def test_unchecked_services_cost_nothing(self):
        set_attendance_points(db.session, 50)
        team, _, threats = _exam_team()
        _add_participation(team, 50)
        rnd = make_round(number=1)
        for threat in threats:
            make_check(service=threat, round_obj=rnd, result=True)

        grade = team_grades(db.session, [team.id])[team.id]
        assert grade["total"] == 100

    def test_total_never_goes_below_zero(self):
        set_attendance_points(db.session, 50)
        team, services, _ = _exam_team()
        rnd = make_round(number=1)
        for service in services:
            make_check(service=service, round_obj=rnd, result=False)

        grade = team_grades(db.session, [team.id])[team.id]
        assert grade["attendance"] == {"present": False, "points_earned": 0, "points_max": 50}
        assert sum(c["points_earned"] for c in grade["checks"]) == -10
        assert grade["total"] == 0


class TestGradeSheet:
    def test_rows_use_real_names_and_up_down_values(self):
        set_attendance_points(db.session, 50)
        team1, services1, threats1 = _exam_team("Team1")
        team2, services2, _ = _exam_team("Team2")
        db.session.add(Team(name="White", color="White"))
        db.session.commit()
        _add_participation(team1, 50)
        rnd = make_round(number=1)
        make_check(service=services1[0], round_obj=rnd, result=True)
        make_check(service=threats1[0], round_obj=rnd, result=True)
        make_check(service=services2[1], round_obj=rnd, result=False)

        sheet = grade_sheet(db.session)

        assert sheet["attendance"] == 50
        assert sheet["threat_points"] == [10]
        assert sheet["service_penalties"] == [5]
        assert sheet["columns"] == ["SSH", "HTTP", *THREAT_NAMES]
        assert [row["team"] for row in sheet["rows"]] == ["Team1", "Team2"]
        first, second = sheet["rows"]
        assert first["attendance"] == "Up"
        assert first["checks"] == {"SSH": "Up", "HTTP": "", "Threat A": "Up", **{n: "" for n in THREAT_NAMES[1:]}}
        assert (first["total"], first["total_max"]) == (60, 100)
        assert second["attendance"] == "Down"
        assert second["checks"]["HTTP"] == "Down"
        assert (second["total"], second["total_max"]) == (0, 100)
