"""Overpass API client used in place of Ortho4XP's ``get_overpass_data``.

Ortho4XP sends anonymous GET requests with the whole query in the URL and
picks a random server per attempt. Public Overpass instances increasingly
reject such requests (missing User-Agent, overly long URLs, rate limits), so
we POST the same query with an identifying User-Agent, rotate through all
known servers and report the HTTP status of every failure.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import requests

from . import __version__

USER_AGENT = (f"XP12-DEM-Generator/{__version__} "
              "(+https://github.com/Jumanja991xx/XP12-DEM-Generator; Ortho4XP based)")

# Main instance first (with its alternative front ends), then independent mirrors.
SERVERS: tuple[tuple[str, str], ...] = (
    ("overpass-api.de", "https://overpass-api.de/api/interpreter"),
    ("private.coffee", "https://overpass.private.coffee/api/interpreter"),
    ("lz4.overpass-api.de", "https://lz4.overpass-api.de/api/interpreter"),
    ("mail.ru", "https://maps.mail.ru/osm/tools/overpass/api/interpreter"),
    ("z.overpass-api.de", "https://z.overpass-api.de/api/interpreter"),
    ("kumi.systems", "https://overpass.kumi.systems/api/interpreter"),
)

MAX_ROUNDS = 4  # each round tries every server once
TIMEOUT = 120  # server side query timeout (seconds)
REQUEST_TIMEOUT = 150  # client side, per HTTP request
MAX_SECONDS = 600  # give up on a query after this long, whatever the rounds

last_error: Optional[str] = None
failed_queries: list[str] = []  # one entry per query that failed on every server


def build_query(query, bbox) -> str:
    """Same query text Ortho4XP builds, as an Overpass QL body."""
    if isinstance(query, str):
        body = query + str(bbox) + ";"
    else:
        body = "".join(part + str(bbox) + ";" for part in query)
    return f"[timeout:{TIMEOUT}];({body});(._;>>;);out meta;"


def _looks_complete(content: bytes) -> bool:
    tail = content[-32:].rstrip()
    return tail.endswith(b"</osm>") or tail.endswith(b"</OSM>")


def fetch(query, bbox, log: Callable[[str], None] = print,
          session: Optional[requests.Session] = None,
          sleep: Callable[[float], None] = time.sleep,
          is_cancelled: Callable[[], bool] = lambda: False,
          clock: Callable[[], float] = time.monotonic) -> bytes | int:
    """Return the OSM XML answer, or ``0`` when every server failed."""
    global last_error
    if failed_queries:
        # A previous query already failed everywhere; the tile cannot be built
        # correctly, so don't make the user wait for every remaining query.
        return 0
    session = session or requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml"})
    data = build_query(query, bbox)
    deadline = clock() + MAX_SECONDS
    for round_index in range(MAX_ROUNDS):
        for name, url in SERVERS:
            if is_cancelled():
                failed_queries.append("cancelled")
                return 0
            if clock() > deadline:
                log(f"        Giving up after {MAX_SECONDS // 60} minutes without a valid answer")
                failed_queries.append(last_error or "timeout")
                return 0
            try:
                response = session.post(url, data={"data": data}, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = f"{name}: {type(exc).__name__}"
                log(f"        OSM server {name} unreachable ({type(exc).__name__}), trying next server")
                continue
            content = response.content
            if response.status_code == 200 and _looks_complete(content):
                last_error = None
                return content
            if response.status_code == 200:
                reason = "incomplete answer"
            elif response.status_code == 429:
                reason = "rate limited (429)"
            elif response.status_code in (502, 503, 504):
                reason = f"busy ({response.status_code})"
            else:
                reason = f"HTTP {response.status_code}"
            last_error = f"{name}: {reason}"
            log(f"        OSM server {name} {reason}, trying next server")
        wait = 15 * (round_index + 1)
        log(f"        All Overpass servers failed this round, retrying in {wait} s...")
        sleep(wait)
    failed_queries.append(last_error or "unknown error")
    return 0


def install(osm_module, ui_module) -> None:
    """Replace Ortho4XP's Overpass downloader with :func:`fetch`."""

    def get_overpass_data(query, bbox, server_code=None):
        return fetch(query, bbox, log=lambda text: ui_module.vprint(1, text),
                     is_cancelled=lambda: bool(ui_module.red_flag))

    osm_module.get_overpass_data = get_overpass_data
