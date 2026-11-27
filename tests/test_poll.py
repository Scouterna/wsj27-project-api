"""Polls: who may do what, when a poll takes responses, and which messages go out when.

The few plain rules are tested first, as tables. The routes are then driven on a
bare app over a fake database, and last the message pass is run at chosen times,
with the clock and the posting replaced.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import poll as poll_module
from app.authenctication import AuthUser, require_auth_user

ME, OTHER = 1234567, 999
CMT = "wsj27:cmt:admin:it"
LEADER_1 = "wsj27:al:1"
LEADER_2 = "wsj27:al:2"
NO_ACCESS = ["wsj27:legacy-access:x", "wsj27:al", "wsj27:cmtx:admin", "wsj27:alx:1"]

# The poll in these tests is open for 20 hours, so half time is 10 hours in and 5% is left at 19.
OPENS = datetime(2027, 7, 30, 6, 0, tzinfo=UTC)
CLOSES = OPENS + timedelta(hours=20)


def _at(hours: float) -> datetime:
    return OPENS + timedelta(hours=hours)


def _user(*roles: str, member: int = ME) -> AuthUser:
    return AuthUser(
        name="Test User",
        preferred_username=f"{member}@scoutnet",
        given_name="Test",
        family_name="User",
        member_no=str(member),
        roles=list(roles),
    )


def _poll(**fields) -> dict:
    """A whole poll row: poll 1, a troop poll made through the API, with no message sent."""
    return {
        "id": 1,
        "created_at": OPENS - timedelta(days=1),
        "creator_id": OTHER,
        "type": "troop",
        "title": "Avdelningens hälsa",
        "description": "",
        "opens_at": OPENS,
        "closes_at": CLOSES,
        "options": ["grön", "gul", "röd"],
        "audience": [],
        "opening_message": True,
        "reminders": True,
        "messages_sent": 0,
    } | fields


def _individual(**fields) -> dict:
    """A whole individual poll row: poll 1, put to troop leaders and CMT, made by someone else."""
    return (
        _poll(type="individual", title="Kommer du på mötet?", options=["Ja", "Nej", "Kanske"], audience=["al", "cmt"])
        | fields
    )


def _response(**fields) -> dict:
    """A whole response row: troop 1's grön response to poll 1, given by someone else."""
    return {
        "id": 1,
        "poll_id": 1,
        "troop": "1",
        "respondent_id": OTHER,
        "responded_at": _at(1),
        "status": "grön",
        "comment": "",
    } | fields


class Statement(NamedTuple):
    verb: str
    table: str
    sql: str
    args: tuple


class FakeDatabase:
    """Stands in for db.py: rows in `polls` and `responses`, and every statement run in `calls`.

    Statements are told apart by verb and table, and one this does not know fails
    the test. Unlike the fake in test_case_access.py, writes change the rows, as
    the tests here follow a poll through several steps.
    """

    def __init__(self):
        self.polls: list[dict] = []
        self.responses: list[dict] = []
        self.calls: list[Statement] = []

    @property
    def writes(self) -> list[Statement]:
        return [call for call in self.calls if call.verb != "SELECT"]

    def _record(self, sql: str, args: tuple) -> Statement:
        sql = " ".join(sql.split())
        table = re.search(r"\b(?:FROM|INTO|UPDATE) (\w+)", sql)[1]
        statement = Statement(sql.split()[0], table, sql, args)
        self.calls.append(statement)
        return statement

    def _poll(self, poll_id: int) -> dict | None:
        return next((p for p in self.polls if p["id"] == poll_id), None)

    async def fetchrow(self, sql: str, *args):
        statement = self._record(sql, args)
        match statement.verb, statement.table:
            case "SELECT", "polls":
                return self._poll(args[0])
            case "INSERT", "polls":
                columns = [column.strip() for column in re.search(r"\((.*?)\) VALUES", statement.sql)[1].split(",")]
                row = _poll(id=len(self.polls) + 1, **dict(zip(columns, args, strict=True)))
                self.polls.append(row)
                return row
            case "UPDATE", "polls" if "messages_sent" in statement.sql:  # claiming a message
                row = self._poll(args[0])
                if row["messages_sent"] != args[1]:
                    return None
                row["messages_sent"] = args[1] + 1
                return {"id": row["id"]}
            case "UPDATE", "polls":
                row = self._poll(args[0])
                fields = (
                    "title",
                    "description",
                    "opens_at",
                    "closes_at",
                    "options",
                    "audience",
                    "opening_message",
                    "reminders",
                )
                row.update(zip(fields, args[1:], strict=True))
                return row
            case "INSERT", "poll_responses":
                columns = re.search(r"\((.*?)\) VALUES", statement.sql)[1].split(", ")
                row = _response(**dict(zip(columns, args, strict=True)))

                # A troop's response replaces the troop's, a person's own (troop None) the person's.
                def same(r):
                    if row["troop"] is not None:
                        return r["troop"] == row["troop"]
                    return r["troop"] is None and r["respondent_id"] == row["respondent_id"]

                self.responses = [r for r in self.responses if r["poll_id"] != row["poll_id"] or not same(r)] + [row]
                return row
        raise AssertionError(f"Unexpected statement: {statement.sql}")

    async def fetch(self, sql: str, *args):
        statement = self._record(sql, args)
        match statement.verb, statement.table:
            case "SELECT", "polls":
                return self.polls
            case "SELECT", "poll_responses" if "r.troop = $1" in statement.sql:  # one troop's, in all polls
                return [r for r in self.responses if r["troop"] == args[0]]
            case "SELECT", "poll_responses":
                return [r for r in self.responses if r["poll_id"] == args[0]]
        raise AssertionError(f"Unexpected statement: {statement.sql}")

    async def execute(self, sql: str, *args):
        statement = self._record(sql, args)
        match statement.verb, statement.table:
            case "DELETE", "polls":
                self.polls = [p for p in self.polls if p["id"] != args[0]]
            case _:
                raise AssertionError(f"Unexpected statement: {statement.sql}")


@pytest.fixture(autouse=True)
def db(monkeypatch) -> FakeDatabase:
    database = FakeDatabase()
    monkeypatch.setattr(poll_module, "db_fetchrow", database.fetchrow)
    monkeypatch.setattr(poll_module, "db_fetch", database.fetch)
    monkeypatch.setattr(poll_module, "db_execute", database.execute)
    return database


# Leaders in troops 1, 2 and 10, so that number order and string order differ.
PARTICIPANTS = {
    ME: {"troop": "CMT", "roles": [CMT]},
    1000001: {"troop": "1", "roles": [LEADER_1]},
    1000002: {"troop": "2", "roles": [LEADER_2]},
    1000010: {"troop": "10", "roles": ["wsj27:al:10"]},
    1000011: {"troop": "10", "roles": ["wsj27:al:10"]},
    3000001: {"troop": "1", "roles": []},  # a youth participant
    3000002: {"troop": "3", "roles": ["wsj27:alx:3"]},  # a string prefix of wsj27:al
    2000001: {"troop": "CMT", "roles": ["wsj27:cmt:support:halsa"]},
    3000003: {"troop": "CMT", "roles": ["wsj27:cmtx:admin"]},  # a string prefix of wsj27:cmt
}
HOC = "wsj27:cmt:hoc:hoc"


@pytest.fixture(autouse=True)
def participants(monkeypatch):
    monkeypatch.setattr(poll_module, "get_single_project", lambda: SimpleNamespace(participants=PARTICIPANTS))


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> SimpleNamespace:
    """The time poll.py sees. Two hours into the poll unless a test sets `clock.now`."""
    clock = SimpleNamespace(now=_at(2))
    monkeypatch.setattr(poll_module, "_now", lambda: clock.now)
    return clock


def _client(*roles: str, member: int = ME) -> TestClient:
    app = FastAPI()
    app.include_router(poll_module.router, prefix="/polls")
    app.dependency_overrides[require_auth_user] = lambda: _user(*roles, member=member)
    return TestClient(app)


CREATE = {
    "title": "Avdelningens hälsa",
    "opens_at": OPENS.isoformat(),
    "closes_at": CLOSES.isoformat(),
    "options": ["grön", "gul", "röd"],
}
GREEN = {"status": "grön"}

# --- The rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "state"),
    [(-0.01, "upcoming"), (0, "open"), (19.99, "open"), (20, "closed"), (100, "closed")],
)
def test_a_poll_is_open_from_opens_at_until_closes_at(hours, state):
    assert poll_module._state(_poll(), _at(hours)) == state


def test_a_troop_poll_is_put_to_the_troops_with_leaders_in_number_order():
    assert poll_module._recipients(_poll()) == ["1", "2", "10"]


@pytest.mark.parametrize(
    ("audience", "members"),
    [
        (["al"], [1000001, 1000002, 1000010, 1000011]),
        (["cmt"], [ME, 2000001]),
        (["al", "cmt"], [1000001, 1000002, 1000010, 1000011, ME, 2000001]),
    ],
)
def test_an_individual_poll_is_put_to_everyone_in_its_audience(audience, members):
    assert poll_module._recipients(_individual(audience=audience)) == members


# --- The routes ---------------------------------------------------------------


def _calls() -> list[tuple[str, str, dict | None]]:
    """Every route, as a request that would succeed for the right caller."""
    return [
        ("POST", "/polls", CREATE),
        ("GET", "/polls", None),
        ("GET", "/polls/1", None),
        ("PUT", "/polls/1", {"title": "t"}),
        ("DELETE", "/polls/1", None),
        ("GET", "/polls/1/responses", None),
        ("PUT", "/polls/1/response", GREEN),
        ("GET", "/polls/troops/1/responses", None),
    ]


def test_every_route_is_covered_by_the_refusal_tests():
    routes = {(method, "/polls" + route.path) for route in poll_module.router.routes for method in route.methods}
    called = {
        (method, path.replace("/polls/1", "/polls/{poll_id}").replace("troops/1", "troops/{troop}"))
        for method, path, _ in _calls()
    }
    assert routes == called


def test_every_route_documents_itself_the_same_way():
    for route in poll_module.router.routes:
        assert isinstance(route, APIRoute)
        assert route.summary and route.description, route.path
        assert route.status_code in route.responses, route.path


@pytest.mark.parametrize("role", NO_ACCESS)
@pytest.mark.parametrize(("method", "path", "body"), _calls())
def test_a_caller_with_no_poll_role_is_refused_before_the_database(db, role, method, path, body):
    db.polls = [_poll()]
    assert _client(role).request(method, path, json=body).status_code == 403
    assert db.calls == []


@pytest.mark.parametrize(("method", "path", "body"), [c for c in _calls() if c[:2] != ("PUT", "/polls/1/response")])
def test_cmt_may_use_every_route_but_responding(db, clock, method, path, body):
    db.polls = [_poll()]
    clock.now = _at(-1)  # upcoming, so it can also be changed and removed
    assert _client(CMT).request(method, path, json=body).status_code in {200, 201, 204}


@pytest.mark.parametrize(("method", "path", "body"), [_calls()[0], _calls()[3], _calls()[4]])
def test_a_leader_may_not_schedule_change_or_remove(db, clock, method, path, body):
    db.polls = [_poll()]
    clock.now = _at(-1)
    assert _client(LEADER_1).request(method, path, json=body).status_code == 403
    assert db.writes == []


@pytest.mark.parametrize(("method", "path", "body"), [c for c in _calls() if "/polls/1" in c[1]])
def test_an_unknown_poll_is_404(db, method, path, body):
    assert _client(CMT, LEADER_1).request(method, path, json=body).status_code == 404
    assert db.writes == []


def test_creating_a_poll_records_who_did_it(db, clock):
    clock.now = _at(-1)
    created = _client(CMT).post("/polls", json=CREATE)
    assert created.status_code == 201
    assert created.json() | {"created_at": None} == {
        "id": 1,
        "created_at": None,
        "creator_id": ME,
        "type": "troop",
        "title": "Avdelningens hälsa",
        "description": "",
        "opens_at": "2027-07-30T06:00:00Z",
        "closes_at": "2027-07-31T02:00:00Z",
        "options": ["grön", "gul", "röd"],
        "audience": [],
        "opening_message": True,
        "reminders": True,
        "state": "upcoming",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"closes_at": OPENS.isoformat()},  # closes when it opens
        {"closes_at": (OPENS - timedelta(hours=1)).isoformat()},
        {"opens_at": "2027-07-30T06:00:00"},  # no time zone
        {"title": " "},
        {"type": "individual"},  # with no audience
        {"audience": ["al"]},  # a troop poll has none
        {"type": "individual", "audience": ["al", "al"]},
        {"type": "individual", "audience": ["ist"]},
        {"type": "poll"},
        {"options": []},
        {"options": ["grön", " "]},
        {"options": ["grön", "grön"]},
        {"options": None},
    ],
)
def test_a_poll_that_makes_no_sense_is_not_created(db, change):
    assert _client(CMT).post("/polls", json=CREATE | change).status_code == 422
    assert db.writes == []


def test_listing_polls_can_keep_one_state(db):
    db.polls = [
        _poll(id=1, opens_at=_at(-50), closes_at=_at(-40)),
        _poll(id=2),
        _poll(id=3, opens_at=_at(40), closes_at=_at(50)),
    ]
    client = _client(LEADER_1)
    assert [(p["id"], p["state"]) for p in client.get("/polls").json()] == [
        (1, "closed"),
        (2, "open"),
        (3, "upcoming"),
    ]
    assert [p["id"] for p in client.get("/polls", params={"state": "open"}).json()] == [2]
    assert client.get("/polls", params={"state": "soon"}).status_code == 422


@pytest.mark.parametrize(
    ("hours", "change", "expected"),
    [
        (-1, {"title": "Ny titel"}, 200),
        (-1, {"opens_at": _at(1).isoformat()}, 200),
        (2, {"title": "Ny titel"}, 200),  # open
        (2, {"closes_at": _at(30).isoformat()}, 200),
        (2, {"closes_at": _at(1).isoformat()}, 200),  # closes it now
        (2, {"opens_at": _at(1).isoformat()}, 409),  # has opened
        (2, {"opens_at": OPENS.isoformat()}, 200),  # the opening time it already has
        (20, {"title": "Ny titel"}, 409),  # has closed
        (-1, {"closes_at": OPENS.isoformat()}, 422),
        (-1, {"opens_at": _at(21).isoformat()}, 422),  # after the closing time it keeps
        (-1, {"options": ["1", "2", "3"]}, 200),
        (2, {"options": ["1", "2", "3"]}, 409),  # has opened, and may have responses
        (2, {"options": ["grön", "gul", "röd"]}, 200),  # the options it already has
        (-1, {"options": ["1", "1"]}, 422),
        (-1, {"options": []}, 422),
    ],
)
def test_what_can_be_changed_on_a_poll_and_when(db, clock, hours, change, expected):
    db.polls = [_poll()]
    clock.now = _at(hours)
    assert _client(CMT).put("/polls/1", json=change).status_code == expected
    assert bool(db.writes) == (expected == 200)


def test_changing_a_poll_leaves_the_fields_that_were_not_given(db, clock):
    db.polls = [_poll(description="Hur mår avdelningen?")]
    clock.now = _at(-1)
    changed = _client(CMT).put("/polls/1", json={"title": "Ny titel", "closes_at": _at(30).isoformat()}).json()
    assert (changed["title"], changed["description"]) == ("Ny titel", "Hur mår avdelningen?")
    assert (changed["opens_at"], changed["closes_at"]) == ("2027-07-30T06:00:00Z", "2027-07-31T12:00:00Z")
    assert _client(CMT).put("/polls/1", json={"description": ""}).json()["description"] == ""


@pytest.mark.parametrize(("hours", "expected"), [(-1, 204), (0, 409), (2, 409), (20, 409)])
def test_only_a_poll_that_has_not_opened_can_be_removed(db, clock, hours, expected):
    db.polls = [_poll()]
    clock.now = _at(hours)
    assert _client(CMT).delete("/polls/1").status_code == expected
    assert len(db.polls) == (0 if expected == 204 else 1)


def test_a_leader_responds_for_their_troop(db):
    db.polls = [_poll()]
    response = _client(LEADER_1).put("/polls/1/response", json={"status": "gul", "comment": "Två är förkylda"})
    assert response.status_code == 200
    assert response.json() | {"responded_at": None} == {
        "poll_id": 1,
        "troop": "1",
        "status": "gul",
        "comment": "Två är förkylda",
        "respondent_id": ME,
        "responded_at": None,
    }


def test_another_leader_of_the_troop_replaces_the_response(db):
    db.polls = [_poll()]
    _client(LEADER_1, member=1000001).put("/polls/1/response", json={"status": "röd", "comment": "Hjälp"})
    _client(LEADER_1, member=ME).put("/polls/1/response", json=GREEN)
    assert [(r["troop"], r["status"], r["comment"], r["respondent_id"]) for r in db.responses] == [
        ("1", "grön", "", ME)
    ]


@pytest.mark.parametrize(
    ("roles", "troop", "expected"),
    [
        ([LEADER_1], "1", 200),
        (["wsj27:al:10"], "10", 200),
        ([CMT], None, 403),  # CMT sees the responses but gives none
        (["wsj27:alx:1"], None, 403),  # a string prefix of wsj27:al
    ],
)
def test_a_leader_responds_for_their_own_troop(db, roles, troop, expected):
    db.polls = [_poll()]
    assert _client(*roles).put("/polls/1/response", json=GREEN).status_code == expected
    assert [r["troop"] for r in db.responses] == ([troop] if troop else [])


@pytest.mark.parametrize(("hours", "expected"), [(-0.01, 409), (0, 200), (19.99, 200), (20, 409)])
def test_a_response_is_taken_only_while_the_poll_is_open(db, clock, hours, expected):
    db.polls = [_poll()]
    clock.now = _at(hours)
    assert _client(LEADER_1).put("/polls/1/response", json=GREEN).status_code == expected
    assert bool(db.writes) == (expected == 200)


@pytest.mark.parametrize(
    ("options", "body", "expected"),
    [
        (["grön", "gul", "röd"], {"status": "gul"}, 200),  # a comment is Campfire's business
        (["grön", "gul", "röd"], {"status": "röd", "comment": "Hjälp"}, 200),
        (["grön", "gul", "röd"], {"status": "orange"}, 422),
        (["grön", "gul", "röd"], {"status": "Grön"}, 422),  # as written, no folding
        (["grön", "gul", "röd"], {"status": "green"}, 422),
        (["1", "2", "3", "4", "5"], {"status": "4"}, 200),
        (["1", "2", "3", "4", "5"], {"status": 4}, 422),  # a string, as the options are
        (["1", "2", "3", "4", "5"], {"status": "grön"}, 422),
        (["grön", "gul", "röd"], {}, 422),
    ],
)
def test_a_response_must_be_one_of_the_polls_options(db, options, body, expected):
    db.polls = [_poll(options=options)]
    assert _client(LEADER_1).put("/polls/1/response", json=body).status_code == expected
    assert bool(db.writes) == (expected == 200)


def test_only_troops_that_have_responded_are_listed(db):
    db.polls = [_poll()]
    db.responses = [_response(troop="10", status="röd", comment="Hjälp", respondent_id=1000010)]
    listed = _client(CMT).get("/polls/1/responses").json()
    assert [(r["troop"], r["status"], r["comment"], r["respondent_id"]) for r in listed] == [
        ("10", "röd", "Hjälp", 1000010),
    ]


@pytest.mark.parametrize(
    ("roles", "troops"),
    [
        ([CMT], ["1", "2", "10"]),
        (["wsj27:cmt"], ["1", "2", "10"]),
        ([LEADER_2], ["2"]),
        (["wsj27:cmtx:admin", LEADER_2], ["2"]),  # a string prefix of wsj27:cmt
        ([LEADER_1], ["1"]),  # their troop is a string prefix of 10
    ],
)
def test_cmt_sees_every_troops_response_and_a_leader_their_own(db, roles, troops):
    db.polls = [_poll()]
    db.responses = [_response(troop=troop) for troop in ("1", "2", "10")]
    assert [r["troop"] for r in _client(*roles).get("/polls/1/responses").json()] == troops


def test_a_leader_gets_only_their_own_troops_response(db):
    db.polls = [_poll()]
    db.responses = [_response(troop="1", status="röd", comment="Hjälp"), _response(troop="2")]
    listed = _client(LEADER_2).get("/polls/1/responses").json()
    assert [(r["troop"], r["status"]) for r in listed] == [("2", "grön")]


# --- The messages -------------------------------------------------------------


@pytest.fixture
def posted(monkeypatch) -> list[tuple[str, str]]:
    """Every (troop or member, text) the pass posts in a troop's channel or sends to a person."""
    posted = []

    async def post(recipient, message: str) -> None:
        posted.append((recipient, message))

    monkeypatch.setattr(poll_module, "_post_to_troop", post)
    monkeypatch.setattr(poll_module, "_notify_person", post)
    return posted


def _pass(clock, hours: float, posted: list) -> list[tuple]:
    """Run one pass at this time, and return what it posted."""
    clock.now = _at(hours)
    posted.clear()
    asyncio.run(poll_module._send_due_messages())
    return [(troop, text.split()[0]) for troop, text in posted]


def test_a_poll_is_announced_to_all_and_reminders_go_to_those_without_a_response(db, clock, posted):
    db.polls = [_poll()]
    assert _pass(clock, -0.5, posted) == []
    assert _pass(clock, 0, posted) == [("1", "Ny"), ("2", "Ny"), ("10", "Ny")]
    assert _pass(clock, 0.02, posted) == []  # the next minute: not again
    db.responses = [_response(troop="2")]
    assert _pass(clock, 9.9, posted) == []
    assert _pass(clock, 10, posted) == [("1", "Påminnelse:"), ("10", "Påminnelse:")]
    assert _pass(clock, 10.02, posted) == []
    db.responses.append(_response(troop="1"))
    assert _pass(clock, 19, posted) == [("10", "Sista")]
    assert _pass(clock, 19.5, posted) == []
    assert _pass(clock, 20, posted) == []


def test_after_downtime_every_due_message_is_sent_in_order(db, clock, posted):
    db.polls = [_poll()]
    db.responses = [_response(troop="2")]
    assert _pass(clock, 19.5, posted) == [
        ("1", "Ny"),
        ("2", "Ny"),
        ("10", "Ny"),
        ("1", "Påminnelse:"),
        ("10", "Påminnelse:"),
        ("1", "Sista"),
        ("10", "Sista"),
    ]
    assert db.polls[0]["messages_sent"] == len(poll_module.MESSAGES)
    assert _pass(clock, 19.6, posted) == []


def test_a_message_claimed_by_someone_else_is_not_sent(db, clock, posted, monkeypatch):
    async def already_claimed(poll_id: int, number: int) -> bool:
        return False

    db.polls = [_poll()]
    monkeypatch.setattr(poll_module, "db_claim_message", already_claimed)
    assert _pass(clock, 0, posted) == []


def test_a_poll_that_opened_during_downtime_past_half_time_gets_both_messages(db, clock, posted):
    db.polls = [_poll()]
    db.responses = [_response(troop="2")]
    assert _pass(clock, 12, posted) == [
        ("1", "Ny"),
        ("2", "Ny"),
        ("10", "Ny"),
        ("1", "Påminnelse:"),
        ("10", "Påminnelse:"),
    ]
    assert _pass(clock, 19, posted) == [("1", "Sista"), ("10", "Sista")]


def test_everyone_having_responded_means_no_reminder(db, clock, posted):
    db.polls = [_poll(messages_sent=1)]
    db.responses = [_response(troop=troop) for troop in ("1", "2", "10")]
    assert _pass(clock, 10, posted) == []
    assert db.polls[0]["messages_sent"] == 2


def test_each_open_poll_gets_its_own_messages(db, clock, posted):
    db.polls = [_poll(id=1), _poll(id=2, opens_at=_at(9), closes_at=_at(11), messages_sent=1)]
    db.responses = [_response(poll_id=1, troop="1"), _response(poll_id=1, troop="2"), _response(poll_id=2, troop="10")]
    # Poll 1 has had no message and is at half time, poll 2 is at its own half time.
    assert _pass(clock, 10, posted) == [
        ("1", "Ny"),
        ("2", "Ny"),
        ("10", "Ny"),
        ("10", "Påminnelse:"),
        ("1", "Påminnelse:"),
        ("2", "Påminnelse:"),
    ]


def test_message_text_gives_the_closing_time_in_swedish_time_and_the_link(db, clock, posted, monkeypatch):
    db.polls = [_poll()]
    monkeypatch.setattr(poll_module.settings, "CAMPFIRE_URL", "")
    _pass(clock, 0, posted)
    assert posted[0] == ("1", "Ny avstämning: Avdelningens hälsa. Svara i Campfire senast lör 31 juli kl. 04:00.")
    monkeypatch.setattr(poll_module.settings, "CAMPFIRE_URL", "https://campfire.example.org")
    _pass(clock, 19, posted)
    assert posted[0][1].startswith("Påminnelse:")
    assert posted[-1][1].endswith("innan dess. https://campfire.example.org/polls/1")
    monkeypatch.setattr(poll_module.settings, "CAMPFIRE_URL", "https://campfire.example.org/")
    db.polls = [_poll(id=7)]  # a trailing slash on the address is not doubled
    _pass(clock, 0, posted)
    assert posted[0][1].endswith("kl. 04:00. https://campfire.example.org/polls/7")


def test_a_troops_responses_come_from_every_poll_and_only_that_troop(db):
    db.responses = [
        _response(poll_id=1, troop="10", status="grön"),
        _response(poll_id=1, troop="1", status="röd"),
        _response(poll_id=2, troop="10", status="gul", comment="Trötta"),
    ]
    listed = _client(CMT).get("/polls/troops/10/responses").json()
    assert [(r["poll_id"], r["troop"], r["status"], r["comment"]) for r in listed] == [
        (1, "10", "grön", ""),
        (2, "10", "gul", "Trötta"),
    ]
    assert _client(CMT).get("/polls/troops/99/responses").json() == []


@pytest.mark.parametrize(
    ("roles", "troop", "expected"),
    [
        ([CMT], "10", 200),
        (["wsj27:cmt"], "2", 200),
        ([LEADER_1], "1", 200),
        ([LEADER_1], "10", 403),  # their troop is a string prefix of it
        ([LEADER_1], "2", 403),
        (["wsj27:cmtx:admin", LEADER_2], "1", 403),  # a string prefix of wsj27:cmt
    ],
)
def test_cmt_may_see_any_troops_responses_and_a_leader_their_own(db, roles, troop, expected):
    response = _client(*roles).get(f"/polls/troops/{troop}/responses")
    assert response.status_code == expected
    if expected == 403:
        assert db.calls == []


# --- Individual polls ---------------------------------------------------------


def test_creating_an_individual_poll(db, clock):
    clock.now = _at(-1)
    body = CREATE | {"type": "individual", "audience": ["cmt"], "options": ["Ja", "Nej"], "reminders": False}
    created = _client(CMT).post("/polls", json=body).json()
    assert (created["type"], created["audience"], created["options"], created["reminders"]) == (
        "individual",
        ["cmt"],
        ["Ja", "Nej"],
        False,
    )


@pytest.mark.parametrize(
    ("hours", "change", "expected"),
    [
        (-1, {"audience": ["al"]}, 200),
        (2, {"audience": ["al"]}, 409),  # has opened, and may have responses
        (2, {"audience": ["al", "cmt"]}, 200),  # the audience it already has
        (-1, {"audience": []}, 422),
        (-1, {"audience": ["cmt", "cmt"]}, 422),
        (2, {"reminders": False}, 200),
        (2, {"opening_message": False}, 200),
        (2, {"type": "troop"}, 200),  # not a field that can be changed, so left out
    ],
)
def test_what_can_be_changed_on_an_individual_poll_and_when(db, clock, hours, change, expected):
    db.polls = [_individual()]
    clock.now = _at(hours)
    response = _client(CMT).put("/polls/1", json=change)
    assert response.status_code == expected
    if expected == 200:
        assert response.json()["type"] == "individual"


def test_a_troop_poll_gets_no_audience(db, clock):
    db.polls = [_poll()]
    clock.now = _at(-1)
    assert _client(CMT).put("/polls/1", json={"audience": ["al"]}).status_code == 422
    assert db.writes == []


@pytest.mark.parametrize("poll", [_poll(), _individual()])
def test_an_open_poll_read_with_get_can_be_sent_back_with_a_later_close(db, clock, poll):
    db.polls = [poll]
    clock.now = _at(2)
    body = _client(CMT).get("/polls/1").json() | {"closes_at": (CLOSES + timedelta(hours=1)).isoformat()}
    response = _client(CMT).put("/polls/1", json=body)
    assert response.status_code == 200
    assert response.json()["audience"] == poll["audience"]


def test_a_leader_sees_troop_polls_and_the_individual_polls_put_to_leaders(db):
    db.polls = [_poll(id=1), _individual(id=2, audience=["al"]), _individual(id=3, audience=["cmt"])]
    assert [p["id"] for p in _client(LEADER_1).get("/polls").json()] == [1, 2]
    assert [p["id"] for p in _client(CMT).get("/polls").json()] == [1, 2, 3]
    # The routes a leader may use; the others refuse a leader before looking for the poll.
    for method, path, body in [_calls()[2], _calls()[5], _calls()[6]]:
        path = path.replace("/polls/1", "/polls/3")
        assert _client(LEADER_1).request(method, path, json=body).status_code == 404, path
    assert db.writes == []


@pytest.mark.parametrize(
    ("audience", "roles", "expected"),
    [
        (["al"], [LEADER_1], 200),
        (["al"], [CMT], 403),  # sees the poll, but it is not put to CMT
        (["cmt"], [CMT], 200),
        (["cmt"], ["wsj27:cmt"], 200),
        (["cmt"], [LEADER_1], 404),  # does not see the poll
        (["al", "cmt"], [LEADER_2], 200),
        (["al", "cmt"], [HOC], 200),
    ],
)
def test_an_individual_poll_is_answered_by_those_in_its_audience(db, audience, roles, expected):
    db.polls = [_individual(audience=audience)]
    response = _client(*roles).put("/polls/1/response", json={"status": "Ja"})
    assert response.status_code == expected
    if expected == 200:
        assert (response.json()["troop"], response.json()["respondent_id"]) == (None, ME)
    assert bool(db.writes) == (expected == 200)


def test_each_person_has_their_own_response_and_replaces_only_that(db):
    db.polls = [_individual()]
    _client(LEADER_1, member=1000001).put("/polls/1/response", json={"status": "Ja"})
    _client(LEADER_1, member=ME).put("/polls/1/response", json={"status": "Nej"})
    _client(LEADER_1, member=1000001).put("/polls/1/response", json={"status": "Kanske", "comment": "Sen"})
    assert sorted((r["respondent_id"], r["troop"], r["status"]) for r in db.responses) == [
        (1000001, None, "Kanske"),
        (ME, None, "Nej"),
    ]


@pytest.mark.parametrize(
    ("roles", "member", "respondents"),
    [
        ([HOC], 2000001, [1000001, 1000002, ME]),
        ([CMT], OTHER, [1000001, 1000002, ME]),  # the poll's creator
        ([CMT], ME, [ME]),
        ([LEADER_1], 1000001, [1000001]),
        ([LEADER_2], 1000010, []),  # has not responded
        (["wsj27:cmt:hocx:hoc"], 2000001, []),  # a string prefix of the head of contingent's role
    ],
)
def test_an_individual_polls_responses_go_to_the_head_and_the_creator_and_others_see_their_own(
    db, roles, member, respondents
):
    db.polls = [_individual(creator_id=OTHER)]
    db.responses = [
        _response(troop=None, respondent_id=respondent, status="Ja") for respondent in (1000001, 1000002, ME)
    ]
    listed = _client(*roles, member=member).get("/polls/1/responses").json()
    assert [r["respondent_id"] for r in listed] == respondents


def test_a_troops_responses_leave_out_individual_polls(db):
    db.responses = [_response(poll_id=1, troop="1"), _response(poll_id=2, troop=None, respondent_id=1000001)]
    assert [r["poll_id"] for r in _client(LEADER_1).get("/polls/troops/1/responses").json()] == [1]


def test_an_individual_poll_messages_each_person_and_reminds_those_without_a_response(db, clock, posted):
    db.polls = [_individual(audience=["cmt"])]
    assert _pass(clock, 0, posted) == [(ME, "Ny"), (2000001, "Ny")]
    db.responses = [_response(troop=None, respondent_id=ME, status="Ja")]
    assert _pass(clock, 10, posted) == [(2000001, "Påminnelse:")]
    assert posted[0][1].startswith("Påminnelse: Kommer du på mötet? väntar på ditt/ert svar.")


@pytest.mark.parametrize("make", [_poll, _individual])
def test_without_reminders_only_the_opening_message_goes_out(db, clock, posted, make):
    db.polls = [make(reminders=False)]
    assert _pass(clock, 0, posted) != []
    assert _pass(clock, 10, posted) == []
    assert _pass(clock, 19, posted) == []
    assert db.polls[0]["messages_sent"] == len(poll_module.MESSAGES)


def test_without_the_opening_message_only_the_reminders_go_out(db, clock, posted):
    db.polls = [_poll(opening_message=False)]
    db.responses = [_response(troop="2")]
    assert _pass(clock, 0, posted) == []
    assert db.polls[0]["messages_sent"] == 1
    assert _pass(clock, 10, posted) == [("1", "Påminnelse:"), ("10", "Påminnelse:")]
    assert _pass(clock, 19, posted) == [("1", "Sista"), ("10", "Sista")]


def test_with_both_turned_off_no_message_goes_out(db, clock, posted):
    db.polls = [_poll(opening_message=False, reminders=False)]
    assert _pass(clock, 19.5, posted) == []
    assert db.polls[0]["messages_sent"] == len(poll_module.MESSAGES)


def test_a_message_turned_on_after_its_time_is_not_sent(db, clock, posted):
    db.polls = [_poll(opening_message=False, reminders=False)]
    assert _pass(clock, 12, posted) == []
    db.polls[0] |= {"opening_message": True, "reminders": True}
    assert _pass(clock, 12.02, posted) == []
    assert _pass(clock, 19, posted) == [("1", "Sista"), ("2", "Sista"), ("10", "Sista")]


# --- Receipts -----------------------------------------------------------------


def test_a_troops_response_is_posted_in_the_troops_channel_with_the_link(db, posted, monkeypatch):
    monkeypatch.setattr(poll_module.settings, "CAMPFIRE_URL", "https://campfire.example.org")
    db.polls = [_poll()]
    _client(LEADER_1).put("/polls/1/response", json={"status": "gul", "comment": "Två är förkylda"})
    assert posted == [
        (
            "1",
            (
                "Svar sparat: Avdelningens hälsa. Svaret kan ändras i Campfire till lör 31 juli kl. 04:00. "
                "https://campfire.example.org/polls/1"
            ),
        )
    ]


def test_a_persons_response_is_sent_to_them(db, posted, monkeypatch):
    monkeypatch.setattr(poll_module.settings, "CAMPFIRE_URL", "")
    db.polls = [_individual()]
    _client(LEADER_1, member=1000001).put("/polls/1/response", json={"status": "Ja"})
    assert posted == [
        (1000001, "Svar sparat: Kommer du på mötet?. Svaret kan ändras i Campfire till lör 31 juli kl. 04:00.")
    ]


@pytest.mark.parametrize(("hours", "body"), [(-1, GREEN), (2, {"status": "orange"})])
def test_a_response_that_is_not_taken_gets_no_receipt(db, clock, posted, hours, body):
    db.polls = [_poll()]
    clock.now = _at(hours)
    assert _client(LEADER_1).put("/polls/1/response", json=body).status_code in {409, 422}
    assert posted == []
