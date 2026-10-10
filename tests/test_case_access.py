"""Who may do what with cases and notes.

The access rules in case.py are plain functions, so they are tested first, as
tables. The routes are then driven on a bare app over a fake database, to check
that each applies the right rule in the right order: 403 before the database for
a caller with no case type, 404 for a case the caller does not reach (the same
as for one that does not exist), 409 for a closed one, and nothing written
whenever a route refuses.
"""

import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import case as case_module
from app.authenctication import AuthUser, require_auth_user

ME, OTHER = 1234567, 999  # the caller, and whoever else created a case or wrote a note
CMT_HEALTH = "wsj27:cmt:support:halsa"
CMT_IT = "wsj27:cmt:admin:it"
LEADER_18 = "wsj27:al:18"


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        name="Test User",
        preferred_username=f"{ME}@scoutnet",
        given_name="Test",
        family_name="User",
        member_no=str(ME),
        roles=list(roles),
    )


def _case(**fields) -> dict:
    """A whole case row: case 1, an open level 3 hälsa case for troop 18, created by someone else."""
    return {
        "id": 1,
        "created_at": "2026-10-07T12:00:00Z",
        "creator_id": OTHER,
        "secrecy_level": 3,
        "title": "t",
        "type": "hälsa",
        "about_person_id": None,
        "assigned_to_id": None,
        "troop": "18",
        "latest_note_at": None,
        "closed": False,
        "closed_at": None,
        "closed_by_id": None,
        "extra_access": [],
        "tags": [],
    } | fields


def _note(**fields) -> dict:
    """A whole note row: note 1 on case 1, at level 3, by someone else."""
    return {
        "id": 1,
        "case_id": 1,
        "created_at": "2026-10-07T12:00:00Z",
        "creator_id": OTHER,
        "secrecy_level": 3,
        "title": "t",
        "note": "n",
        "tags": [],
    } | fields


# The caller's relation to a hälsa case: their roles, the case's creator, its extra_access.
CALLERS = {
    "role holder": ([CMT_HEALTH], OTHER, []),
    "creator": ([CMT_HEALTH], ME, []),
    "creator without the role": ([CMT_IT], ME, []),
    "grantee": ([CMT_IT], OTHER, [ME]),
    "outsider": ([CMT_IT], OTHER, []),
}


def _as(who: str, **case_fields) -> tuple[list[str], dict]:
    """The roles of one of CALLERS, and the case they are that to."""
    roles, creator_id, extra_access = CALLERS[who]
    return roles, _case(**{"creator_id": creator_id, "extra_access": extra_access, **case_fields})


# --- The rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "case_type", "troop", "holds"),
    [
        ([CMT_HEALTH], "hälsa", "18", True),
        ([CMT_IT], "hälsa", "18", False),
        (["wsj27:cmt:support"], "hälsa", "18", False),  # the function, not the health part of it
        (["wsj27:cmt:support:halsax"], "hälsa", "18", False),  # a string prefix of halsa
        ([CMT_HEALTH], "cmt", "18", True),  # the health team are CMT members too
        ([CMT_IT], "cmt", "18", True),
        (["wsj27:cmt"], "cmt", "18", True),  # not yet in the Funktion/Roll CSV
        (["wsj27:cmtx:admin"], "cmt", "18", False),  # a string prefix of wsj27:cmt
        ([LEADER_18], "avdelning", "18", True),
        ([LEADER_18], "avdelning", "19", False),
        ([LEADER_18], "avdelning", "181", False),  # a string prefix of the leader's troop
        (["wsj27:al"], "avdelning", "18", False),  # a leader role naming no troop is no troop
        (["wsj27:alx:18"], "avdelning", "18", False),  # a string prefix of wsj27:al
        ([CMT_HEALTH], "avdelning", "18", False),  # troop cases are for the troop's leaders only
        ([LEADER_18], "cmt", "18", False),
        ([LEADER_18], "hälsa", "18", False),
        ([CMT_HEALTH], "admin", "18", False),  # not a case type
    ],
)
def test_the_role_each_case_type_needs(roles, case_type, troop, holds):
    assert case_module._holds_role(_user(*roles), case_type, troop) == holds


@pytest.mark.parametrize(
    ("level", "who", "reaches", "role_holder"),
    [
        (3, "role holder", True, True),
        (3, "grantee", True, False),
        (3, "outsider", False, False),
        (4, "role holder", True, True),
        (4, "grantee", False, False),  # a level 4 case has no grantees, even if one slipped in
        (5, "creator", True, True),
        (5, "role holder", False, False),  # on level 5 only the creator counts as one
        (5, "creator without the role", False, False),
        (5, "grantee", True, False),
    ],
)
def test_who_reaches_a_case_at_each_level(level, who, reaches, role_holder):
    roles, case = _as(who, secrecy_level=level)
    assert case_module._reaches(_user(*roles), case) == reaches
    assert case_module._is_role_holder(_user(*roles), case) == role_holder


@pytest.mark.parametrize(
    ("case_level", "note_level", "who", "author", "readable"),
    [
        (3, 3, "role holder", OTHER, True),
        (3, 3, "grantee", OTHER, True),
        (3, 4, "role holder", OTHER, True),
        (3, 4, "grantee", OTHER, False),  # level 4 keeps grantees out
        (3, 5, "role holder", OTHER, False),  # someone else's level 5 note
        (3, 5, "role holder", ME, True),  # their own
        (3, 5, "grantee", ME, True),  # a grantee's own private note
        (4, 4, "role holder", OTHER, True),
        (4, 5, "role holder", OTHER, False),
        (4, 5, "role holder", ME, True),
        (5, 5, "creator", OTHER, True),  # on a level 5 case, level 5 notes are shared
        (5, 5, "grantee", OTHER, True),  # with its grantees too
    ],
)
def test_who_reads_a_note_at_each_level(case_level, note_level, who, author, readable):
    roles, case = _as(who, secrecy_level=case_level)
    assert case_module._reaches(_user(*roles), case)  # the rule is only asked of someone who does
    note = _note(secrecy_level=note_level, creator_id=author)
    assert case_module._may_read_note(_user(*roles), case, note) == readable


# --- The routes ---------------------------------------------------------------


class Statement(NamedTuple):
    verb: str
    table: str
    sql: str
    args: tuple


class FakeDatabase:
    """Stands in for db.py: rows in `cases` and `notes`, and every statement run in `calls`.

    Statements are told apart by verb and table, not by their exact text, and one
    this does not know fails the test rather than passing for the wrong reason. An
    INSERT returns the row it stored; an UPDATE returns the row as it was, and a
    test reads what changed from `writes`.
    """

    def __init__(self):
        self.cases: list[dict] = []
        self.notes: list[dict] = []
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

    async def fetchrow(self, sql: str, *args):
        statement = self._record(sql, args)
        match statement.verb, statement.table:
            case "SELECT", "cases":  # one case, by id
                return next((c for c in self.cases if c["id"] == args[0]), None)
            case (("SELECT" | "UPDATE"), "case_notes"):  # one note, by case and note id
                return next((n for n in self.notes if (n["case_id"], n["id"]) == args[:2]), None)
            case "UPDATE", "cases":
                return next(c for c in self.cases if c["id"] == args[0])
            case "INSERT", ("cases" | "case_notes") as table:
                columns = re.search(r"\((.*?)\) VALUES", statement.sql)[1].split(", ")
                row = dict(zip(columns, args, strict=True))
                return _case(**row) if table == "cases" else _note(**row)
        raise AssertionError(f"Unexpected statement: {statement.sql}")

    async def fetch(self, sql: str, *args):
        statement = self._record(sql, args)
        match statement.verb, statement.table:
            case "SELECT", "cases":  # the filters in its WHERE are not applied
                return self.cases
            case "SELECT", "case_notes":  # of one case, or of ANY of a list of them
                case_ids = args[0] if isinstance(args[0], list) else [args[0]]
                return [n for n in self.notes if n["case_id"] in case_ids]
        raise AssertionError(f"Unexpected statement: {statement.sql}")

    async def execute(self, sql: str, *args):
        statement = self._record(sql, args)
        if (statement.verb, statement.table) not in {("INSERT", "case_note_read_log"), ("UPDATE", "cases")}:
            raise AssertionError(f"Unexpected statement: {statement.sql}")

    @asynccontextmanager
    async def transaction(self):
        yield self  # as the connection, with the same fetchrow and execute


@pytest.fixture(autouse=True)
def db(monkeypatch) -> FakeDatabase:
    database = FakeDatabase()
    monkeypatch.setattr(case_module, "db_fetchrow", database.fetchrow)
    monkeypatch.setattr(case_module, "db_fetch", database.fetch)
    monkeypatch.setattr(case_module, "db_execute", database.execute)
    monkeypatch.setattr(case_module, "db_transaction", database.transaction)
    return database


# The project's participants, with the roles the Scoutnet cache gives them.
PARTICIPANTS = {
    ME: {"troop": "CMT", "roles": [CMT_HEALTH]},
    1000018: {"troop": "18", "roles": [LEADER_18]},
    1000019: {"troop": "19", "roles": ["wsj27:al:19"]},
    2000001: {"troop": "CMT", "roles": [CMT_IT]},
    2000002: {"troop": "CMT", "roles": ["wsj27:cmtx:admin"]},  # a string prefix of wsj27:cmt
    3000001: {"troop": "18", "roles": []},  # a youth participant in troop 18
    3000002: {"troop": "19", "roles": ["wsj27:legacy-access:x"]},  # roles, but no case type
}


@pytest.fixture(autouse=True)
def participants(monkeypatch):
    monkeypatch.setattr(case_module, "get_single_project", lambda: SimpleNamespace(participants=PARTICIPANTS))


def _client(*roles: str) -> TestClient:
    app = FastAPI()
    app.include_router(case_module.router, prefix="/cases")
    app.dependency_overrides[require_auth_user] = lambda: _user(*roles)
    return TestClient(app)


def _routes(case_routes_only=False):
    for route in case_module.router.routes:
        assert isinstance(route, APIRoute)
        if case_routes_only and "{case_id}" not in route.path:
            continue
        path = route.path.replace("{case_id}", "1").replace("{note_id}", "1")
        for method in route.methods:
            yield method, f"/cases{path}"


@pytest.mark.parametrize(("method", "path"), list(_routes()))
@pytest.mark.parametrize(
    "roles",
    [
        [],
        ["wsj27:cmtx:admin"],  # a string prefix of wsj27:cmt
        ["wsj27:legacy-access:Hälsa plus intern information"],  # access roles come later
        ["wsj27:al"],  # a leader role naming no troop is no troop
        ["wsj27:alx:18"],  # a string prefix of wsj27:al
        ["wsj27:bulkread"],
    ],
)
def test_callers_with_no_case_type_are_refused_before_the_database(method, path, roles, db):
    db.cases = [_case(extra_access=[ME])]  # not even a grant lets them in
    response = _client(*roles).request(method, path, json={})
    assert response.status_code == 403
    assert response.json() == {"detail": "No access to cases"}
    assert db.calls == []


@pytest.mark.parametrize(
    ("roles", "types"),
    [
        ([CMT_HEALTH], ["hälsa", "cmt"]),  # the health team are CMT members too
        ([CMT_IT], ["cmt"]),
        (["wsj27:cmt"], ["cmt"]),  # not yet in the Funktion/Roll CSV
        (["wsj27:cmt:support"], ["cmt"]),  # the function, not the health part of it
        ([LEADER_18], ["avdelning"]),
        ([CMT_IT, LEADER_18], ["cmt", "avdelning"]),
    ],
)
def test_types_follow_roles(roles, types):
    response = _client(*roles).get("/cases/types")
    assert response.json() == types


@pytest.mark.parametrize(("method", "path"), list(_routes(case_routes_only=True)))
@pytest.mark.parametrize(
    ("roles", "case_fields"),
    [
        ([CMT_HEALTH], None),  # no such case
        ([CMT_IT], {}),  # a hälsa case, to someone outside the health team
        ([LEADER_18], {"type": "avdelning", "troop": "19"}),  # another troop's case
        ([CMT_HEALTH], {"secrecy_level": 5}),  # someone else's level 5 case
        ([CMT_IT], {"secrecy_level": 4, "extra_access": [ME]}),  # a grant on a level 4 case
        ([CMT_IT], {"extra_access": [ME + 1]}),  # someone else's grant
    ],
)
def test_a_case_the_caller_does_not_reach_looks_missing(method, path, roles, case_fields, db):
    db.cases = [] if case_fields is None else [_case(**case_fields)]
    response = _client(*roles).request(method, path, json={})
    assert response.status_code == 404
    assert response.json() == {"detail": "Case not found"}
    assert len(db.calls) == 1  # the lookup, and nothing after it


NOTE = {"title": "t", "note": "n"}

# What a role holder and a grantee may do with an open level 3 case they reach. A
# grantee may not pass access on, nor write a note the other grantees cannot read.
ACTIONS = [
    ("POST", "/cases/1/close", None, 200, 200),
    ("GET", "/cases/1/notes", None, 200, 200),
    ("POST", "/cases/1/notes", NOTE, 201, 201),
    ("POST", "/cases/1/notes", NOTE | {"secrecy_level": 4}, 201, 403),
    ("POST", "/cases/1/notes", NOTE | {"secrecy_level": 5}, 201, 201),
    ("GET", "/cases/1/access", None, 200, 200),
    ("PUT", "/cases/1/title", {"title": "t"}, 200, 200),
    ("PUT", "/cases/1/tags", {"tags": ["x"]}, 200, 200),
    ("PUT", "/cases/1/notes/1/tags", {"tags": ["x"]}, 200, 200),
    ("PUT", "/cases/1/assignee", {"assigned_to_id": None}, 200, 200),
    ("PUT", "/cases/1/extra_access", {"extra_access": [2000001]}, 200, 403),
]


@pytest.mark.parametrize(("method", "path", "body", "role_holder", "grantee"), ACTIONS)
@pytest.mark.parametrize("who", ["role holder", "grantee"])
def test_what_role_holders_and_grantees_may_do(method, path, body, role_holder, grantee, who, db):
    roles, case = _as(who)
    db.cases, db.notes = [case], [_note()]
    response = _client(*roles).request(method, path, json=body)
    expected = role_holder if who == "role holder" else grantee
    assert response.status_code == expected
    if expected == 403:
        assert response.json() == {"detail": "Only the case type's own roles may do this"}
        assert db.writes == []


CHANGES = [
    ("POST", "/cases/1/close", None),
    ("POST", "/cases/1/notes", NOTE),
    ("PUT", "/cases/1/title", {"title": "t"}),
    ("PUT", "/cases/1/tags", {"tags": []}),
    ("PUT", "/cases/1/notes/1/tags", {"tags": []}),
    ("PUT", "/cases/1/assignee", {"assigned_to_id": None}),
    ("PUT", "/cases/1/extra_access", {"extra_access": []}),
]


@pytest.mark.parametrize(("method", "path", "body"), CHANGES)
@pytest.mark.parametrize("who", ["role holder", "grantee"])
def test_a_closed_case_cannot_be_changed(method, path, body, who, db):
    roles, case = _as(who, closed=True)
    db.cases, db.notes = [case], [_note()]
    response = _client(*roles).request(method, path, json=body)
    assert response.status_code == 409
    assert response.json() == {"detail": "Case is closed"}
    assert len(db.calls) == 1  # the lookup, and nothing after it


@pytest.mark.parametrize("who", ["role holder", "grantee"])
@pytest.mark.parametrize(("closed", "status_code"), [(True, 200), (False, 409)])
def test_only_a_closed_case_can_be_reopened(who, closed, status_code, db):
    roles, case = _as(who, closed=closed)
    db.cases = [case]
    response = _client(*roles).post("/cases/1/reopen")
    assert response.status_code == status_code
    assert bool(db.writes) == (status_code == 200)


# --- Creating a case ----------------------------------------------------------

BODY = {"title": "t", "type": "hälsa", "troop": "18"}


@pytest.mark.parametrize(
    ("roles", "body"),
    [
        ([CMT_IT], {}),  # a hälsa case without the health role
        ([CMT_HEALTH], {"type": "avdelning"}),  # troop cases are for the troop's leaders
        ([LEADER_18], {"type": "avdelning", "troop": "19"}),
        ([LEADER_18], {"type": "cmt"}),
        ([CMT_IT], {"extra_access": [ME]}),  # being a grantee gives no right to create the type
    ],
)
def test_creating_a_case_needs_its_role(roles, body, db):
    response = _client(*roles).post("/cases", json=BODY | body)
    assert response.status_code == 403
    assert db.calls == []


def test_unknown_case_types_are_rejected(db):
    response = _client(CMT_HEALTH).post("/cases", json=BODY | {"type": "admin"})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid case type: admin"}
    assert db.calls == []


@pytest.mark.parametrize(("sent", "stored"), [({}, 3), ({"secrecy_level": 4}, 4), ({"secrecy_level": 5}, 5)])
def test_a_case_is_stored_with_the_secrecy_level_sent_or_3(sent, stored):
    response = _client(CMT_HEALTH).post("/cases", json=BODY | sent)
    assert response.status_code == 201
    assert response.json()["secrecy_level"] == stored


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"secrecy_level": 1}, "Secrecy levels 1 and 2 are not in use yet"),
        ({"secrecy_level": 2}, "Secrecy levels 1 and 2 are not in use yet"),
        ({"secrecy_level": 4, "extra_access": [2000001]}, "A secrecy level 4 case cannot have extra_access"),
    ],
)
def test_secrecy_levels_a_case_cannot_be_created_with(body, detail, db):
    response = _client(CMT_HEALTH).post("/cases", json=BODY | body)
    assert response.status_code == 422
    assert response.json() == {"detail": detail}
    assert db.calls == []


@pytest.mark.parametrize(
    ("about_person_id", "status_code"),
    [
        (3000001, 201),  # someone in the leader's own troop
        (None, 201),  # the troop as a whole
        (1000019, 422),  # someone in another troop
        (9999999, 422),  # nobody: the same answer, so a leader cannot map the contingent
    ],
)
def test_a_leader_files_troop_cases_only_about_their_own_troop(about_person_id, status_code, db):
    response = _client(LEADER_18).post("/cases", json=BODY | {"type": "avdelning", "about_person_id": about_person_id})
    assert response.status_code == status_code
    if status_code == 422:
        assert response.json() == {"detail": f"Member {about_person_id} is not in troop 18"}
        assert db.calls == []


# --- extra_access -------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "extra_access", "status_code"), [(3, [2000001], 200), (4, [2000001], 422), (4, [], 200)]
)
def test_a_level_4_case_cannot_be_given_extra_access(level, extra_access, status_code, db):
    roles, case = _as("role holder", secrecy_level=level)
    db.cases = [case]
    response = _client(*roles).put("/cases/1/extra_access", json={"extra_access": extra_access})
    assert response.status_code == status_code
    assert bool(db.writes) == (status_code == 200)


@pytest.mark.parametrize(
    ("who", "status_code"),
    [
        ("creator", 200),  # the creator may share a level 5 case
        ("grantee", 403),  # a grantee may not
        ("role holder", 404),  # another role holder cannot see it at all
    ],
)
def test_who_shares_a_level_5_case(who, status_code, db):
    roles, case = _as(who, secrecy_level=5)
    db.cases = [case]
    response = _client(*roles).put("/cases/1/extra_access", json={"extra_access": [2000001]})
    assert response.status_code == status_code
    assert bool(db.writes) == (status_code == 200)


# --- Notes --------------------------------------------------------------------


@pytest.mark.parametrize(("case_level", "sent", "stored"), [(3, None, 3), (5, None, 5), (3, 4, 4), (4, 5, 5)])
def test_a_note_gets_the_level_sent_or_its_cases(case_level, sent, stored, db):
    roles, case = _as("creator", secrecy_level=case_level)
    db.cases = [case]
    response = _client(*roles).post("/cases/1/notes", json=NOTE if sent is None else NOTE | {"secrecy_level": sent})
    assert response.status_code == 201
    assert response.json()["secrecy_level"] == stored


def test_a_note_cannot_be_below_its_case(db):
    roles, case = _as("role holder", secrecy_level=4)
    db.cases = [case]
    response = _client(*roles).post("/cases/1/notes", json=NOTE | {"secrecy_level": 3})
    assert response.status_code == 422
    assert db.writes == []


NOTES = [
    _note(id=1, secrecy_level=3),
    _note(id=2, secrecy_level=4),
    _note(id=3, secrecy_level=5),
    _note(id=4, secrecy_level=5, creator_id=ME),
]


@pytest.mark.parametrize(("who", "seen"), [("role holder", [1, 2, 4]), ("grantee", [1, 4])])
def test_the_note_list_leaves_out_notes_the_caller_may_not_read(who, seen, db):
    roles, case = _as(who)
    db.cases, db.notes = [case], NOTES
    response = _client(*roles).get("/cases/1/notes")
    assert sorted(note["id"] for note in response.json()) == seen
    assert [write.table for write in db.writes] == ["case_note_read_log"]  # every read is logged


@pytest.mark.parametrize(
    ("who", "note_id", "status_code"),
    [
        ("role holder", 2, 200),  # a level 4 note, to a role holder
        ("role holder", 4, 200),  # the caller's own level 5 note
        ("role holder", 3, 404),  # someone else's level 5 note
        ("grantee", 2, 404),  # a level 4 note, to a grantee
        ("role holder", 99, 404),  # no such note
    ],
)
def test_note_tags_can_only_be_changed_on_a_note_the_caller_may_read(who, note_id, status_code, db):
    roles, case = _as(who)
    db.cases, db.notes = [case], NOTES
    response = _client(*roles).put(f"/cases/1/notes/{note_id}/tags", json={"tags": ["x"]})
    assert response.status_code == status_code
    if status_code == 404:
        assert response.json() == {"detail": "Note not found"}
        assert db.writes == []


# --- Lists --------------------------------------------------------------------


def test_the_case_list_holds_only_the_cases_the_caller_reaches(db):
    db.cases = [
        _case(id=1, type="cmt"),
        _case(id=2),  # a hälsa case
        _case(id=3, type="avdelning", troop="18"),
        _case(id=4, type="avdelning", troop="19"),  # another troop's
        _case(id=5, extra_access=[ME]),  # a hälsa case, granted
        _case(id=6, type="cmt", secrecy_level=5),  # someone else's level 5 case
    ]
    response = _client(CMT_IT, LEADER_18).get("/cases")
    assert [case["id"] for case in response.json()] == [1, 3, 5]


def test_the_tag_list_holds_only_tags_the_caller_may_read(db):
    db.cases = [
        _case(id=1, extra_access=[ME], tags=["granted case"]),
        _case(id=2, tags=["unreached case"]),
        _case(id=3, type="cmt", tags=["cmt case"]),
    ]
    db.notes = [
        _note(id=1, case_id=1, tags=["level 3"]),
        _note(id=2, case_id=1, secrecy_level=4, tags=["level 4"]),  # kept from grantees
        _note(id=3, case_id=1, secrecy_level=5, tags=["someone else's"]),
        _note(id=4, case_id=1, secrecy_level=5, creator_id=ME, tags=["my own"]),
        _note(id=5, case_id=2, tags=["unreached note"]),
    ]
    response = _client(CMT_IT).get("/cases/tags")
    assert response.json() == ["cmt case", "granted case", "level 3", "my own"]


# --- Who has access, and who a case can be assigned to ------------------------


@pytest.mark.parametrize(
    ("case_fields", "members"),
    [
        ({}, [ME]),
        ({"extra_access": [2000001]}, [ME, 2000001]),  # a grantee who can use cases
        ({"extra_access": [3000001, 3000002, 7654321]}, [ME]),  # grantees refused all of /cases
        ({"type": "cmt"}, [ME, 2000001]),
        ({"type": "avdelning", "extra_access": [ME]}, [1000018, ME]),  # the troop's own leaders only
        ({"secrecy_level": 4, "extra_access": [2000001]}, [ME]),  # a level 4 case has no grantees
        ({"secrecy_level": 5, "creator_id": ME, "extra_access": [2000001]}, [ME, 2000001]),  # creator and grantees
        ({"secrecy_level": 5, "creator_id": 2000001, "extra_access": [ME]}, [ME]),  # a creator without the role
    ],
)
def test_who_has_access_to_a_case(case_fields, members, db):
    db.cases = [_case(**case_fields)]
    response = _client(CMT_HEALTH).get("/cases/1/access")
    assert response.json() == members


@pytest.mark.parametrize(
    ("assignee", "allowed"),
    [
        (ME, True),  # a role holder
        (2000001, True),  # a grantee who can use cases
        (None, True),  # nobody
        (3000001, False),  # a grantee whose roles give no case type
        (7654321, False),  # a grantee who is not a participant
        (1000018, False),  # a troop leader, on a hälsa case
        (9999999, False),
    ],
)
def test_a_case_can_only_be_assigned_to_someone_who_can_open_it(assignee, allowed, db):
    db.cases = [_case(extra_access=[2000001, 3000001, 7654321])]
    response = _client(CMT_HEALTH).put("/cases/1/assignee", json={"assigned_to_id": assignee})
    assert bool(db.writes) == allowed
    if not allowed:
        assert response.status_code == 422
        assert response.json() == {"detail": f"Member {assignee} has no access to this case"}


@pytest.mark.parametrize(("title", "status_code"), [("New title", 200), ("", 422)])
def test_a_case_can_be_retitled(title, status_code, db):
    db.cases = [_case()]
    response = _client(CMT_HEALTH).put("/cases/1/title", json={"title": title})
    assert response.status_code == status_code
    assert [write.args for write in db.writes] == ([(1, title)] if status_code == 200 else [])
