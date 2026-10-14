"""CMT Funktion/Roll: parsing Scoutnet question 120008, and merging a trailing "PL" into its Roll."""

import pytest

from app.roles import _drop_pl_suffix, roles_for_participant


def _cmt_roles(value: str) -> list[str]:
    return roles_for_participant({"member_type": "Kontingentledning", "access_level": "Ingen", "cmt_role": value})


@pytest.mark.parametrize(
    ("roll", "expected"),
    [
        ("Hälsa PL", "Hälsa"),
        ("IST-support PL", "IST-support"),
        ("Avdelningssupport PL", "Avdelningssupport"),
        ("Hälsa pl", "Hälsa"),  # case-insensitive
        ("Hälsa", "Hälsa"),  # nothing to strip
        ("Projektledare", "Projektledare"),  # "PL" only as a substring, not a trailing word
        ("PL Food house", "PL Food house"),  # PL is a prefix here, not a suffix - left alone
    ],
)
def test_drop_pl_suffix(roll, expected):
    assert _drop_pl_suffix(roll) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Support | Avdelningssupport", "wsj27:cmt:support:avdelningssupport"),
        ("Admin | FA/CET", "wsj27:cmt:admin:fa-cet"),  # why the separator is not "/"
        ("Support|Avdelningssupport", "wsj27:cmt:support:avdelningssupport"),  # spacing is not significant
        ("  Support  |  Avdelningssupport  ", "wsj27:cmt:support:avdelningssupport"),
        ("Admin | Food House", "wsj27:cmt:admin:food-house"),
        ("Admin | Food house", "wsj27:cmt:admin:food-house"),  # case is not significant either
        ("Support", "wsj27:cmt:support"),  # Funktion only
        ("Support |", "wsj27:cmt:support"),
        ("", "wsj27:cmt"),  # not set yet
        (" | Hälsa", "wsj27:cmt"),  # a Roll without a Funktion grants nothing more
    ],
)
def test_cmt_role_from_question(value, expected):
    assert _cmt_roles(value) == [expected]


def test_no_cmt_role_question_means_plain_cmt():
    assert roles_for_participant({"member_type": "Kontingentledning", "access_level": "Ingen"}) == ["wsj27:cmt"]


def test_a_colon_in_the_field_cannot_fake_a_deeper_role():
    """The field is edited by hand; whatever is typed must stay within its two segments."""
    assert _cmt_roles("Support:Hälsa | x:y") == ["wsj27:cmt:support-halsa:x-y"]


def test_pl_and_plain_roll_mint_the_same_role():
    """Someone with "Hälsa PL" and someone with "Hälsa" get an identical role,
    not two roles that happen to look similar."""
    assert _cmt_roles("Support | Hälsa") == _cmt_roles("Support | Hälsa PL") == ["wsj27:cmt:support:halsa"]


def test_pl_as_a_title_prefix_is_not_merged_away():
    """ "PL Food house" is its own title, not "Food house" plus a PL qualifier.

    Only a trailing "PL" is a qualifier; the same two letters leading the Roll
    must not collapse it into the base title.
    """
    assert _cmt_roles("Admin | Food house") != _cmt_roles("Admin | PL Food house")


def test_only_cmt_members_read_the_field():
    """120008 is on form 47115, so a leader could have a value too; it must not
    turn them into CMT."""
    leader = {"member_type": "Avdelningsledare", "troop": "18", "access_level": "Ingen", "cmt_role": "Admin | FA"}
    assert roles_for_participant(leader) == ["wsj27:al:18"]
