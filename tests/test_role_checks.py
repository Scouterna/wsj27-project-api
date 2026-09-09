"""Prefix matching over the role hierarchy.

These are authorization decisions, so the interesting cases are the ones where a
plausible-looking implementation grants too much: string prefixes that are not
role prefixes, and an empty scope set read as "no restriction".

`has_role`/`has_any_role`/`role_suffixes` are format-only: they know nothing
about which role strings mean what, only how to compare them. `HEALTH` below is
a fixture for exercising that comparison, not a value anyone else depends on —
the last section keeps it honest against what `app.roles` actually mints, so a
change there doesn't leave this file testing a string nobody grants any more.
The literal role strings production code actually branches on (health access,
CMT detail roles) live in `app.participants`, and are pinned against minting in
test_participant_access.py, not here.
"""

import pytest

from app.authenctication import has_any_role, has_role, role_suffixes

CMT_IT = "wsj27:cmt:admin:it"
LEADER_38 = "wsj27:al:38"
HEALTH = "wsj27:access:Hälsa plus intern information"


# --- has_role -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("held", "required"),
    [
        ([CMT_IT], CMT_IT),  # exact
        ([CMT_IT], "wsj27:cmt:admin"),  # one level up
        ([CMT_IT], "wsj27:cmt"),  # two levels up
        ([CMT_IT], "wsj27"),  # whole namespace
        (["wsj27:cmt"], "wsj27:cmt"),  # exact, no detail roles
        ([LEADER_38, HEALTH], HEALTH),  # among several
    ],
)
def test_grants(held, required):
    assert has_role(held, required)


@pytest.mark.parametrize(
    ("held", "required"),
    [
        (["wsj27:cmt"], "wsj27:cmt:admin"),  # holding less than required
        ([CMT_IT], "wsj27:cmt:support"),  # sibling function
        ([CMT_IT], "wsj27:cmt:admin:fa"),  # sibling roll
        ([LEADER_38], "wsj27:cmt"),  # unrelated branch
        ([], "wsj27:cmt"),  # no roles at all
    ],
)
def test_denies(held, required):
    assert not has_role(held, required)


@pytest.mark.parametrize(
    ("held", "required"),
    [
        (["wsj27:cmtx"], "wsj27:cmt"),
        (["wsj27:cmtx:admin"], "wsj27:cmt"),
        (["wsj27x:cmt"], "wsj27"),
        (["wsj27:al:381"], "wsj27:al:38"),
    ],
)
def test_string_prefix_is_not_a_role_prefix(held, required):
    """The reason this is compared segment-wise and never with str.startswith().

    Every pair here passes a startswith() check and must still be denied: the
    held role is a different role that merely shares an opening substring.
    """
    assert not has_role(held, required)


def test_a_deeper_requirement_is_not_satisfied_by_a_shallower_role():
    """The transitional CMT state, stated as a rule.

    A member not yet in the mapping CSV holds plain "wsj27:cmt": enough for a
    two-part check, not enough for a four-part one. Broad access now, specific
    access once the CSV catches up.
    """
    assert has_role(["wsj27:cmt"], "wsj27:cmt")
    assert not has_role(["wsj27:cmt"], "wsj27:cmt:admin:it")


# --- has_any_role -------------------------------------------------------------


def test_any_role_matches_one_of_several():
    required = frozenset({"wsj27:bulkread", "wsj27:rolereader", "wsj27:cmt:admin"})
    assert has_any_role([CMT_IT], required)  # via the wsj27:cmt:admin prefix
    assert has_any_role(["wsj27:bulkread"], required)  # service account, exact
    assert not has_any_role(["wsj27:cmt:support:halsa"], required)
    assert not has_any_role([], required)


# --- role_suffixes ------------------------------------------------------------


def test_suffixes_are_the_scopes_under_a_prefix():
    assert role_suffixes([LEADER_38, "wsj27:al:44"], "wsj27:al") == {"38", "44"}


def test_suffixes_keep_remaining_depth_joined():
    """More than one segment below the prefix comes back as one string.

    So role_suffixes(..., "wsj27:cmt") answers "which function/roll", not just
    the next segment — the caller decides how much of it to interpret.
    """
    assert role_suffixes([CMT_IT], "wsj27:cmt") == {"admin:it"}
    assert role_suffixes([CMT_IT], "wsj27:cmt:admin") == {"it"}


def test_bare_prefix_yields_no_scope():
    """Holding "wsj27:al" with no troop names no troop.

    It must not read as "every troop", which is why the comparison requires
    strictly more segments than the prefix.
    """
    assert role_suffixes(["wsj27:al"], "wsj27:al") == set()


def test_empty_means_nothing_not_everything():
    """The failure mode callers must not write: `if not troops: allow_all()`.

    Someone with no leader role at all is indistinguishable here from a leader
    of no troops, and both mean "no access".
    """
    assert role_suffixes([CMT_IT], "wsj27:al") == set()
    assert role_suffixes([], "wsj27:al") == set()


def test_unrelated_branches_are_excluded():
    held = [LEADER_38, CMT_IT, HEALTH]
    assert role_suffixes(held, "wsj27:al") == {"38"}


def test_string_prefix_does_not_leak_a_scope():
    assert role_suffixes(["wsj27:alx:38"], "wsj27:al") == set()


# --- HEALTH as a fixture, checked against what roles.py actually mints --------


def test_health_access_role_round_trips():
    """The access role carries a Swedish sentence with spaces, not a slug.

    Only the CMT Funktion/Roll segments are slugified; access levels are used
    verbatim, so the checked constant must match that shape exactly.
    """
    assert has_role([HEALTH], HEALTH)
    assert not has_role(["wsj27:access:Ingen"], HEALTH)


def test_minted_access_role_matches_the_fixture_above():
    """`HEALTH` above is a literal, written by hand, not imported from anywhere.

    Nothing in this file imports from roles.py, so nothing enforces that this
    file's copy still matches what it actually mints — except this test. Mint
    it and compare. If someone renames the access level in the Scoutnet form,
    or edits the namespace, this fails instead of leaving every other test in
    this file quietly exercising a role nobody is granted any more.
    """
    from app.roles import roles_for_participant

    minted = roles_for_participant(
        {
            "member_type": "Kontingentledning",
            "member_no": 3073781,
            "access_level": "Hälsa plus intern information",
        }
    )
    assert HEALTH in minted
    assert has_role(minted, HEALTH)


def test_minted_roles_are_checkable_by_their_prefixes():
    """Every role this service mints must answer to a prefix check.

    Minting and checking are in different modules now; this walks one real
    participant of each kind across that seam.
    """
    from app.roles import roles_for_participant

    leader = roles_for_participant({"member_type": "Avdelningsledare", "troop": "38", "access_level": "Ingen"})
    assert role_suffixes(leader, "wsj27:al") == {"38"}

    cmt = roles_for_participant({"member_type": "Kontingentledning", "member_no": 0, "access_level": "Ingen"})
    assert has_role(cmt, "wsj27:cmt")

    deltagare = roles_for_participant({"member_type": "Deltagare", "access_level": "Ingen"})
    assert deltagare == []
    assert not has_role(deltagare, "wsj27")
