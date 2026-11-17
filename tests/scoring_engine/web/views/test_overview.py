from unittest.mock import MagicMock, call

import pytest
from scoring_engine.db import db
from scoring_engine.models.team import Team
from scoring_engine.models.user import User
from flask import render_template as render_template_orig

import scoring_engine.web.views.overview as view_module


class TestOverview:

    @pytest.fixture(autouse=True)
    def setup(self, test_client, db_session):
        self.client = test_client
        self.mock_obj = MagicMock(side_effect=render_template_orig)
        view_module.render_template = self.mock_obj
        yield
        view_module.render_template = render_template_orig

    def test_requires_login(self):
        resp = self.client.get("/overview")
        assert resp.status_code == 302
        assert "/login?" in resp.location

    def test_blue_home(self):
        team = Team(name="Exam Team", color="Blue")
        db.session.add(team)
        db.session.flush()
        db.session.add(User(username="examuser", password="testpass", team=team))
        db.session.commit()
        self.client.post("/login", data={"username": "examuser", "password": "testpass"})
        resp = self.client.get("/overview")
        assert resp.status_code == 200
        assert self.mock_obj.call_args == call("overview.html")
