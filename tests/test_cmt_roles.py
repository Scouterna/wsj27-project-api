"""CMT Funktion/Roll: loading the CSV, and merging a trailing "PL" into its Roll."""

import pytest

from app import roles
from app.roles import _drop_pl_suffix, load_cmt_roles, roles_for_participant


@pytest.fixture(autouse=True)
def _reset_cmt_details():
    """Isolate `_cmt_details` from whatever another test file loaded into it.

    load_cmt_roles() always replaces the whole module-level map in one go, so
    ending on an empty one here can't leak state into tests that run after this
    file - the next load_cmt_roles() call anywhere just repopulates it fresh.
    """
    roles._cmt_details.clear()
    yield
    roles._cmt_details.clear()


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


def test_pl_and_plain_roll_mint_the_same_role(tmp_path):
    """The actual request: someone with "Hälsa PL" and someone with "Hälsa" get
    an identical role, not two roles that happen to look similar.

    Written through the real CSV loader rather than calling _drop_pl_suffix
    directly, so this also exercises the path a live deployment takes.
    """
    csv_path = tmp_path / "cmt-roles.csv"
    csv_path.write_text(
        "Medlemsnummer,Funktion,Roll\n1000001,Support,Hälsa\n1000002,Support,Hälsa PL\n",
        encoding="utf-8",
    )
    load_cmt_roles(csv_path)

    plain = roles_for_participant({"member_type": "Kontingentledning", "member_no": 1000001, "access_level": "Ingen"})
    pl = roles_for_participant({"member_type": "Kontingentledning", "member_no": 1000002, "access_level": "Ingen"})
    assert plain == pl == ["wsj27:cmt:support:halsa"]


def test_pl_as_a_title_prefix_is_not_merged_away(tmp_path):
    """ "PL Food house" is its own title, not "Food house" plus a PL qualifier.

    Only a trailing "PL" is a qualifier; the same two letters leading the Roll
    must not collapse it into the base title.
    """
    csv_path = tmp_path / "cmt-roles.csv"
    csv_path.write_text(
        "Medlemsnummer,Funktion,Roll\n1000003,Admin,Food house\n1000004,Admin,PL Food house\n",
        encoding="utf-8",
    )
    load_cmt_roles(csv_path)

    plain = roles_for_participant({"member_type": "Kontingentledning", "member_no": 1000003, "access_level": "Ingen"})
    pl_prefixed = roles_for_participant(
        {"member_type": "Kontingentledning", "member_no": 1000004, "access_level": "Ingen"}
    )
    assert plain != pl_prefixed
