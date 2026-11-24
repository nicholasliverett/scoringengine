"""White-team exam grade sheet, CSV export, and attendance setting."""

import csv
import io

import pytest

from scoring_engine.db import db
from scoring_engine.exam_grades import get_attendance_points
from scoring_engine.models.exam_credential import ExamParticipation
from scoring_engine.models.score_adjustment import ScoreAdjustment
from scoring_engine.models.service import Service
from tests.scoring_engine.conftest import _login
from tests.scoring_engine.factories import make_check, make_round


@pytest.fixture()
def exam_setup(three_teams):
    blue = three_teams["blue_team"]
    ssh = Service(name="SSH", exam_display="name", team=blue, check_name="SSHCheck", host="h")
    threat = Service(name="=Rogue Key", exam_display="threat", team=blue, check_name="SSHCheck", host="h")
    db.session.add_all([ssh, threat])
    db.session.commit()
    adjustment = ScoreAdjustment(team_id=blue.id, points=100, reason="Exam participation")
    db.session.add(adjustment)
    db.session.flush()
    db.session.add(ExamParticipation(team_id=blue.id, score_adjustment_id=adjustment.id))
    db.session.commit()
    rnd = make_round(number=1)
    make_check(service=ssh, round_obj=rnd, result=False)
    return {**three_teams, "ssh": ssh, "threat": threat, "adjustment": adjustment}


class TestExamGradesAccess:
    @pytest.fixture(autouse=True)
    def setup(self, test_client, exam_setup):
        self.client = test_client

    @pytest.mark.parametrize(
        "method, path",
        [
            ("get", "/api/admin/exam/grades"),
            ("get", "/api/admin/exam/grades.csv"),
            ("post", "/api/admin/exam/attendance"),
        ],
    )
    def test_requires_login(self, method, path):
        assert getattr(self.client, method)(path).status_code == 302

    # The blue portal hides every API outside its allowlist with 404.
    @pytest.mark.parametrize("username, status", [("blueuser", 404), ("reduser", 403)])
    def test_non_white_teams_are_refused(self, username, status):
        _login(self.client, username)
        assert self.client.get("/api/admin/exam/grades").status_code == status
        assert self.client.get("/api/admin/exam/grades.csv").status_code == status
        assert self.client.post("/api/admin/exam/attendance", json={"points": 10}).status_code == status
        assert get_attendance_points() == 100

    def test_admin_page_is_white_only(self):
        _login(self.client, "blueuser")
        assert self.client.get("/admin/grades").status_code == 302
        self.client.get("/logout")
        _login(self.client, "whiteuser")
        page = self.client.get("/admin/grades")
        assert page.status_code == 200
        assert b"Exam Grades" in page.data
        assert b"/api/admin/exam/grades.csv" in page.data


class TestExamGradesWhite:
    @pytest.fixture(autouse=True)
    def setup(self, test_client, exam_setup):
        self.client = test_client
        self.data = exam_setup
        _login(self.client, "whiteuser")

    def test_grid_shows_up_down_and_blank(self):
        self.client.post("/api/admin/exam/attendance", json={"points": 70})
        data = self.client.get("/api/admin/exam/grades").get_json()

        assert data["attendance"] == 70
        assert data["columns"] == ["SSH", "=Rogue Key"]
        assert data["threat_points"] == [20]
        assert data["service_points"] == [10]
        assert data["rows"] == [
            {
                "team": "Blue Team",
                "attendance": "Up",
                "checks": {"SSH": "Down", "=Rogue Key": ""},
                "total": 70,
                "total_max": 100,
            }
        ]

    def test_csv_matches_grid_and_neutralizes_formulas(self):
        self.client.post("/api/admin/exam/attendance", json={"points": 70})
        rnd = make_round(number=2)
        make_check(service=self.data["threat"], round_obj=rnd, result=True)

        response = self.client.get("/api/admin/exam/grades.csv")

        assert response.status_code == 200
        assert response.mimetype == "text/csv"
        assert "attachment" in response.headers["Content-Disposition"]
        rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
        assert rows == [
            ["Team", "Attendance", "SSH", "'=Rogue Key", "Total", "Max"],
            ["Blue Team", "Up", "Down", "Up", "90", "100"],
        ]

    def test_saving_attendance_updates_existing_award(self):
        response = self.client.post("/api/admin/exam/attendance", json={"points": "50"})

        assert response.status_code == 200
        assert response.get_json() == {"attendance": 50}
        db.session.expire_all()
        assert db.session.get(ScoreAdjustment, self.data["adjustment"].id).points == 50
        assert get_attendance_points() == 50

    @pytest.mark.parametrize("points", [-1, 101, "abc", "12.5", True, None, ""])
    def test_rejects_invalid_attendance(self, points):
        response = self.client.post("/api/admin/exam/attendance", json={"points": points})
        assert response.status_code == 400
        assert get_attendance_points() == 100
