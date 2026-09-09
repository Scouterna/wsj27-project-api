"""scoutnet_forms.py mints each participant's roles once, at decode time.

`access_level` used to be stored on the participant record purely so roles.py
could turn it into a role on every request to GET /roles. It is now consumed
right where it is derived instead, and the resulting roles list is stored in
its place - so a participant record never carries `access_level` at all.
"""

from app.scoutnet import ProjectCache, ScoutnetProjectData
from app.scoutnet_forms import scoutnet_forms_decoder


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
