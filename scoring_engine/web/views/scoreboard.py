from flask import Blueprint, abort, render_template
from flask_login import current_user, login_required

mod = Blueprint("scoreboard", __name__)


@mod.route("/scoreboard")
@login_required
def home():
    if not current_user.is_white_team:
        abort(404)
    return render_template("scoreboard.html")
