"""GET /participants/roles: minted roles from the record, plus assigned ones.

Minted roles are computed once in scoutnet_forms.py at decode time. The first
test proves the endpoint reads that stored field rather than recomputing it: the
fixture participant's roles are a value roles_for_participant() would never
produce from its own dict (which has no member_type), so a pass here is only
possible by reading what's stored.

Hand-assigned roles live in the participant's scoutnet_db object and are merged
in as the endpoint serves, so a write reaches it on the next request.
"""

import copy

from app import scoutnet_db
from app.roles import merge_stored_roles

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


ACCESS = "wsj27:access:Hälsa plus intern information"

PARTICIPANTS = {
    1000000: {"member_no": 1000000, "roles": ["wsj27:cmt:support:halsa"]},
    1000001: {"member_no": 1000001, "roles": []},
    1000002: {"member_no": 1000002, "roles": ["wsj27:cmt"], scoutnet_db.FIELD: {"roles": [ACCESS]}},
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
    assert body["1000000"] == ["wsj27:cmt:support:halsa"]
    assert "1000001" not in body  # empty roles list omitted, same as before


def test_assigned_roles_are_merged_in_as_the_endpoint_serves(client):
    participants = client.get("/participants/roles").json()["participants"]
    assert participants["1000002"] == sorted(["wsj27:cmt", ACCESS])


def test_a_member_with_only_an_assigned_role_is_listed(client):
    """Omission means "no roles", so an assigned role alone must still show up."""
    from app import roles as roles_module

    roles_module.get_single_project().participants[1000001][scoutnet_db.FIELD] = {"roles": [ACCESS]}
    participants = client.get("/participants/roles").json()["participants"]
    assert participants["1000001"] == [ACCESS]


# --- merge_stored_roles ---------------------------------------------------------


def test_merge_adds_assigned_roles_sorted():
    assert merge_stored_roles(["wsj27:cmt"], [ACCESS]) == sorted(["wsj27:cmt", ACCESS])


def test_merge_with_nothing_stored_is_the_minted_roles():
    assert merge_stored_roles(["wsj27:cmt"], None) == ["wsj27:cmt"]


@pytest.mark.parametrize(
    "stored",
    [["wsj27:al:38"], ["wsj27:cmt:admin"], ["wsj27:accessx:sneaky"], ["admin", 7, None], "wsj27:access:x"],
    ids=["troop-role", "cmt-role", "near-miss-prefix", "junk", "not-a-list"],
)
def test_merge_refuses_anything_outside_the_assigned_namespace(stored):
    """Segment-wise: "wsj27:accessx" must not pass for "wsj27:access"."""
    assert merge_stored_roles(["wsj27:cmt"], stored) == ["wsj27:cmt"]
