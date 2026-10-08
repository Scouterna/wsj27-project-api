"""Case access by type: `hälsa` for the health team, `cmt` for every CMT member,
`avdelning` for the leaders of the case's own troop, and anyone on a case's
`extra_access` list — all of it narrowed by the case's and each note's
`secrecy_level`.

The routes are driven on a bare app with the database calls replaced, so a
refusal can be checked to come before any case is looked up or written, and a
case of another type to look exactly like a missing one.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import case as case_module
from app.authenctication import AuthUser, require_auth_user

CMT_HEALTH = "wsj27:cmt:support:halsa"
CMT_IT = "wsj27:cmt:admin:it"


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        name="Test User",
        preferred_username="1234567@scoutnet",
        given_name="Test",
        family_name="User",
        member_no="1234567",
        roles=list(roles),
    )


@pytest.fixture
def db_calls(monkeypatch):
    """Records every database call.

    If `db_calls.case_type` is set, case 1 exists with the attributes below, and
    its notes are `db_calls.notes`.
    """
    defaults = {
        "case_type": None,
        "case_troop": "18",
        "extra_access": [],
        "secrecy_level": 3,
        "creator_id": 999,
        "closed": False,
    }
    calls = type("Calls", (list,), {**defaults, "notes": []})()

    async def fake(*args):
        calls.append(args)
        if args[0].startswith("SELECT * FROM case_notes WHERE id = $2"):
            return next((n for n in calls.notes if n["id"] == args[2]), None)
        if not calls.case_type or args[0] != "SELECT * FROM cases WHERE id = $1":
            return None
        return {
            "type": calls.case_type,
            "troop": calls.case_troop,
            "extra_access": calls.extra_access,
            "secrecy_level": calls.secrecy_level,
            "creator_id": calls.creator_id,
            "closed": calls.closed,
        }

    async def fake_list(*args):
        calls.append(args)
        return calls.notes if args[0].startswith("SELECT * FROM case_notes") else []

    monkeypatch.setattr(case_module, "db_fetchrow", fake)
    monkeypatch.setattr(case_module, "db_fetch", fake_list)
    monkeypatch.setattr(case_module, "db_execute", fake)
    monkeypatch.setattr(case_module, "db_transaction", lambda: calls.append(("BEGIN",)) or _NoDatabase())
    return calls


class _NoDatabase:
    async def __aenter__(self):
        raise RuntimeError("no database in these tests")

    async def __aexit__(self, *exc):
        return False


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


LEADER_18 = "wsj27:al:18"
BODY = {"title": "t", "troop": "18", "secrecy_level": 5}


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
def test_callers_with_no_case_type_are_refused_before_the_database(method, path, roles, db_calls):
    response = _client(*roles).request(method, path, json={})
    assert response.status_code == 403
    assert db_calls == []


@pytest.mark.parametrize(
    ("roles", "types"),
    [
        ([CMT_HEALTH], ["hälsa", "cmt"]),  # the health team are CMT members too
        ([CMT_IT], ["cmt"]),
        (["wsj27:cmt"], ["cmt"]),  # not yet in the Funktion/Roll CSV
        (["wsj27:cmt:support"], ["cmt"]),  # the function, not the health part of it
        (["wsj27:cmt:support:halsax"], ["cmt"]),  # a string prefix of halsa
        ([LEADER_18], ["avdelning"]),
        ([CMT_IT, LEADER_18], ["cmt", "avdelning"]),
    ],
)
def test_types_follow_roles(roles, types):
    response = _client(*roles).get("/cases/types")
    assert response.status_code == 200
    assert response.json() == types


@pytest.mark.parametrize(("method", "path"), list(_routes(case_routes_only=True)))
@pytest.mark.parametrize(
    ("roles", "case_type", "case_troop"),
    [
        ([CMT_IT], "hälsa", "18"),
        ([CMT_HEALTH], "avdelning", "18"),  # troop cases are for the troop's leaders
        ([LEADER_18], "avdelning", "19"),
        ([LEADER_18], "avdelning", "181"),  # a string prefix of the leader's troop
        ([LEADER_18], "cmt", "18"),
        ([LEADER_18], "hälsa", "18"),
    ],
)
def test_a_case_the_caller_may_not_see_looks_missing(method, path, roles, case_type, case_troop, db_calls):
    db_calls.case_type, db_calls.case_troop = case_type, case_troop
    response = _client(*roles).request(method, path, json={})
    assert response.status_code == 404
    assert response.json() == {"detail": "Case not found"}
    assert len(db_calls) == 1  # the case lookup, and nothing after it


@pytest.mark.parametrize(
    ("roles", "case_type", "case_troop"),
    [([CMT_HEALTH], "hälsa", "19"), ([CMT_IT], "cmt", "19"), ([LEADER_18], "avdelning", "18")],
)
def test_a_case_the_caller_may_see_gets_past_the_lookup(roles, case_type, case_troop, db_calls):
    db_calls.case_type, db_calls.case_troop = case_type, case_troop
    _client(*roles).post("/cases/1/close")
    assert len(db_calls) > 1


@pytest.mark.parametrize(
    ("roles", "full_types", "troops"),
    [
        ([CMT_IT], ["cmt"], []),
        ([LEADER_18, "wsj27:al:19"], [], ["18", "19"]),
        ([CMT_HEALTH, LEADER_18], ["hälsa", "cmt"], ["18"]),
    ],
)
def test_listing_and_tags_are_limited_to_what_the_caller_may_see(roles, full_types, troops, db_calls):
    _client(*roles).get("/cases")
    _client(*roles).get("/cases/tags")
    [listing, tags] = db_calls
    assert case_module._SCOPE in listing[0] and listing[1:4] == (full_types, troops, 1234567)
    assert tags[1:] == (full_types, troops, 1234567)
    for sql in (listing[0], tags[0]):
        assert "$3 = ANY(c.extra_access)" in sql  # granted cases are listed whatever their type
    assert case_module._NOTE_SCOPE in tags[0]  # a hidden note's tags stay hidden


@pytest.mark.parametrize(
    ("roles", "case_type", "troop"),
    [
        ([CMT_IT], "hälsa", "18"),
        ([CMT_HEALTH], "avdelning", "18"),
        ([LEADER_18], "avdelning", "19"),
        ([LEADER_18], "cmt", "18"),
    ],
)
def test_creating_a_case_the_caller_may_not_see_is_refused(roles, case_type, troop, db_calls):
    response = _client(*roles).post("/cases", json={**BODY, "type": case_type, "troop": troop})
    assert response.status_code == 403
    assert db_calls == []


def test_unknown_case_types_are_rejected(db_calls):
    response = _client(CMT_HEALTH).post("/cases", json={**BODY, "type": "admin"})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid case type: admin"}
    assert db_calls == []


@pytest.mark.parametrize(("sent", "stored"), [({}, 3), ({"secrecy_level": 4}, 4), ({"secrecy_level": 5}, 5)])
def test_a_case_is_stored_with_the_secrecy_level_sent_or_3(sent, stored, monkeypatch):
    calls = []

    async def fake_fetchrow(sql, *args):
        calls.append(args)
        raise RuntimeError("stop here")

    monkeypatch.setattr(case_module, "db_fetchrow", fake_fetchrow)
    body = {k: v for k, v in BODY.items() if k != "secrecy_level"}
    with pytest.raises(RuntimeError):
        _client(CMT_HEALTH).post("/cases", json={**body, "type": "hälsa", **sent})
    [args] = calls
    assert args[1] == stored


def _project():
    return SimpleNamespace(participants={1000018: {"troop": "18"}, 1000019: {"troop": "19"}})


@pytest.mark.parametrize("about_person_id", [1000019, 9999999])  # another troop, nobody
def test_a_leader_cannot_file_a_troop_case_about_someone_outside_it(about_person_id, monkeypatch, db_calls):
    monkeypatch.setattr(case_module, "get_single_project", _project)
    body = {**BODY, "type": "avdelning", "about_person_id": about_person_id}
    response = _client(LEADER_18).post("/cases", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": f"Member {about_person_id} is not in troop 18"}
    assert db_calls == []


@pytest.mark.parametrize("about_person_id", [1000018, None])  # their own troop, the troop as a whole
def test_a_leader_can_file_a_troop_case_about_their_own_troop(about_person_id, monkeypatch, db_calls):
    monkeypatch.setattr(case_module, "get_single_project", _project)
    body = {**BODY, "type": "avdelning", "about_person_id": about_person_id}
    with pytest.raises(TypeError):  # the fake insert returns no row to build a Case from
        _client(LEADER_18).post("/cases", json=body)
    assert "INSERT INTO cases" in db_calls[0][0]


# --- extra_access -------------------------------------------------------------

ME = 1234567  # _user()'s member number

GRANTEE_REQUESTS = [
    ("POST", "/cases/1/close", None),
    ("POST", "/cases/1/reopen", None),
    ("GET", "/cases/1/notes", None),
    ("POST", "/cases/1/notes", {"title": "t", "note": "n", "secrecy_level": 5}),
    ("PUT", "/cases/1/tags", {"tags": []}),
    ("PUT", "/cases/1/notes/1/tags", {"tags": []}),
    ("PUT", "/cases/1/assignee", {"assigned_to_id": None}),
]


@pytest.mark.parametrize(("method", "path", "body"), GRANTEE_REQUESTS)
@pytest.mark.parametrize(
    ("roles", "case_type", "case_troop"),
    [([CMT_IT], "hälsa", "18"), ([LEADER_18], "avdelning", "19"), ([LEADER_18], "cmt", "18")],
)
def test_extra_access_lets_a_member_in_on_a_case_their_roles_do_not_reach(
    method, path, body, roles, case_type, case_troop, db_calls
):
    db_calls.case_type, db_calls.case_troop, db_calls.extra_access = case_type, case_troop, [ME]
    try:
        response = _client(*roles).request(method, path, json=body)
    except TypeError, RuntimeError:
        return  # got past the lookup; the fake database has no row to build a response from
    # Past the lookup if anything came after it, or if it answered something other than "missing".
    assert len(db_calls) > 1 or response.json() != {"detail": "Case not found"}


@pytest.mark.parametrize(("method", "path", "body"), GRANTEE_REQUESTS[:1])
def test_extra_access_for_someone_else_is_not_access(method, path, body, db_calls):
    db_calls.case_type, db_calls.extra_access = "hälsa", [ME + 1]
    response = _client(CMT_IT).request(method, path, json=body)
    assert response.status_code == 404
    assert len(db_calls) == 1


def test_a_grantee_cannot_pass_access_on(db_calls):
    db_calls.case_type, db_calls.extra_access = "hälsa", [ME]
    response = _client(CMT_IT).put("/cases/1/extra_access", json={"extra_access": [ME, 7654321]})
    assert response.status_code == 403
    assert len(db_calls) == 1


def test_a_role_holder_can_change_extra_access(db_calls):
    db_calls.case_type = "hälsa"
    _client(CMT_HEALTH).put("/cases/1/extra_access", json={"extra_access": [7654321]})
    assert "UPDATE cases SET extra_access" in db_calls[-1][0]


def test_a_grantee_still_cannot_create_cases_of_that_type(db_calls):
    response = _client(CMT_IT).post("/cases", json={**BODY, "type": "hälsa", "extra_access": [ME]})
    assert response.status_code == 403
    assert db_calls == []


# --- secrecy_level ------------------------------------------------------------

OTHER = 999  # the fixture's default creator


@pytest.mark.parametrize(
    ("level", "roles", "creator_id", "extra_access", "visible"),
    [
        (3, [CMT_HEALTH], OTHER, [], True),  # role holders
        (3, [CMT_IT], OTHER, [ME], True),  # and grantees
        (4, [CMT_HEALTH], OTHER, [], True),  # role holders
        (4, [CMT_IT], OTHER, [ME], False),  # but not grantees, even if one slipped in
        (5, [CMT_HEALTH], OTHER, [], False),  # not other role holders
        (5, [CMT_HEALTH], ME, [], True),  # the creator
        (5, [CMT_IT], ME, [], False),  # who must still hold the role
        (5, [CMT_IT], OTHER, [ME], True),  # and grantees
    ],
)
def test_who_sees_a_case_at_each_level(level, roles, creator_id, extra_access, visible, db_calls):
    db_calls.case_type, db_calls.secrecy_level = "hälsa", level
    db_calls.creator_id, db_calls.extra_access = creator_id, extra_access
    response = _client(*roles).post("/cases/1/close")
    assert (len(db_calls) > 1) == visible
    if not visible:
        assert response.status_code == 404


@pytest.mark.parametrize("level", [1, 2])
def test_levels_1_and_2_are_not_in_use(level, db_calls):
    response = _client(CMT_HEALTH).post("/cases", json={**BODY, "type": "hälsa", "secrecy_level": level})
    assert response.status_code == 422
    assert db_calls == []


def test_a_level_4_case_cannot_be_created_with_extra_access(db_calls):
    body = {**BODY, "type": "hälsa", "secrecy_level": 4, "extra_access": [7654321]}
    response = _client(CMT_HEALTH).post("/cases", json=body)
    assert response.status_code == 422
    assert db_calls == []


@pytest.mark.parametrize(("extra_access", "status_code"), [([7654321], 422), ([], None)])
def test_a_level_4_case_cannot_be_given_extra_access(extra_access, status_code, db_calls):
    db_calls.case_type, db_calls.secrecy_level = "hälsa", 4
    response = _client(CMT_HEALTH).put("/cases/1/extra_access", json={"extra_access": extra_access})
    updated = any("UPDATE cases SET extra_access" in call[0] for call in db_calls)
    assert updated == (status_code is None)
    if status_code:
        assert response.status_code == status_code


@pytest.mark.parametrize(
    ("roles", "creator_id", "extra_access", "status_code"),
    [
        ([CMT_HEALTH], ME, [], None),  # the creator may share it
        ([CMT_IT], OTHER, [ME], 403),  # a grantee may not
        ([CMT_HEALTH], OTHER, [], 404),  # another role holder cannot see it at all
    ],
)
def test_who_shares_a_level_5_case(roles, creator_id, extra_access, status_code, db_calls):
    db_calls.case_type, db_calls.secrecy_level = "hälsa", 5
    db_calls.creator_id, db_calls.extra_access = creator_id, extra_access
    response = _client(*roles).put("/cases/1/extra_access", json={"extra_access": [7654321]})
    updated = any("UPDATE cases SET extra_access" in call[0] for call in db_calls)
    assert updated == (status_code is None)
    if status_code:
        assert response.status_code == status_code


@pytest.mark.parametrize(("case_level", "sent", "stored"), [(3, None, 3), (5, None, 5), (3, 4, 4), (4, 5, 5)])
def test_a_note_gets_the_level_sent_or_its_cases(case_level, sent, stored, db_calls, monkeypatch):
    inserted = []

    class _Conn:
        async def fetchrow(self, sql, *args):
            inserted.append(args)
            raise RuntimeError("stop here")

    class _Transaction:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(case_module, "db_transaction", _Transaction)
    db_calls.case_type, db_calls.secrecy_level, db_calls.creator_id = "hälsa", case_level, ME
    body = {"title": "t", "note": "n"} | ({} if sent is None else {"secrecy_level": sent})
    with pytest.raises(RuntimeError):
        _client(CMT_HEALTH).post("/cases/1/notes", json=body)
    [args] = inserted
    assert args[2] == stored


def test_a_note_cannot_be_below_its_case(db_calls):
    db_calls.case_type, db_calls.secrecy_level = "hälsa", 4
    response = _client(CMT_HEALTH).post("/cases/1/notes", json={"title": "t", "note": "n", "secrecy_level": 3})
    assert response.status_code == 422


def _note(note_id, level, creator_id):
    return {
        "id": note_id,
        "case_id": 1,
        "created_at": "2026-10-07T12:00:00Z",
        "creator_id": creator_id,
        "secrecy_level": level,
        "title": "t",
        "note": "n",
        "tags": [],
    }


NOTES = [_note(1, 3, OTHER), _note(2, 4, OTHER), _note(3, 5, OTHER), _note(4, 5, ME)]


@pytest.mark.parametrize(
    ("case_level", "roles", "creator_id", "extra_access", "seen"),
    [
        (3, [CMT_HEALTH], OTHER, [], [1, 2, 4]),  # a role holder: all but someone else's level 5
        (3, [CMT_IT], OTHER, [ME], [1, 4]),  # a grantee: not the level 4 note either
        (4, [CMT_HEALTH], OTHER, [], [2, 4]),  # someone else's level 5 note is still theirs alone
        (5, [CMT_HEALTH], ME, [], [3, 4]),  # on a level 5 case, level 5 notes are shared
        (5, [CMT_IT], OTHER, [ME], [3, 4]),  # with its grantees too
    ],
)
def test_who_sees_which_notes(case_level, roles, creator_id, extra_access, seen, db_calls):
    db_calls.case_type, db_calls.secrecy_level = "hälsa", case_level
    db_calls.creator_id, db_calls.extra_access = creator_id, extra_access
    db_calls.notes = [n for n in NOTES if n["secrecy_level"] >= case_level]
    response = _client(*roles).get("/cases/1/notes")
    assert response.status_code == 200
    assert sorted(n["id"] for n in response.json()) == seen


# --- note tags, level 4 notes, closed cases -----------------------------------


def _updated(db_calls, table):
    return any(call[0].lstrip().startswith(f"UPDATE {table}") for call in db_calls)


@pytest.mark.parametrize(
    ("roles", "extra_access", "note_id", "readable"),
    [
        ([CMT_HEALTH], [], 3, False),  # someone else's level 5 note
        ([CMT_HEALTH], [], 4, True),  # the caller's own
        ([CMT_IT], [ME], 2, False),  # a level 4 note, to a grantee
        ([CMT_HEALTH], [], 2, True),  # a level 4 note, to a role holder
        ([CMT_HEALTH], [], 99, False),  # no such note
    ],
)
def test_note_tags_can_only_be_changed_on_a_note_the_caller_may_read(roles, extra_access, note_id, readable, db_calls):
    db_calls.case_type, db_calls.extra_access, db_calls.notes = "hälsa", extra_access, NOTES
    try:
        response = _client(*roles).put(f"/cases/1/notes/{note_id}/tags", json={"tags": ["x"]})
    except TypeError:
        response = None  # updated; the fake database returns no row
    assert _updated(db_calls, "case_notes") == readable
    if not readable:
        assert response.status_code == 404
        assert response.json() == {"detail": "Note not found"}


@pytest.mark.parametrize(("roles", "extra_access", "allowed"), [([CMT_HEALTH], [], True), ([CMT_IT], [ME], False)])
def test_only_role_holders_write_level_4_notes(roles, extra_access, allowed, db_calls):
    db_calls.case_type, db_calls.extra_access = "hälsa", extra_access
    try:
        response = _client(*roles).post("/cases/1/notes", json={"title": "t", "note": "n", "secrecy_level": 4})
    except RuntimeError:
        response = None  # reached the insert
    assert (("BEGIN",) in db_calls) == allowed
    if not allowed:
        assert response.status_code == 403


def test_a_grantee_may_still_write_a_private_note(db_calls):
    db_calls.case_type, db_calls.extra_access = "hälsa", [ME]
    with pytest.raises(RuntimeError):  # reached the insert
        _client(CMT_IT).post("/cases/1/notes", json={"title": "t", "note": "n", "secrecy_level": 5})


CHANGES = [
    ("POST", "/cases/1/close", None),
    ("POST", "/cases/1/notes", {"title": "t", "note": "n"}),
    ("PUT", "/cases/1/tags", {"tags": []}),
    ("PUT", "/cases/1/notes/1/tags", {"tags": []}),
    ("PUT", "/cases/1/assignee", {"assigned_to_id": None}),
    ("PUT", "/cases/1/extra_access", {"extra_access": []}),
]


@pytest.mark.parametrize(("method", "path", "body"), CHANGES)
def test_a_closed_case_cannot_be_changed(method, path, body, db_calls):
    db_calls.case_type, db_calls.closed, db_calls.notes = "hälsa", True, NOTES
    response = _client(CMT_HEALTH).request(method, path, json=body)
    assert response.status_code == 409
    assert response.json() == {"detail": "Case is closed"}
    assert len(db_calls) == 1  # the case lookup, and nothing after it


@pytest.mark.parametrize(("closed", "status_code"), [(True, None), (False, 409)])
def test_only_a_closed_case_can_be_reopened(closed, status_code, db_calls):
    db_calls.case_type, db_calls.closed = "hälsa", closed
    response = _client(CMT_HEALTH).post("/cases/1/reopen")
    assert _updated(db_calls, "cases") == (status_code is None)
    if status_code:
        assert response.status_code == status_code


# --- who has access, and who a case can be assigned to ------------------------

PARTICIPANTS = {
    ME: {"roles": [CMT_HEALTH]},
    1000018: {"roles": [LEADER_18]},
    1000019: {"roles": ["wsj27:al:19"]},
    2000001: {"roles": [CMT_IT]},
    2000002: {"roles": ["wsj27:cmtx:admin"]},  # a string prefix of wsj27:cmt
    3000001: {"roles": []},
}


@pytest.fixture
def participants(monkeypatch):
    monkeypatch.setattr(case_module, "get_single_project", lambda: SimpleNamespace(participants=PARTICIPANTS))


@pytest.mark.parametrize(
    ("case_type", "level", "creator_id", "extra_access", "members"),
    [
        ("hälsa", 3, OTHER, [], [ME]),
        ("hälsa", 3, OTHER, [7654321], [ME, 7654321]),  # grantees, participants or not
        ("cmt", 3, OTHER, [], [ME, 2000001]),
        ("avdelning", 3, OTHER, [ME], [1000018, ME]),  # the troop's own leaders only
        ("hälsa", 4, OTHER, [7654321], [ME]),  # a level 4 case has no grantees, even if one slipped in
        ("hälsa", 5, ME, [7654321], [ME, 7654321]),  # the creator and grantees
        ("hälsa", 5, 2000001, [ME], [ME]),  # a creator without the role is out
    ],
)
def test_who_has_access_to_a_case(case_type, level, creator_id, extra_access, members, db_calls, participants):
    db_calls.case_type, db_calls.secrecy_level = case_type, level
    db_calls.creator_id, db_calls.extra_access = creator_id, extra_access
    response = _client(CMT_HEALTH).get("/cases/1/access")
    assert response.status_code == 200
    assert response.json() == members


@pytest.mark.parametrize(
    ("assignee", "allowed"),
    [(ME, True), (7654321, True), (None, True), (2000001, False), (1000018, False), (9999999, False)],
)
def test_a_case_can_only_be_assigned_to_someone_with_access(assignee, allowed, db_calls, participants):
    db_calls.case_type, db_calls.extra_access = "hälsa", [7654321]
    response = _client(CMT_HEALTH).put("/cases/1/assignee", json={"assigned_to_id": assignee})
    assert _updated(db_calls, "cases") == allowed
    if not allowed:
        assert response.status_code == 422
        assert response.json() == {"detail": f"Member {assignee} has no access to this case"}
