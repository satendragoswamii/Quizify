"""WSGI entry point for running Quizify behind a real server.

The development server in ``app.py`` is for local work only. In production a WSGI
server imports this module and serves ``application``:

    gunicorn --workers 4 --threads 4 --timeout 300 --bind 0.0.0.0:8000 wsgi:application
    waitress-serve --threads 8 --listen 0.0.0.0:8000 wsgi:application       # Windows
    uwsgi --http :8000 --module wsgi:application --enable-threads

``--timeout`` matters: a conversion that calls an AI provider can legitimately run for
minutes, and gunicorn's 30-second default would kill the worker mid-request. Set it
comfortably above ``QUIZ_AI_TIMEOUT`` × the number of retries.

Threads are the better lever here than processes. The parser is I/O-bound while it
waits on a provider, and every worker process keeps its own copy of the settings
overlay — see the note under `Multiple workers` below.

Configuration is read from the environment and from ``.env`` (loaded by ``app``), then
overlaid with whatever the admin panel has stored in the database. Nothing needs to be
passed in here.
"""

import logging
import os

from app import app as application

log = logging.getLogger("quizify.wsgi")

# `app` is the conventional alias some servers and tutorials expect.
app = application


# --------------------------------------------------------------------------------------
# Reverse proxy
# --------------------------------------------------------------------------------------
# Behind nginx, Caddy, or a load balancer, the request as Flask sees it arrives over
# plain HTTP from localhost. QUIZ_PROXY_HOPS tells Werkzeug how many proxies it may
# trust so that the scheme and host are read from X-Forwarded-* instead.
#
# It is off by default and must be set deliberately: trusting those headers when the
# app is reachable directly lets a caller forge them. Set it to the number of proxies
# in front of the app, which is usually 1.
#
# Audit and lockout records do not depend on this — quizify.admin.auth.client_ip reads
# X-Forwarded-For itself — so this is about links and cookie security, not logging.

_hops = 0
try:
    _hops = max(0, int(os.environ.get("QUIZ_PROXY_HOPS", "0")))
except ValueError:
    log.warning("QUIZ_PROXY_HOPS is not a number; ignoring it.")

if _hops:
    from werkzeug.middleware.proxy_fix import ProxyFix

    application.wsgi_app = ProxyFix(
        application.wsgi_app, x_for=_hops, x_proto=_hops, x_host=_hops, x_prefix=_hops)
    log.info("Trusting X-Forwarded-* headers from %s proxy hop(s).", _hops)


# --------------------------------------------------------------------------------------
# Production sanity checks
# --------------------------------------------------------------------------------------
# Warnings rather than failures: an install may be behind a proxy that terminates TLS,
# on a private network, or mid-setup, and refusing to boot would be the wrong call.

def _check_production_config() -> None:
    from quizify import config

    if application.debug:
        log.error("Flask debug mode is ON. Never serve a production site this way.")

    if not config.get_bool("QUIZ_SECURE_COOKIES", False):
        log.warning(
            "QUIZ_SECURE_COOKIES is off, so the session cookie will be sent over plain "
            "HTTP. Turn it on once the site is served over HTTPS.")

    if not config.get("QUIZ_SECRET_KEY"):
        log.info(
            "No QUIZ_SECRET_KEY set; using the key stored in the database. Set one "
            "explicitly if you ever plan to run from more than one database file.")


_check_production_config()


# --------------------------------------------------------------------------------------
# Multiple workers
# --------------------------------------------------------------------------------------
# The database is SQLite in WAL mode with a busy timeout, so several worker processes
# can read and write it safely, and each thread gets its own connection.
#
# Two things to know before scaling out processes:
#
#   Settings are cached per process. A change saved in the admin panel updates the
#   config overlay of the worker that handled that request; the others keep serving the
#   previous values until they restart. With one worker this cannot happen, which is
#   why threads are the better way to add concurrency here. If you do run several
#   processes, reload after changing settings.
#
#   --preload is safe but unnecessary. Importing this module opens the database, and a
#   forked child would inherit that connection; quizify.admin.db notices the pid change
#   and reopens rather than sharing a descriptor. Skipping --preload avoids the question
#   entirely and costs only a little memory.

if __name__ == "__main__":
    # A convenience for checking the production object itself, without gunicorn.
    # Still the development server — do not use it to serve real traffic.
    application.run(host=os.environ.get("QUIZ_HOST", "127.0.0.1"),
                    port=int(os.environ.get("QUIZ_PORT", "8000")),
                    debug=False)
