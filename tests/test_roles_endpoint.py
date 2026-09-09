"""GET /participants/roles serves the roles stored on each participant record.

It used to call roles_for_participant() itself, fresh, on every request; that
now happens once in scoutnet_forms.py at decode time. The test below proves
the endpoint reads the stored field rather than recomputing it: the fixture
participant's stored roles are a value roles_for_participant() would never
produce from its own dict (which has no member_type), so a pass here is only
possible by reading what's stored.
"""

import copy

import pytest
from fastapi.testclient import TestClient

from app.authenctication import AuthUser, require_auth_user


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        name="Test User",
        preferred_username="scoutnet|1234567",
        given_name="Test",
        family_name="User",
        member_no="1234567",
        roles=list(roles),
    )


class _FakeProject:
    def __init__(self, participants):
        self.participants = participants


PARTICIPANTS = {
    1000000: {"member_no": 1000000, "roles": ["wsj27:cmt:support:halsa"]},
    1000001: {"member_no": 1000001, "roles": []},
}


@pytest.fixture
def client(monkeypatch):
    from app import roles as roles_module
    from app.main import app

    project = _FakeProject(copy.deepcopy(PARTICIPANTS))
    monkeypatch.setattr(roles_module, "get_single_project", lambda: project)

    test_client = TestClient(app)
    app.dependency_overrides[require_auth_user] = lambda: _user("wsj27:bulkread")
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


def test_roles_endpoint_serves_the_stored_field_not_a_recompute(client):
    response = client.get("/participants/roles")
    assert response.status_code == 200

    body = response.json()["participants"]
    assert body == {"1000000": ["wsj27:cmt:support:halsa"]}
    assert "1000001" not in body  # empty roles list omitted, same as before
