import logging
import secrets

from flask import Flask, flash, jsonify, redirect, request, url_for
from flask_login import LoginManager, current_user
from flask_wtf.csrf import CSRFError

from scoring_engine.cache import agent_cache, cache
from scoring_engine.config import config
from scoring_engine.db import db
from scoring_engine.logger import logger
from scoring_engine.version import version_info
from scoring_engine.web.csrf import csrf, form_retry_target, is_browser_form_submission

# Number of bytes of entropy used for the throwaway key generated when no
# secret_key has been configured.  token_hex returns twice this many chars.
GENERATED_SECRET_KEY_BYTES = 64

# How an operator should produce a key.  We print the *command*, never a live
# key: logs are widely readable and routinely shipped to aggregators, so a
# generated secret emitted to stdout should be considered burned the moment it
# is printed.  Generating it locally keeps it out of the log stream entirely.
SECRET_KEY_GENERATION_HINT = 'Generate one with: python -c "import secrets; print(secrets.token_hex(64))"'

SECRET_KEY_LOCATION_HINT = (
    "Then set SCORINGENGINE_SECRET_KEY in your .env file (docker) or 'secret_key' in "
    "engine.conf (manual install). Do not paste it into a shell command or a log."
)

MISSING_SECRET_KEY_WARNING = (
    "No secret_key is configured, so a random Flask session key was generated for this "
    "process. Sessions will NOT survive a restart (every user is logged out) and will NOT "
    "work across more than one web process or container, which blocks horizontal scaling. "
    "Set a long, random, stable value via the SCORINGENGINE_SECRET_KEY environment "
    "variable or 'secret_key' in engine.conf. " + SECRET_KEY_GENERATION_HINT
)

# Shown by bin/setup, which runs before the web app and is the natural place to
# catch this while there is still time to fix it.
MISSING_SECRET_KEY_SETUP_WARNING = (
    "No secret_key is configured. Until one is set, every web restart logs all users out "
    "and sessions cannot be shared between web processes/containers. " + SECRET_KEY_GENERATION_HINT
)


def secret_key_is_configured():
    """Return whether a usable ``secret_key`` is present in the configuration.

    Used by ``bin/setup`` so the check and the web app agree on what counts as
    configured (a whitespace-only value does not).
    """
    return bool((getattr(config, "secret_key", "") or "").strip())


def get_secret_key():
    """Return the Flask session signing key.

    The value comes from the normal configuration precedence implemented in
    :mod:`scoring_engine.config_loader` -- ``SCORINGENGINE_SECRET_KEY`` in the
    environment, then ``secret_key`` in ``engine.conf``.

    When nothing is configured the application still boots: a random key is
    generated and a loud warning is logged.  No hardcoded fallback key is
    shipped on purpose -- a fixed default would let anyone forge session
    cookies for any deployment that never overrode it.
    """
    if secret_key_is_configured():
        return config.secret_key.strip()

    logger.warning(MISSING_SECRET_KEY_WARNING)
    return secrets.token_hex(GENERATED_SECRET_KEY_BYTES)


def create_app():
    app = Flask(__name__)

    app.config.update(DEBUG=config.debug)
    app.config.update(UPLOAD_FOLDER=config.upload_folder)
    app.secret_key = get_secret_key()

    # Session/remember-me cookie hardening.
    #   HttpOnly  - the session cookie is never readable from JavaScript.
    #   SameSite  - "Lax" still allows top-level GET navigation into the app
    #               (e.g. following a link to the scoreboard) but keeps the
    #               cookie off cross-site POSTs.
    #   Secure    - driven by config.  The shipped docker deployment puts nginx
    #               in front (port 80 301-redirects to 443), so Secure is
    #               correct there, but a plain-HTTP dev run must be able to turn
    #               it off or the browser will silently drop the cookie.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = config.session_cookie_secure
    app.config["REMEMBER_COOKIE_HTTPONLY"] = True
    app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
    app.config["REMEMBER_COOKIE_SECURE"] = config.session_cookie_secure

    # CSRF tokens are bound to the session, so let the session lifetime govern
    # instead of expiring tokens after an hour.  Admin pages are routinely left
    # open for the length of a competition; a time limit would turn every
    # long-lived tab into a stream of 400s.
    app.config["WTF_CSRF_TIME_LIMIT"] = None

    # Static file caching: 1 hour in debug mode, 1 week in production
    # Browsers will cache CSS/JS/images and not re-request until max-age expires
    if config.debug:
        app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 3600  # 1 hour
    else:
        app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 604800  # 1 week

    # Configure Flask-SQLAlchemy
    app.config["SQLALCHEMY_DATABASE_URI"] = config.db_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,  # Verify connections before using them
        "pool_recycle": 3600,  # Recycle connections after 1 hour
    }

    # Initialize Flask-SQLAlchemy with the app
    db.init_app(app)

    if not config.debug:
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)

    from scoring_engine.web.views import (
        about,
        admin,
        api,
        auth,
        flags,
        health,
        injects,
        notifications,
        overview,
        profile,
        scoreboard,
        services,
        stats,
        welcome,
        announcements,
    )

    cache.init_app(app)
    agent_cache.init_app(app)

    # Enable CSRF protection on every state-changing request.  The browser
    # front end supplies the token either as a hidden ``csrf_token`` form field
    # or as an ``X-CSRFToken`` header (see ``templates/base.html``).
    #
    # /api/agent/checkin is deliberately exempt: black team agents post an
    # AES-GCM sealed payload with no session cookie at all, so there is no
    # cookie for an attacker to ride and no way for the agent to obtain a
    # token.  The exemption is declared with @csrf.exempt in
    # scoring_engine/web/views/api/agent.py, next to the endpoint itself.
    #
    # WTF_CSRF_SSL_STRICT is left at its default (on), which additionally
    # requires a same-origin Referer header once the request is HTTPS.  If a
    # deployment ever fronts this with something that strips Referer, that is
    # the knob to turn off -- not the CSRF protection itself.
    csrf.init_app(app)

    # Initialize login manager
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "info"
    login_manager.session_protection = "strong"

    # Register the user_loader function after initializing login_manager
    from scoring_engine.models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    app.register_blueprint(welcome.mod)
    app.register_blueprint(services.mod)
    app.register_blueprint(stats.mod)
    app.register_blueprint(scoreboard.mod)
    app.register_blueprint(profile.mod)
    app.register_blueprint(overview.mod)
    app.register_blueprint(notifications.mod)
    app.register_blueprint(injects.mod)
    app.register_blueprint(auth.mod)
    app.register_blueprint(flags.mod)
    app.register_blueprint(api.mod)
    app.register_blueprint(admin.mod)
    app.register_blueprint(about.mod)
    app.register_blueprint(announcements.mod)
    app.register_blueprint(health.mod)

    @app.before_request
    def restrict_student_portal():
        """Keep Blue exam users inside their private Overview and Stats portal."""
        if not current_user.is_authenticated or not current_user.is_blue_team:
            return None
        path = request.path
        if path.startswith("/static/") or path in {
            "/overview", "/stats", "/logout", "/api/exam/summary", "/api/exam/stats",
            "/api/overview/get_round_data",
        }:
            return None
        if path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return redirect(url_for("overview.home"))

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        """Answer a rejected request in the format its client can actually use.

        Two very different clients hit this handler:

        * Scripted callers (jQuery ``$.ajax``, Dropzone, anything posting JSON)
          parse the body as JSON.  Handing them the stock HTML error page
          produces a baffling "unexpected token <" in the console, so they get
          a 400 with a JSON body.

        * A browser submitting a native ``<form method="POST">`` can only
          render HTML.  Before CSRF protection existed, a stale token on
          ``/login`` just re-rendered the sign-in form -- a scoreboard tab left
          open for hours cost the user one extra click.  A 400 error document
          would turn that into a dead end with no way forward, which during a
          competition is a support ticket per person.  So we flash an
          explanation and bounce them back to the page the form lives on, which
          re-renders it with a token minted from the current session.

        ``is_browser_form_submission`` decides between the two on request
        headers rather than on the ``/api/`` path prefix, because the admin and
        profile screens submit native forms *to* ``/api/`` endpoints -- see the
        docstring there for the full reasoning.
        """
        if is_browser_form_submission():
            flash("Your session expired before that form was submitted. Please try again.", "danger")
            fallback = url_for("welcome.home") if current_user.is_authenticated else url_for("auth.login")
            return redirect(form_retry_target(fallback))
        return jsonify({"status": "error", "error": "CSRF validation failed", "reason": error.description}), 400

    @app.context_processor
    def inject_version():
        return {'version_info': version_info}

    return app
