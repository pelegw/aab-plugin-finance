"""Build the plugin-finance app from the container's environment.

The container receives only its own values. The installer sets them from
aab-plugin.yaml. The names are generic, so the image does not care which .env
variable fed them:
  * PLUGIN_TOKEN: the broker's X-Plugin-Token for this service (mandatory).
  * PLUGIN_SECRETS_KEY: the Fernet key for this service's secret store. The
    store holds nothing, because config_schema is empty and no credential
    exists.
  * PLUGIN_SECRETS_DIR: where that store lives (the /secrets volume).
  * FINANCE_DB: the database, in this service's own finance_data volume.
  * TZ / FINANCE_TZ: the owner's time zone for calendar dates. The default is
    Asia/Jerusalem. When both exist, FINANCE_TZ wins.

`aab_plugin_runtime.from_env` reads the first three. With an empty
PLUGIN_TOKEN, the service does not boot, because an open plugin API must fail
loudly.

The plugin creates the schema at boot. If the volume is not writable yet, the
service still starts and reports 503 health. It then creates the schema on the
first request that can open the file.

uvicorn serves this module as a factory
(`uvicorn --factory aab_plugin_finance.main:create_app`), so an import of this
module reads no environment.
"""

import logging
import os
import sqlite3
from collections.abc import Mapping

from aab_plugin_runtime import from_env, logging_setup
from aab_plugin_runtime.logging_setup import kv
from fastapi import FastAPI

from .adapter import FinanceAdapter
from .clock import Clock
from .store import Store

log = logging.getLogger(__name__)

DEFAULT_DB = "/data/finance.db"
SERVICE = "finance"


def build_adapter(environ: Mapping[str, str]) -> FinanceAdapter:
    store = Store(environ.get("FINANCE_DB") or DEFAULT_DB)
    try:
        store.ensure()
    except (sqlite3.Error, OSError) as exc:
        log.warning("finance store not ready at boot; will retry on use %s",
                    kv(error=type(exc).__name__))
    else:
        log.info("finance store ready %s", kv(db=store.path))
    return FinanceAdapter(store, Clock(environ.get("FINANCE_TZ") or environ.get("TZ")))


def create_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    # serve() also configures logging, but only after the adapter exists. By
    # then the store has already logged its boot lines. A call here first
    # puts those lines in the service's format. A second configure() is a
    # no-op.
    logging_setup.configure(f"plugin-{SERVICE}", dict(env))
    return from_env([build_adapter(env)], dict(env), service=SERVICE)
