import pytest

from scoring_engine.db import db
from scoring_engine.exam_grades import (
    ATTENDANCE_SETTING,
    clamp_attendance,
    get_attendance_points,
    grade_sheet,
    set_attendance_points,
    split_check_points,
    team_grades,
)
from scoring_engine.models.exam_credential import ExamParticipation
from scoring_engine.models.score_adjustment import ScoreAdjustment
from scoring_engine.models.service import Service
from scoring_engine.models.setting import Setting
from scoring_engine.models.team import Team
from tests.scoring_engine.factories import make_check, make_round


def _add_participation(team, points):
    adjustment = ScoreAdjustment(team_id=team.id, points=points, reason="Exam participation")
    db.session.add(adjustment)
    db.session.flush()
    db.session.add(ExamParticipation(team_id=team.id, score_adjustment_id=adjustment.id))
    db.session.commit()
    return adjustment


def _exam_team(name="Team1", service_names=("SSH", "HTTP"), threat_names=("Threat A", "Threat B", "Threat C", "Threat D")):  # noqa: E501
    team = Team(name=name, color="Blue")
    db.session.add(team)
    services = [Service(name=n, exam_display="name", team=team, check_name="SSHCheck", host="h") for n in service_names]
    threats = [Service(name=n, exam_display="threat", team=team, check_name="SSHCheck", host="h") for n in threat_names]
    db.session.add_all(services + threats)
    db.session.commit()
    return team, services, threats


class TestSplitCheckPoints:
    def test_attendance_50_four_threats_two_services(self):
        categories = ["service", "service", "threat", "threat", "threat", "threat"]
        assert split_check_points(50, categories) == [5, 5, 10, 10, 10, 10]

    def test_uneven_split_still_sums_to_remaining(self):
        categories = ["threat", "threat", "threat", "service", "service"]
        points = split_check_points(50, categories)
        # 50 over weight 8: threats 12.5 each, services 6.25 each.
        assert points == [13, 13, 12, 6, 6]
        assert sum(points) == 50

    def test_no_checks_or_full_attendance_award_nothing(self):
        assert split_check_points(50, []) == []
        assert split_check_points(100, ["threat", "service"]) == [0, 0]

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
    def test_up_down_and_blank_follow_latest_completed_check(self):
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
            ("SSH", True, 5, 5),
            ("HTTP", False, 0, 5),
            ("Threat A", True, 10, 10),
            ("Threat B", False, 0, 10),
            ("Threat C", None, 0, 10),
            ("Threat D", None, 0, 10),
        ]
        assert grade["total"] == 50 + 5 + 10
        assert grade["total_max"] == 100

    def test_absent_team_earns_no_attendance(self):
        set_attendance_points(db.session, 50)
        team, _, _ = _exam_team()
        grade = team_grades(db.session, [team.id])[team.id]
        assert grade["attendance"] == {"present": False, "points_earned": 0, "points_max": 50}
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
        assert sheet["service_points"] == [5]
        assert sheet["columns"] == ["SSH", "HTTP", "Threat A", "Threat B", "Threat C", "Threat D"]
        assert [row["team"] for row in sheet["rows"]] == ["Team1", "Team2"]
        first, second = sheet["rows"]
        assert first["attendance"] == "Up"
        assert first["checks"] == {
            "SSH": "Up", "HTTP": "", "Threat A": "Up", "Threat B": "", "Threat C": "", "Threat D": "",
        }
        assert (first["total"], first["total_max"]) == (65, 100)
        assert second["attendance"] == "Down"
        assert second["checks"]["HTTP"] == "Down"
        assert (second["total"], second["total_max"]) == (0, 100)
