from unittest.mock import MagicMock, call

import pytest
from flask import render_template as render_template_orig

import scoring_engine.web.views.scoreboard as view_module
from tests.scoring_engine.helpers import populate_sample_data

from scoring_engine.db import db
from scoring_engine.models.team import Team
from scoring_engine.models.user import User


class TestScoreboard:

    @pytest.fixture(autouse=True)
    def setup(self, test_client, db_session):
        self.client = test_client
        self.mock_obj = MagicMock(side_effect=render_template_orig)
        view_module.render_template = self.mock_obj
        yield
        view_module.render_template = render_template_orig

    def test_scoreboard(self):
        populate_sample_data(db.session)
        team = Team(name="Instructor", color="White")
        db.session.add(team)
        db.session.flush()
        db.session.add(User(username="instructor", password="testpass", team=team))
        db.session.commit()
        self.client.post("/login", data={"username": "instructor", "password": "testpass"})
        resp = self.client.get("/scoreboard")
        assert resp.status_code == 200
        assert self.mock_obj.call_args == call("scoreboard.html")

    def test_scoreboard_requires_login(self):
        assert self.client.get("/scoreboard").status_code == 302
