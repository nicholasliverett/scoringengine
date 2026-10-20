import hashlib
import time
import uuid
from urllib.parse import urlparse

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import NoResultFound
from wtforms import BooleanField, FileField, PasswordField, StringField
from wtforms.validators import InputRequired

from scoring_engine.config import config
from scoring_engine.db import db
from scoring_engine.models.exam_credential import ExamCredential, ExamParticipation
from scoring_engine.models.score_adjustment import ScoreAdjustment
from scoring_engine.models.user import User

mod = Blueprint("auth", __name__)
mod.secret_key = str(uuid.uuid4())


# Create the login_manager object here, but don't call init_app yet
# The init_app() call will be done inside create_app() in the main app setup
login_manager = LoginManager()


@mod.before_request
def limit_exam_flag_upload():
    if request.path == "/login/flag":
        request.max_content_length = 8192


# You don't need to call init_app here, it's done in create_app()
# You can still define the user_loader function here, as it's needed for Flask-Login
@login_manager.user_loader
def load_user(id):
    return db.session.get(User, int(id))


# Define the before_request function
@mod.before_app_request
def get_current_user():
    g.user = current_user



# Creating our login form
class LoginForm(FlaskForm):
    username = StringField(
        "inputUsername",
        [InputRequired()],
        render_kw={"class": "form-control", "placeholder": "Username", "required": "true"},
    )
    password = PasswordField(
        "inputUsername",
        [InputRequired()],
        render_kw={"class": "form-control", "placeholder": "Password", "required": "true"},
    )
    remember = BooleanField("Remember Me")


class FlagLoginForm(FlaskForm):
    flag_file = FileField("Participation flag")


def _redirect_after_login(user):
    next_url = request.values.get("next")
    if next_url:
        parsed = urlparse(next_url)
        if parsed.netloc or parsed.scheme:
            next_url = None
    if user.is_white_team:
        return redirect(next_url or url_for("admin.status"))
    if user.is_blue_team:
        return redirect(next_url or url_for("overview.home"))
    if user.is_red_team:
        return redirect(next_url or url_for("flags.home"))
    return redirect(next_url or url_for("overview.home"))


def _record_exam_participation_once(team_id):
    """Atomically record one credit, retrying transient database contention.

    The unique team constraint remains the final arbiter across web workers.
    Some SQLite/deadlock cases surface as OperationalError before that unique
    conflict can be reported, so re-check after rollback and retry briefly.
    """
    for attempt in range(3):
        try:
            existing = db.session.query(ExamParticipation.id).filter_by(team_id=team_id).first()
            if existing is not None:
                return
            adjustment = ScoreAdjustment(
                team_id=team_id,
                points=max(0, int(config.exam_participation_points)),
                reason="Exam participation",
                author_id=None,
            )
            db.session.add(adjustment)
            db.session.flush()
            db.session.add(ExamParticipation(team_id=team_id, score_adjustment_id=adjustment.id))
            db.session.commit()
            return
        except IntegrityError:
            # The other concurrent request may have inserted the unique team
            # record. Roll back its adjustment too, then confirm the winner.
            db.session.rollback()
            if db.session.query(ExamParticipation.id).filter_by(team_id=team_id).first() is not None:
                return
            raise
        except OperationalError:
            db.session.rollback()
            if db.session.query(ExamParticipation.id).filter_by(team_id=team_id).first() is not None:
                return
            if attempt == 2:
                raise
            time.sleep(0.1 * (2**attempt))


@mod.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        flash("You are already logged in.", "info")
        return redirect(url_for("welcome.home"))

    form = LoginForm()
    flag_form = FlagLoginForm()

    if form.errors:
        flash(form.errors, "danger")
        return render_template("login.html", form=form, flag_form=flag_form)

    if form.validate_on_submit():
        username = request.form.get("username")
        password = request.form.get("password")

        if len(password.encode("utf-8")) > 72:
            flash("Password must be 72 bytes or fewer.", "danger")
            return render_template("login.html", form=form, flag_form=flag_form)

        try:
            user = db.session.query(User).filter(User.username == username).one()
        except NoResultFound:
            flash("Invalid username or password. Please try again.", "danger")
            return render_template("login.html", form=form, flag_form=flag_form)

        if user.check_password(password):
            user.authenticated = True
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=form.remember.data)
            return _redirect_after_login(user)
        else:
            flash("Invalid username or password. Please try again.", "danger")
            return render_template("login.html", form=form, flag_form=flag_form)

    return render_template("login.html", form=form, flag_form=flag_form)


@mod.route("/login/flag", methods=["POST"])
def flag_login():
    if current_user.is_authenticated:
        return redirect(url_for("overview.home"))
    form = FlagLoginForm()
    uploaded = form.flag_file.data
    if not form.validate_on_submit() or uploaded is None or not uploaded.filename:
        flash("Choose your exam flag file and try again.", "danger")
        return redirect(url_for("auth.login"))
    # Read one byte over the accepted limit; never trust the browser's filename
    # or Content-Length and never persist the submitted bearer credential.
    raw = uploaded.stream.read(4097)
    if len(raw) > 4096:
        flash("The flag file is too large.", "danger")
        return redirect(url_for("auth.login"))
    try:
        token = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        token = ""
    if not token or len(token) > 512:
        flash("That flag file is not valid.", "danger")
        return redirect(url_for("auth.login"))

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    credential = db.session.query(ExamCredential).filter_by(token_hash=token_hash).one_or_none()
    if credential is None or not credential.user.is_blue_team or credential.team_id != credential.user.team_id:
        flash("That flag was not recognized. Check that you selected the right file.", "danger")
        return redirect(url_for("auth.login"))

    user = credential.user
    _record_exam_participation_once(user.team_id)

    user.authenticated = True
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=False)
    flash("Exam flag accepted. Participation points can only be awarded once.", "success")
    return _redirect_after_login(user)


@mod.route("/unauthorized")
def unauthorized():
    return render_template("unauthorized.html")


@mod.route("/logout")
@login_required
def logout():
    user = current_user
    user.authenticated = False
    db.session.add(user)
    db.session.commit()
    logout_user()
    flash("You have successfully logged out.", "success")
    return redirect(url_for("auth.login"))
