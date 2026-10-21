import pytest
import requests

from xp12dem import overpass


@pytest.fixture(autouse=True)
def _reset_state():
    overpass.failed_queries.clear()
    overpass.last_error = None
    yield
    overpass.failed_queries.clear()


class _Resp:
    def __init__(self, status, content):
        self.status_code = status
        self.content = content


class _Session:
    def __init__(self, answers):
        self.answers = list(answers)
        self.headers = {}
        self.calls = []

    def post(self, url, data, timeout):
        self.calls.append((url, data["data"]))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_build_query_matches_ortho4xp_format():
    q = overpass.build_query(('way["natural"="water"]', 'rel["natural"="water"]'), (47, 11, 48, 12))
    assert q == ('[timeout:180];(way["natural"="water"](47, 11, 48, 12);'
                 'rel["natural"="water"](47, 11, 48, 12););(._;>>;);out meta;')


def test_rotates_servers_until_success():
    ok = b"<?xml version='1.0'?>\n<osm>\n</osm>\n"
    session = _Session([_Resp(429, b""), requests.ConnectionError(), _Resp(200, ok)])
    logs = []
    overpass.failed_queries.clear()
    result = overpass.fetch('way["aeroway"]', (47, 11, 48, 12), log=logs.append,
                            session=session, sleep=lambda s: None)
    assert result == ok
    assert [c[0] for c in session.calls] == [u for _, u in overpass.SERVERS[:3]]
    assert "User-Agent" in session.headers and "XP12-DEM-Generator" in session.headers["User-Agent"]
    assert any("429" in line for line in logs)
    assert overpass.failed_queries == []


def test_gives_up_and_records_failure():
    total = overpass.MAX_ROUNDS * len(overpass.SERVERS)
    session = _Session([_Resp(504, b"")] * total)
    overpass.failed_queries.clear()
    result = overpass.fetch('way["aeroway"]', (0, 0, 1, 1), log=lambda s: None,
                            session=session, sleep=lambda s: None)
    assert result == 0
    assert len(session.calls) == total
    assert overpass.failed_queries and "504" in overpass.failed_queries[0]


def test_incomplete_answer_is_retried():
    session = _Session([_Resp(200, b"<osm><node"), _Resp(200, b"<osm></osm>")])
    assert overpass.fetch("x", (0, 0, 1, 1), log=lambda s: None, session=session,
                          sleep=lambda s: None) == b"<osm></osm>"


def test_fails_fast_after_a_total_failure():
    overpass.failed_queries[:] = ["overpass-api.de: busy (504)"]
    session = _Session([])
    assert overpass.fetch("x", (0, 0, 1, 1), log=lambda s: None, session=session,
                          sleep=lambda s: None) == 0
    assert session.calls == []
    overpass.failed_queries.clear()
