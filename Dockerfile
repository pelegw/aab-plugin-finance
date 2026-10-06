# plugin-finance: the finance plugin service for the Agent Authority Broker.
# Serves the plugin API on :8090 on its own network (net_finance, shared with
# the broker only; never published) and owns its database in the
# finance_data volume at /data.
#
# The base image is the gateway's published plugin base (built from the
# gateway's plugins/base/Dockerfile at the same version). It provides Python
# 3.12, the unprivileged `aab` user (uid 10001), /secrets owned by aab (0700),
# the aab-plugin-runtime package, PLUGIN_SECRETS_DIR=/secrets and a TCP
# healthcheck on :8090 (every plugin API route needs the token, which the
# healthcheck must not hold).
FROM ghcr.io/pelegw/aab-plugin-base:0.3.0

# Code is installed as root and stays root-owned: read-only to the running
# process. The runtime dependency is already satisfied by the base image, so
# this pip install fetches only the plugin's own small dependencies.
USER root
COPY VERSION pyproject.toml /tmp/aab-plugin-finance/
COPY aab_plugin_finance /tmp/aab-plugin-finance/aab_plugin_finance
RUN pip install --no-cache-dir /tmp/aab-plugin-finance \
 && rm -rf /tmp/aab-plugin-finance

# The database volume: created and owned in the image so a fresh named
# volume mounted here inherits aab ownership and mode 0700. Only this
# service mounts it.
RUN mkdir -p /data \
 && chown aab:aab /data \
 && chmod 0700 /data
VOLUME /data
ENV FINANCE_DB=/data/finance.db

USER aab
EXPOSE 8090
# One worker: the database has one writer process. --no-access-log: the
# runtime writes its own access line with the broker's request id.
CMD ["uvicorn", "--factory", "aab_plugin_finance.main:create_app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--no-access-log"]
