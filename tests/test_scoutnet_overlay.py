"""SCOUTNET_OVERLAY keeps writes out of the real Scoutnet project.

Outside prod, set_answer() must never reach Scoutnet, yet what was written must
read back, survive a restart, and give way to a real change made in Scoutnet.
Nothing here makes a request: httpx is replaced by something that fails the test.
"""

import asyncio
import json

import pytest

from app import scoutnet, scoutnet_db

MEMBER = 3073781
PATRULL = "120104"


@pytest.fixture
def overlay(monkeypatch, tmp_path):
    """Overlay on, an empty file in a temp dir, and no way to reach Scoutnet."""
    monkeypatch.setattr(scoutnet.settings, "SCOUTNET_OVERLAY", True)
    monkeypatch.setattr(scoutnet, "OVERLAY_FILE", tmp_path / "scoutnet_overlay.json")
    monkeypatch.setattr(scoutnet, "_overlay", {})
    monkeypatch.setattr(scoutnet, "_fetched_answers", {})

    def _no_requests(*args, **kwargs):
        pytest.fail("the overlay let a write through to Scoutnet")

    monkeypatch.setattr(scoutnet.httpx, "AsyncClient", _no_requests)
    return tmp_path / "scoutnet_overlay.json"


def _fetch(patrol=None, member_no=MEMBER):
    """One fetch's raw participants, as Scoutnet returns them (and the dict to pass on)."""
    return {str(member_no): {"member_no": member_no, "questions": {PATRULL: patrol, "84942": "58000"}}}


def _patrol_after(pdata, member_no=MEMBER):
    scoutnet._apply_overlay(pdata)
    return pdata[str(member_no)]["questions"][PATRULL]


def test_a_write_reads_back_on_the_next_fetch(overlay):
    scoutnet._apply_overlay(_fetch("Falken"))
    asyncio.run(scoutnet.set_answer(MEMBER, PATRULL, "Örnen"))
    assert _patrol_after(_fetch("Falken")) == "Örnen"


def test_a_change_in_scoutnet_wins_over_the_overlay(overlay):
    """The IST job, or a hand-edit, filled in the patrol after we wrote ours."""
    scoutnet._apply_overlay(_fetch("Falken"))
    asyncio.run(scoutnet.set_answer(MEMBER, PATRULL, "Örnen"))

    assert _patrol_after(_fetch("Vargen")) == "Vargen"
    assert scoutnet._overlay == {}
    assert _patrol_after(_fetch("Falken")) == "Falken"  # And it stays gone


def test_an_unanswered_question_counts_as_empty(overlay):
    """Scoutnet sends null for an unanswered question, and "" once it is cleared."""
    scoutnet._apply_overlay(_fetch(None))
    asyncio.run(scoutnet.set_answer(MEMBER, PATRULL, "Örnen"))
    assert _patrol_after(_fetch("")) == "Örnen"


def test_an_entry_scoutnet_now_agrees_with_is_dropped(overlay):
    scoutnet._apply_overlay(_fetch("Falken"))
    asyncio.run(scoutnet.set_answer(MEMBER, PATRULL, "Örnen"))
    assert _patrol_after(_fetch("Örnen")) == "Örnen"
    assert scoutnet._overlay == {}


def test_a_second_write_keeps_the_first_ones_base(overlay):
    """The fetched answer already carries our first value; the base is still Scoutnet's."""
    pdata = _fetch("Falken")
    scoutnet._apply_overlay(pdata)
    asyncio.run(scoutnet.set_answer(MEMBER, PATRULL, "Örnen"))
    scoutnet._apply_overlay(_fetch("Falken"))
    asyncio.run(scoutnet.set_answer(MEMBER, PATRULL, "Vargen"))

    assert scoutnet._overlay[str(MEMBER)][PATRULL] == {"value": "Vargen", "base": "Falken"}


def test_a_write_before_any_fetch_takes_its_base_from_the_next_one(overlay):
    """A startup served from the disk cache has no raw answers to compare with."""
    asyncio.run(scoutnet.set_answer(MEMBER, PATRULL, "Örnen"))
    assert scoutnet._overlay[str(MEMBER)][PATRULL]["base"] is None

    assert _patrol_after(_fetch("Falken")) == "Örnen"
    assert scoutnet._overlay[str(MEMBER)][PATRULL]["base"] == "Falken"


def test_every_write_is_saved_and_survives_a_restart(overlay, monkeypatch):
    scoutnet._apply_overlay(_fetch("Falken"))
    asyncio.run(scoutnet.set_answer(MEMBER, PATRULL, "Örnen"))
    assert json.loads(overlay.read_text()) == {str(MEMBER): {PATRULL: {"value": "Örnen", "base": "Falken"}}}

    monkeypatch.setattr(scoutnet, "_overlay", {})
    scoutnet._load_overlay()
    assert _patrol_after(_fetch("Falken")) == "Örnen"


def test_a_dropped_entry_is_also_dropped_from_the_file(overlay):
    scoutnet._apply_overlay(_fetch("Falken"))
    asyncio.run(scoutnet.set_answer(MEMBER, PATRULL, "Örnen"))
    scoutnet._apply_overlay(_fetch("Vargen"))
    assert json.loads(overlay.read_text()) == {}


def test_no_file_or_an_unreadable_one_starts_empty(overlay):
    scoutnet._load_overlay()
    assert scoutnet._overlay == {}
    overlay.write_text("{not json")
    scoutnet._load_overlay()
    assert scoutnet._overlay == {}


def test_a_scoutnet_db_write_goes_through_the_overlay(overlay, monkeypatch):
    """scoutnet_db stores roles and the avatar; it must take the same path as everything else."""
    participants = {MEMBER: {"member_no": MEMBER, "member_type": "Deltagare", scoutnet_db.FIELD: {}}}

    project = scoutnet.CachedProject(project_id=1, project_name="test", participants=participants)
    monkeypatch.setattr(scoutnet_db, "get_single_project", lambda: project)
    monkeypatch.setattr(scoutnet_db.settings, "SCOUTNET_DB_HMAC_KEY", "")

    asyncio.run(scoutnet_db.set_values(MEMBER, {"roles": ["wsj27:x"]}))

    question_id = scoutnet_db.QUESTION_IDS["Deltagare"]
    stored = scoutnet._overlay[str(MEMBER)][question_id]["value"]
    assert scoutnet_db.decode(stored, MEMBER) == {"roles": ["wsj27:x"]}
    assert participants[MEMBER][scoutnet_db.FIELD] == {"roles": ["wsj27:x"]}
