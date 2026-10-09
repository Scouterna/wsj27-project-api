"""Case access by type: `hälsa` for the health team, `cmt` for every CMT member,
`avdelning` for the leaders of the case's own troop.

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
    """Records every database call. A case lookup finds `db_calls.case_type` in `.case_troop`, if set."""
    calls = type("Calls", (list,), {"case_type": None, "case_troop": "18"})()

    async def fake(*args):
        calls.append(args)
        if args[0].startswith("SELECT type, troop FROM cases") and calls.case_type:
            return {"type": calls.case_type, "troop": calls.case_troop}

    async def fake_list(*args):
        calls.append(args)
        return []

    monkeypatch.setattr(case_module, "db_fetchrow", fake)
    monkeypatch.setattr(case_module, "db_fetch", fake_list)
    monkeypatch.setattr(case_module, "db_execute", fake)
    return calls


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
    assert case_module._SCOPE in listing[0] and listing[1:3] == (full_types, troops)
    assert tags[1:] == (full_types, troops)


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


@pytest.mark.parametrize(
    ("roles", "case_type"), [([CMT_HEALTH], "hälsa"), ([CMT_IT], "cmt"), ([LEADER_18], "avdelning")]
)
def test_secrecy_level_and_extra_access_are_stored_as_3_and_empty_whatever_is_sent(roles, case_type, monkeypatch):
    sent = []

    async def fake_fetchrow(sql, *args):
        sent.append((sql, args))
        raise RuntimeError("stop here")

    monkeypatch.setattr(case_module, "db_fetchrow", fake_fetchrow)
    with pytest.raises(RuntimeError):
        _client(*roles).post("/cases", json={**BODY, "type": case_type, "extra_access": [7654321]})

    [(_, args)] = sent
    assert args[1] == 3
    assert args[6] == []


def test_extra_access_cannot_be_changed_and_is_left_out_of_the_docs(db_calls):
    db_calls.case_type = "hälsa"
    client = _client(CMT_HEALTH)
    client.put("/cases/1/extra_access", json={"extra_access": [7654321]})
    assert not any("UPDATE" in call[0] for call in db_calls)
    assert "/cases/{case_id}/extra_access" not in client.get("/openapi.json").json()["paths"]


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
