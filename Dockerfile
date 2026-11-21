# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned labeling commit (needed for AST diffs + fixture grep).
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
ADD https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app && tar -xzf /tmp/sqlglot.tar.gz -C /app \
    && mv /app/sqlglot-${SQLGLOT_SHA} /app/sqlglot \
    && rm /tmp/sqlglot.tar.gz

COPY selector.py /app/selector.py
COPY maps/map.json.gz /app/maps/map.json.gz

# Container runs as nobody on a read-only disk; maps must be world-readable.
RUN chmod -R a+rX /app

WORKDIR /app
CMD ["python", "/app/selector.py"]
