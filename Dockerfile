# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS src
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /opt/sqlglot \
    && tar -xzf /tmp/sqlglot.tar.gz -C /opt/sqlglot --strip-components=1 \
        sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa/sqlglot \
        sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa/tests

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY --from=src /opt/sqlglot /opt/sqlglot
COPY selector/ /app/selector/
COPY maps/ /app/maps/
# The run disk is read-only, so bytecode has to be written at build time.
RUN python -m compileall -q /app/selector && chmod -R a+rX /app /opt/sqlglot
CMD ["python", "-S", "/app/selector/main.py"]
