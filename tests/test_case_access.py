"""Case access by type: `hälsa` for the health team, `cmt` for every CMT member.

The routes are driven on a bare app with the database calls replaced, so a
refusal can be checked to come before any case is looked up or written, and a
case of another type to look exactly like a missing one.
"""

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
    """Records every database call. A case lookup finds a case of `db_calls.case_type`, if set."""
    calls = type("Calls", (list,), {"case_type": None})()

    async def fake(*args):
        calls.append(args)
        if args[0].startswith("SELECT type FROM cases") and calls.case_type:
            return {"type": calls.case_type}

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


@pytest.mark.parametrize(("method", "path"), list(_routes()))
@pytest.mark.parametrize(
    "roles",
    [
        [],
        ["wsj27:cmtx:admin"],  # a string prefix of wsj27:cmt
        ["wsj27:legacy-access:Hälsa plus intern information"],  # access roles come later
        ["wsj27:al:18"],
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
    ],
)
def test_types_follow_roles(roles, types):
    response = _client(*roles).get("/cases/types")
    assert response.status_code == 200
    assert response.json() == types


@pytest.mark.parametrize(("method", "path"), list(_routes(case_routes_only=True)))
def test_a_health_case_looks_missing_to_other_cmt_members(method, path, db_calls):
    db_calls.case_type = "hälsa"
    response = _client(CMT_IT).request(method, path, json={})
    assert response.status_code == 404
    assert response.json() == {"detail": "Case not found"}
    assert len(db_calls) == 1  # the type lookup, and nothing after it


def test_listing_and_tags_are_limited_to_the_callers_types(db_calls):
    _client(CMT_IT).get("/cases")
    _client(CMT_IT).get("/cases/tags")
    [listing, tags] = db_calls
    assert "c.type = ANY($1)" in listing[0] and listing[1] == ["cmt"]
    assert tags[1] == ["cmt"]


def test_cmt_members_cannot_create_health_cases(db_calls):
    body = {"title": "t", "type": "hälsa", "troop": "18", "secrecy_level": 5}
    response = _client(CMT_IT).post("/cases", json=body)
    assert response.status_code == 403
    assert db_calls == []


def test_unknown_case_types_are_rejected(db_calls):
    body = {"title": "t", "type": "avdelning", "troop": "18", "secrecy_level": 5}
    response = _client(CMT_HEALTH).post("/cases", json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid case type: avdelning"}
    assert db_calls == []


@pytest.mark.parametrize(("roles", "case_type"), [([CMT_HEALTH], "hälsa"), ([CMT_IT], "cmt")])
def test_secrecy_level_is_stored_as_3_whatever_is_sent(roles, case_type, monkeypatch):
    sent = []

    async def fake_fetchrow(sql, *args):
        sent.append((sql, args))
        raise RuntimeError("stop here")

    monkeypatch.setattr(case_module, "db_fetchrow", fake_fetchrow)
    body = {"title": "t", "type": case_type, "troop": "18", "secrecy_level": 5}
    with pytest.raises(RuntimeError):
        _client(*roles).post("/cases", json=body)

    [(_, args)] = sent
    assert args[1] == 3
