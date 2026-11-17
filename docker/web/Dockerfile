ARG BASE_IMAGE=ghcr.io/scoringengine/scoringengine/base:latest
FROM ${BASE_IMAGE}

USER root

# Inject upload folder
RUN mkdir -p /var/uploads && chown -R engine:engine /var/uploads && \
    mkdir -p /var/check_outputs && chown -R engine:engine /var/check_outputs

USER engine

# Keep the web application code in this image instead of inheriting it only
# from the base image.  The base image may be a previously published/stale tag
# when Compose builds services in parallel or when an operator runs `up` with
# an existing web image.  In particular, the readiness route is registered by
# this source tree and must be present for the web healthcheck to succeed.
COPY --chown=engine:engine scoring_engine /app/scoring_engine

COPY bin/web /app/bin/web

# nginx talks the uwsgi protocol on :5000.  The extra HTTP socket is bound to
# loopback only so the container healthcheck can curl /health/ready without
# exposing an unencrypted, nginx-bypassing entrypoint on the docker network.
#
# --socket and --http-socket coexist by design: in uWSGI both options are
# handled by the same core option (core/uwsgi.c registers "http-socket" with
# uwsgi_opt_add_socket, protocol "http"), so they append to one socket list and
# every socket is served by the same worker pool.  Verified against the pinned
# uWSGI 2.0.31 with this exact argument list: both sockets bind ("uwsgi socket
# 0 ... 5000", "uwsgi socket 1 ... 5001") and the stats endpoint reports
# proto "uwsgi" for socket 0 and proto "http" for socket 1.
CMD ["sh", "-c", "python -m scoring_engine.sse_server & exec uwsgi --socket :5000 --http-socket 127.0.0.1:5001 --wsgi-file bin/web --master --processes 4 --threads 2 --listen 256 --harakiri 60 --harakiri-verbose --ignore-sigpipe --ignore-write-errors --disable-write-exception --thunder-lock --stats 0.0.0.0:9191 --stats-http --buffer-size 32768 --post-buffering 8192"]

EXPOSE 5000 8001
