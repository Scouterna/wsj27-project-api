"""The first-version case access rule: the health team, and nobody else, on every route.

The routes are driven on a bare app with the database calls replaced, so a
refusal is checked to come before any case is looked up or written.
"""

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import case as case_module
from app.authenctication import AuthUser, require_auth_user

CMT_HEALTH = "wsj27:cmt:support:halsa"


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
    """Records every database call; reads return nothing found."""
    calls = []

    async def fake(*args):
        calls.append(args)

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


def _routes():
    for route in case_module.router.routes:
        assert isinstance(route, APIRoute)
        path = route.path.replace("{case_id}", "1").replace("{note_id}", "1")
        for method in route.methods:
            yield method, f"/cases{path}"


@pytest.mark.parametrize(("method", "path"), list(_routes()))
@pytest.mark.parametrize(
    "roles",
    [
        [],
        ["wsj27:cmt:admin:it"],
        ["wsj27:cmt:support"],  # the function, not the health part of it
        ["wsj27:cmt:support:halsax"],  # a string prefix of nothing granted
        ["wsj27:legacy-access:Hälsa plus intern information"],  # access roles come later
        ["wsj27:al:18"],
    ],
)
def test_everyone_but_the_health_team_is_refused_before_the_database(method, path, roles, db_calls):
    response = _client(*roles).request(method, path, json={})
    assert response.status_code == 403
    assert db_calls == []


def test_the_health_team_gets_through():
    response = _client(CMT_HEALTH).get("/cases/types")
    assert response.status_code == 200
    assert response.json() == ["hälsa"]


@pytest.mark.parametrize("case_type", ["admin", "avdelning"])
def test_only_health_cases_can_be_created(case_type, db_calls):
    body = {"title": "t", "type": case_type, "troop": "18"}
    response = _client(CMT_HEALTH).post("/cases", json=body)
    assert response.status_code == 422
    assert db_calls == []


def test_secrecy_level_is_stored_as_3_whatever_is_sent(monkeypatch):
    sent = []

    async def fake_fetchrow(sql, *args):
        sent.append((sql, args))
        raise RuntimeError("stop here")

    monkeypatch.setattr(case_module, "db_fetchrow", fake_fetchrow)
    body = {"title": "t", "type": "hälsa", "troop": "18", "secrecy_level": 5}
    with pytest.raises(RuntimeError):
        _client(CMT_HEALTH).post("/cases", json=body)

    [(_, args)] = sent
    assert args[1] == 3
