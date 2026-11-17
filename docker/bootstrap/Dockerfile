ARG BASE_IMAGE=ghcr.io/scoringengine/scoringengine/base:latest
FROM ${BASE_IMAGE}

COPY bin/setup /app/bin/setup
COPY bin/migrate /app/bin/migrate
# The generated config contains private team credentials and is mode 0600.
# Preserve that restriction while making it readable to the non-root `engine`
# user that runs setup in the base image.
COPY --chown=engine:engine bin/competition.yaml /app/bin/competition.yaml

COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic

COPY scoring_engine /app/scoring_engine

CMD ["python", "/app/bin/setup"]
