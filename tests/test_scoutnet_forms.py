"""scoutnet_forms.py mints each participant's roles once, at decode time.

`access_level` used to be stored on the participant record purely so roles.py
could turn it into a role on every request to GET /roles. It is now consumed
right where it is derived instead, and the resulting roles list is stored in
its place - so a participant record never carries `access_level` at all.
"""

import pytest

from app import scoutnet_db
from app.scoutnet import ProjectCache, ScoutnetProjectData
from app.scoutnet_forms import _apply_scout_db, scoutnet_forms_decoder


def _raw_participant(**overrides) -> dict:
    base = {
        "confirmed": True,
        "cancelled": False,
        "questions": {"90951": "64031"},  # Kontingentledning
        "member_no": 1000000,
        "date_of_birth": "2000-01-01",
        "sex": "1",
        "primary_membership_info": None,
        "primary_email": "cee@example.org",
        "contact_info": {},
        "first_name": "Cee",
        "last_name": "Cmt",
    }
    base.update(overrides)
    return base


def test_decoder_stores_minted_roles_instead_of_access_level():
    project = ScoutnetProjectData(
        project_id=1,
        project_name="Test project",
        groups={},
        participants={"participants": {"1": _raw_participant()}, "labels": {"sex": {}}},
        questions={"questions": {}},
    )
    cache = ProjectCache()

    scoutnet_forms_decoder([project], cache)

    partdata = cache.projects[1].participants[1000000]
    # No CMT Funktion/Roll CSV loaded in this test, so this member falls back
    # to the plain wsj27:cmt role - the same fallback roles_for_participant()
    # applies when a member is missing from the CSV.
    assert partdata["roles"] == ["wsj27:cmt"]
    assert "access_level" not in partdata


# --- The app's own stored values (scoutnet_db) ---
#
# Storing the object is scoutnet_db.py's job and is tested there. These cover
# the other half: what the decoder lets a stored key do to a participant record.


def _decoded(**overrides) -> dict:
    base = {"member_no": 1000000, "member_type": "Deltagare", "roles": ["wsj27:deltagare"]}
    base.update(overrides)
    return base


def test_promoted_keys_are_lifted_onto_the_record():
    partdata = _decoded()
    _apply_scout_db(partdata, {"patrol": "Falken", "avatar_url": "https://x/y"})

    assert partdata["patrol"] == "Falken"
    assert partdata["avatar_url"] == "https://x/y"
    assert partdata[scoutnet_db.FIELD] == {"patrol": "Falken", "avatar_url": "https://x/y"}


def test_a_blank_stored_value_does_not_blank_out_the_record():
    partdata = _decoded(patrol="Falken")
    _apply_scout_db(partdata, {"patrol": ""})
    assert partdata["patrol"] == "Falken"


def test_stored_roles_are_added_to_the_computed_ones_without_duplicating():
    partdata = _decoded(roles=["wsj27:deltagare", "wsj27:al:12"])
    _apply_scout_db(partdata, {"roles": ["wsj27:extra:foo", "wsj27:al:12"]})
    assert partdata["roles"] == ["wsj27:deltagare", "wsj27:al:12", "wsj27:extra:foo"]


@pytest.mark.parametrize(
    "stored",
    [{"roles": ["admin", 7, None]}, {"roles": "wsj27:extra:foo"}],
    ids=["wrong-namespace-or-type", "not-a-list"],
)
def test_roles_outside_the_wsj27_namespace_are_dropped(stored):
    """A role string is an authorisation decision, so this field cannot mint one freely."""
    partdata = _decoded()
    _apply_scout_db(partdata, stored)
    assert partdata["roles"] == ["wsj27:deltagare"]


def test_a_member_with_nothing_stored_still_gets_the_field():
    partdata = _decoded()
    _apply_scout_db(partdata, {})
    assert partdata[scoutnet_db.FIELD] == {}
    assert partdata["roles"] == ["wsj27:deltagare"]
