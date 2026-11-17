from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user, login_required

mod = Blueprint("overview", __name__)


@mod.route("/overview")
@login_required
def home():
    if not (current_user.is_blue_team or current_user.is_white_team):
        return redirect(url_for("auth.unauthorized"))
    return render_template("overview.html")
