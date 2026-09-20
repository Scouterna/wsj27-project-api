"""The participant access policy, and the endpoints that enforce it.

Two halves. The first exercises `_troop_access` directly: it is a pure function
of a caller's roles and a troop, so the rules can be stated as a table without a
request or the Scoutnet cache anywhere near them. The second drives the real
endpoints, because a policy that is right but applied in the wrong order — or
after the record has already been handed out — is still a leak.

The cases worth writing are the ones where a plausible implementation gives away
too much: an Avdelningsledare with an access role reaching past their own troop,
a caller learning that a record exists by being refused differently, and a
"basic" response quietly emptying the cache for the caller entitled to "full".
"""

import pytest
from fastapi.testclient import TestClient

from app.authenctication import AuthUser, require_auth_user
from app.participants import BASIC_ACCESS, FULL_ACCESS, NO_ACCESS, _troop_access

LEADER_18 = "wsj27:al:18"
CMT_PROGRAM = "wsj27:cmt:program:medlem"
CMT_HEALTH = "wsj27:cmt:support:halsa"
HEALTH_ACCESS = "wsj27:access:Hälsa plus intern information"


def _user(*roles: str) -> AuthUser:
    return AuthUser(
        name="Test User",
        preferred_username="scoutnet|1234567",
        given_name="Test",
        family_name="User",
        member_no="1234567",
        roles=list(roles),
    )


# --- The policy ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "troop", "expected"),
    [
        # Avdelningsledare: everything about their own troop, nothing anywhere else.
        ([LEADER_18], "18", FULL_ACCESS),
        ([LEADER_18], "19", NO_ACCESS),
        ([LEADER_18], None, NO_ACCESS),  # a member-type listing is nobody's troop
        (["wsj27:al:18", "wsj27:al:19"], "19", FULL_ACCESS),  # two troops, both theirs
        # Kontingentledning: the whole contingent, health only when granted.
        ([CMT_PROGRAM], "18", BASIC_ACCESS),
        ([CMT_PROGRAM], None, BASIC_ACCESS),
        (["wsj27:cmt"], "18", BASIC_ACCESS),  # not yet in the Funktion/Roll CSV
        ([CMT_HEALTH], "18", FULL_ACCESS),
        ([CMT_PROGRAM, HEALTH_ACCESS], "18", FULL_ACCESS),  # health by access role
        # Neither group.
        ([], "18", NO_ACCESS),
        (["wsj27:bulkread"], "18", NO_ACCESS),  # a service account is not a person
    ],
)
def test__troop_access(roles, troop, expected):
    assert _troop_access(_user(*roles), troop) == expected


def test_a_leaders_access_role_does_not_reach_past_their_troop():
    """The rule that is easiest to get wrong, stated on its own.

    Health authorisation says *how much* of someone a caller may see, never
    *who*. A leader who holds it still sees only their own troop — if the two
    were ORed together instead of layered, this would come back FULL.
    """
    leader = _user(LEADER_18, HEALTH_ACCESS)
    assert _troop_access(leader, "18") == FULL_ACCESS
    assert _troop_access(leader, "19") == NO_ACCESS


def test_leader_and_cmt_grants_add_up():
    """Someone can be both, and then each rule applies where it applies."""
    both = _user(LEADER_18, CMT_PROGRAM)
    assert _troop_access(both, "18") == FULL_ACCESS  # own troop, as a leader
    assert _troop_access(both, "19") == BASIC_ACCESS  # everyone else, as CMT


def test_a_troopless_leader_role_grants_no_troop():
    """`wsj27:al` with no troop names no troop, and must not read as all of them."""
    assert _troop_access(_user("wsj27:al"), "18") == NO_ACCESS


@pytest.mark.parametrize("held", ["wsj27:alx:18", "wsj27:al:181"])
def test_string_prefixes_do_not_grant_a_troop(held):
    """Both of these pass a startswith() check against troop 18 and must not match."""
    assert _troop_access(_user(held), "18") == NO_ACCESS


def test_a_participant_with_no_troop_is_reachable_only_contingent_wide():
    """CMT and IST records carry no troop number, and an empty troop is not a scope.

    The individual endpoint passes the participant's own `troop` field straight
    in, so this is the value it hands over for them. An empty string must not
    match a leader role that carries no troop either.
    """
    assert _troop_access(_user(LEADER_18), "") == NO_ACCESS
    assert _troop_access(_user("wsj27:al"), "") == NO_ACCESS
    assert _troop_access(_user(CMT_PROGRAM), "") == BASIC_ACCESS


# --- The role strings, against what roles.py mints ----------------------------


def test_the_cmt_health_role_is_what_roles_py_would_mint():
    """`wsj27:cmt:support:halsa` is a literal here but built there, from a CSV.

    The Funktion/Roll segments are slugified — "Hälsa" loses its accent — while
    the access role beside it keeps its Swedish sentence verbatim. Getting that
    asymmetry wrong either way silently locks out the Support health people, so
    mint the role the way roles.py does and compare.
    """
    from app.participants import HEALTH_ROLES
    from app.roles import ROLE_CMT, ROLE_NAMESPACE, _slug

    assert f"{ROLE_NAMESPACE}:{ROLE_CMT}:{_slug('Support')}:{_slug('Hälsa')}" in HEALTH_ROLES


def test_the_health_access_role_is_what_roles_py_would_mint():
    from app.roles import roles_for_participant

    minted = roles_for_participant(
        {"member_type": "Kontingentledning", "member_no": 0, "access_level": "Hälsa plus intern information"}
    )
    assert _troop_access(_user(*minted), "18") == FULL_ACCESS


def test_a_plain_participants_minted_roles_grant_nothing():
    from app.roles import roles_for_participant

    minted = roles_for_participant({"member_type": "Deltagare", "troop": "18", "access_level": "Ingen"})
    assert _troop_access(_user(*minted), "18") == NO_ACCESS


# --- The endpoints ------------------------------------------------------------

# Two participants in different troops, plus a CMT member with no troop. Health
# answers live in forms_data, which is the field the levels are about.
PARTICIPANTS = {
    1000018: {
        "member_no": 1000018,
        "name": "Ada Troop18",
        "troop": "18",
        "member_type": "Deltagare",
        "email": "ada@example.org",
        "contact_info": {"Anhörig": "Ada's mum"},
        "forms_data": {"Hälsa": {"Allergier": "Nötter"}},
    },
    1000019: {
        "member_no": 1000019,
        "name": "Bo Troop19",
        "troop": "19",
        "member_type": "Deltagare",
        "email": "bo@example.org",
        "contact_info": {"Anhörig": "Bo's dad"},
        "forms_data": {"Hälsa": {"Allergier": "Inga"}},
    },
    1000180: {
        "member_no": 1000180,
        "name": "Dag Ledare18",
        "troop": "18",
        "member_type": "Avdelningsledare",
        "email": "dag@example.org",
        "contact_info": {"Anhörig": "Dag's wife"},
        "forms_data": {"Hälsa": {"Allergier": "Skaldjur"}},
    },
    1000000: {
        "member_no": 1000000,
        "name": "Cee Cmt",
        "troop": "",
        "member_type": "Kontingentledning",
        "email": "cee@example.org",
        "contact_info": {"Anhörig": "Cee's partner"},
        "forms_data": {"Hälsa": {"Allergier": "Inga"}},
    },
}


class _FakeProject:
    def __init__(self, participants):
        self.participants = participants


@pytest.fixture
def client(monkeypatch):
    """A TestClient whose caller is set per-test by `client.as_user(...)`.

    The participant cache is replaced with a deep copy of PARTICIPANTS, so a
    test that mutates it — which is the bug one of these tests is here to catch
    — cannot reach the next test.
    """
    import copy

    from app import participants as participants_module
    from app.main import app

    project = _FakeProject(copy.deepcopy(PARTICIPANTS))
    monkeypatch.setattr(participants_module, "get_single_project", lambda: project)

    test_client = TestClient(app)

    def as_user(*roles: str):
        app.dependency_overrides[require_auth_user] = lambda: _user(*roles)
        return test_client

    test_client.as_user = as_user
    test_client.project = project
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


def _by_name(response):
    return {p["name"]: p for p in response.json()}


def test_leader_reads_their_own_troop_in_full(client):
    response = client.as_user(LEADER_18).get("/participants/troopinfo/18?infolevel=full")
    assert response.status_code == 200
    assert _by_name(response)["Ada Troop18"]["forms_data"]


@pytest.mark.parametrize("infolevel", ["name", "basic", "full"])
def test_leader_gets_nothing_from_another_troop(client, infolevel):
    """Not even a name: a 404 at every level, so no level confirms who is there."""
    response = client.as_user(LEADER_18).get(f"/participants/troopinfo/19?infolevel={infolevel}")
    assert response.status_code == 404


def test_an_unauthorised_troop_looks_exactly_like_a_missing_one(client):
    """The refusal must not be a lookup oracle.

    Troop 19 exists and troop 77 does not; a leader of troop 18 has to get the
    same answer for both, body included, or they can map the contingent by
    reading the difference.
    """
    leader = client.as_user(LEADER_18)
    exists = leader.get("/participants/troopinfo/19")
    missing = leader.get("/participants/troopinfo/77")
    assert exists.status_code == missing.status_code == 404
    assert exists.json() == missing.json()


def test_cmt_without_health_is_refused_full_rather_than_downgraded(client):
    """A 403, not a 200 carrying basic data.

    Downgrading silently would leave a client unable to tell "no health answers
    recorded" from "you may not see them" — and the second is a very different
    thing to show a user.
    """
    response = client.as_user(CMT_PROGRAM).get("/participants/troopinfo/18?infolevel=full")
    assert response.status_code == 403


def test_cmt_without_health_reads_basic_everywhere(client):
    response = client.as_user(CMT_PROGRAM).get("/participants/troopinfo/19?infolevel=basic")
    assert response.status_code == 200
    assert response.json()[0]["name"] == "Bo Troop19"
    assert "forms_data" not in response.json()[0]
    assert response.json()[0]["email"] == "bo@example.org"  # basic still carries contact details


def test_cmt_with_health_reads_full_everywhere(client):
    response = client.as_user(CMT_HEALTH).get("/participants/troopinfo/19?infolevel=full")
    assert response.status_code == 200
    assert response.json()[0]["forms_data"]


def test_member_type_listing_is_cmt_only(client):
    assert client.as_user(CMT_PROGRAM).get("/participants/troopinfo/cmt").status_code == 200
    assert client.as_user(LEADER_18).get("/participants/troopinfo/cmt").status_code == 404


def test_basic_does_not_strip_the_cache_for_the_next_caller(client):
    """The regression this refactor exists for.

    The endpoint used to pop "forms_data" out of the shared cache entry, so the
    first basic request deleted that participant's health answers for everyone
    until the next Scoutnet refresh. Ask basic first, then full, and the health
    data still has to be there.
    """
    assert client.as_user(CMT_HEALTH).get("/participants/troopinfo/18?infolevel=basic").status_code == 200
    assert client.project.participants[1000018]["forms_data"], "the cached record was mutated"

    full = client.as_user(CMT_HEALTH).get("/participants/troopinfo/18?infolevel=full")
    assert full.json()[0]["forms_data"]


def test_individual_follows_the_participants_own_troop(client):
    leader = client.as_user(LEADER_18)
    assert leader.get("/participants/individual/1000018?infolevel=full").status_code == 200
    assert leader.get("/participants/individual/1000019?infolevel=full").status_code == 404


def test_individual_hides_existence_from_an_unauthorised_caller(client):
    """Member 1000019 exists and 9999999 does not; a leader of 18 sees no difference."""
    leader = client.as_user(LEADER_18)
    exists = leader.get("/participants/individual/1000019")
    missing = leader.get("/participants/individual/9999999")
    assert exists.status_code == missing.status_code == 404
    assert exists.json() == missing.json()


def test_individual_refuses_full_without_health(client):
    response = client.as_user(CMT_PROGRAM).get("/participants/individual/1000018?infolevel=full")
    assert response.status_code == 403


def test_individual_name_level_needs_basic_access(client):
    """A name is participant data: a caller with no access to someone gets 404, not a name."""
    assert client.as_user(CMT_PROGRAM).get("/participants/individual/1000018?infolevel=name").json() == {
        "name": "Ada Troop18"
    }
    assert client.as_user(LEADER_18).get("/participants/individual/1000019?infolevel=name").status_code == 404


def test_individual_basic_does_not_mutate_the_cache(client):
    assert client.as_user(CMT_HEALTH).get("/participants/individual/1000018?infolevel=basic").status_code == 200
    assert client.project.participants[1000018]["forms_data"], "the cached record was mutated"


# --- Leaders' own details are not troop business ------------------------------


@pytest.mark.parametrize("infolevel", ["basic", "full"])
def test_a_leader_sees_fellow_leaders_without_contact_or_health_data(client, infolevel):
    """The young people in full, the other adults in the troop stripped back.

    Both rows come out of the same request, so this is also the case a
    per-request decision would get wrong: the caller's grant is the same for
    both, and only the participant's own member_type separates them.
    """
    response = client.as_user(LEADER_18).get(f"/participants/troopinfo/18?infolevel={infolevel}")
    assert response.status_code == 200
    rows = _by_name(response)

    assert rows["Dag Ledare18"]["member_type"] == "Avdelningsledare"
    assert "contact_info" not in rows["Dag Ledare18"]
    assert "forms_data" not in rows["Dag Ledare18"]
    # Still a real record otherwise — this strips two leaves, it does not hide him.
    assert rows["Dag Ledare18"]["email"] == "dag@example.org"

    assert rows["Ada Troop18"]["contact_info"]
    assert ("forms_data" in rows["Ada Troop18"]) is (infolevel == "full")


def test_a_leader_reading_a_fellow_leader_individually_is_stripped_too(client):
    """Not just filtered out of listings — the single-record route strips as well."""
    response = client.as_user(LEADER_18).get("/participants/individual/1000180?infolevel=full")
    assert response.status_code == 200
    assert "contact_info" not in response.json()
    assert "forms_data" not in response.json()


def test_cmt_with_health_does_see_a_leaders_details(client):
    """The one caller the rule lets through, by both routes."""
    cmt = client.as_user(CMT_HEALTH)
    listed = _by_name(cmt.get("/participants/troopinfo/18?infolevel=full"))["Dag Ledare18"]
    assert listed["contact_info"] and listed["forms_data"]

    single = cmt.get("/participants/individual/1000180?infolevel=full").json()
    assert single["contact_info"] and single["forms_data"]


def test_cmt_without_health_sees_no_leader_contact_details(client):
    """Basic access to a leader is less than basic access to a participant.

    contact_info rides along with "basic" for everyone else, so this is the row
    where the rule actually costs a CMT member something they otherwise have.
    """
    rows = _by_name(client.as_user(CMT_PROGRAM).get("/participants/troopinfo/18?infolevel=basic"))
    assert "contact_info" not in rows["Dag Ledare18"]
    assert rows["Ada Troop18"]["contact_info"]


def test_being_a_leader_of_the_troop_does_not_unlock_leader_details(client):
    """A leader who also holds the health access role still gets nothing extra.

    This is the check that would pass wrongly if the flag were computed from the
    troop being read rather than from the contingent-wide grant: the caller has
    FULL over troop 18, and must still not have it over the adults in it.
    """
    response = client.as_user(LEADER_18, HEALTH_ACCESS).get("/participants/troopinfo/18?infolevel=full")
    rows = _by_name(response)
    assert rows["Ada Troop18"]["forms_data"]  # health role does work, for the young people
    assert "forms_data" not in rows["Dag Ledare18"]
    assert "contact_info" not in rows["Dag Ledare18"]


def test_a_leaders_own_record_is_stripped_like_any_other_leaders(client):
    """No self-exception: "who is asking" is not part of this rule.

    Dag reading himself gets the same stripped record his colleague does. Worth
    stating because it is the one case where the rule reads oddly — say so here
    rather than let it look like an oversight.
    """
    dag = client.as_user(LEADER_18)  # member_no 1234567 in the fixture, but the rule ignores that
    response = dag.get("/participants/individual/1000180?infolevel=full")
    assert "contact_info" not in response.json()


def test_a_leaders_name_level_is_unaffected(client):
    """ "name" never carried either field, so there is nothing for the rule to take."""
    rows = _by_name(client.as_user(LEADER_18).get("/participants/troopinfo/18?infolevel=name"))
    assert rows["Dag Ledare18"] == {"member_no": 1000180, "name": "Dag Ledare18"}


# --- Kontingentledning's own health answers -----------------------------------
#
# A narrower rule than the leaders' one above: it drops forms_data only, and the
# one role that unlocks it is the per-person grant from the Scoutnet form. The
# Support function's health role passes every other health check in this file
# and must not pass this one.
#
# TEMPORARY (2026-09-20): the field is withheld from *every* caller for now, so
# these tests run over both role sets and the test for the permanent rule is
# parked as xfail(strict) below. Putting the role check back in `_withheld()`
# fails exactly three cases, which is the restore checklist: drop the xfail
# marker (strict, so it reports as a failure the moment it passes), and drop
# HEALTH_ACCESS from CMT_HEALTH_CALLERS so only CMT_HEALTH is asserted withheld.

CMT_HEALTH_CALLERS = [(CMT_HEALTH,), (CMT_PROGRAM, HEALTH_ACCESS)]
TEMPORARY_BLOCK = "TEMPORARY (2026-09-20): CMT health answers are withheld from every caller"


@pytest.mark.parametrize("roles", CMT_HEALTH_CALLERS)
@pytest.mark.parametrize("infolevel", ["basic", "full"])
def test_cmt_health_answers_are_withheld(client, infolevel, roles):
    response = client.as_user(*roles).get(f"/participants/troopinfo/cmt?infolevel={infolevel}")
    assert response.status_code == 200
    row = _by_name(response)["Cee Cmt"]
    assert "forms_data" not in row
    # Only that one field: the rest of the record is as full as the level allows.
    assert row["contact_info"] and row["email"] == "cee@example.org"


@pytest.mark.xfail(strict=True, reason=TEMPORARY_BLOCK)
def test_cmt_health_answers_reach_the_internal_information_role(client):
    """The permanent rule, parked: only this role gets the answers back."""
    response = client.as_user(CMT_PROGRAM, HEALTH_ACCESS).get("/participants/troopinfo/cmt?infolevel=full")
    assert response.status_code == 200
    assert _by_name(response)["Cee Cmt"]["forms_data"]


@pytest.mark.parametrize("roles", CMT_HEALTH_CALLERS)
def test_individual_withholds_cmt_health_answers_too(client, roles):
    """Silently — a 200 with the field gone, not a 403, as for a leader's record."""
    response = client.as_user(*roles).get("/participants/individual/1000000?infolevel=full")
    assert response.status_code == 200
    assert "forms_data" not in response.json()


def test_withholding_cmt_health_answers_does_not_mutate_the_cache(client):
    assert client.as_user(CMT_HEALTH).get("/participants/individual/1000000?infolevel=full").status_code == 200
    assert client.project.participants[1000000]["forms_data"], "the cached record was mutated"


def test_the_cmt_rule_leaves_everyone_elses_health_answers_alone(client):
    """It is keyed on the participant's member_type, not on the caller's roles."""
    response = client.as_user(CMT_HEALTH).get("/participants/troopinfo/18?infolevel=full")
    rows = _by_name(response)
    assert rows["Ada Troop18"]["forms_data"]
    assert rows["Dag Ledare18"]["forms_data"]  # an Avdelningsledare, unlocked by the health role


def test_the_internal_information_role_keeps_its_other_grants(client):
    """Withholding one field must not cost the role everything else it grants.

    Neutering INTERNAL_INFO_ROLE itself is the quicker way to switch the CMT
    rule off and the wrong one: the same constant is half of HEALTH_ROLES, so
    its holders would drop to basic over the *whole* contingent. Stated here so
    that shortcut fails a test rather than a user.
    """
    response = client.as_user(CMT_PROGRAM, HEALTH_ACCESS).get("/participants/troopinfo/18?infolevel=full")
    assert response.status_code == 200  # a 403 here means the role stopped granting "full"
    assert _by_name(response)["Ada Troop18"]["forms_data"]
