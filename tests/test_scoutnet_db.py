"""scoutnet_db.py keeps a JSON key/value object inside one Scoutnet question.

Storage only - decoding, and which of the two question ids a member's value
comes from. What the stored keys *mean* belongs to the forms decoder, and is
covered in test_scoutnet_forms.py. The write path needs a live Scoutnet project
(and would write to it), so it is exercised by hand rather than here - see the
module docstring for the merge-and-replace semantics.
"""

import asyncio

import pytest

from app import scoutnet_db

PARTICIPANT_ID = scoutnet_db.QUESTION_IDS["Deltagare"]
LEADER_ID = scoutnet_db.QUESTION_IDS["Avdelningsledare"]


@pytest.mark.parametrize(
    "raw",
    ["", None, "not json at all", "[1, 2, 3]", '"a string"', "42"],
    ids=["empty", "none", "garbage", "list", "string", "number"],
)
def test_decode_returns_empty_dict_for_anything_that_is_not_an_object(raw):
    """A member with a broken value must not take the whole cache rebuild down."""
    assert scoutnet_db.decode(raw) == {}


def test_decode_reads_an_object():
    assert scoutnet_db.decode('{"patrol": "Falken", "n": 2}') == {"patrol": "Falken", "n": 2}


def test_raw_answer_picks_the_id_belonging_to_the_members_form():
    answers = {PARTICIPANT_ID: '{"who": "participant"}', LEADER_ID: '{"who": "leader"}'}
    assert scoutnet_db.raw_answer(answers, "Deltagare") == '{"who": "participant"}'
    assert scoutnet_db.raw_answer(answers, "Kontingentledning") == '{"who": "leader"}'


def test_raw_answer_falls_back_to_either_id_when_member_type_is_unknown():
    assert scoutnet_db.raw_answer({LEADER_ID: '{"a": 1}'}, "") == '{"a": 1}'
    assert scoutnet_db.raw_answer({}, "") is None


def test_stored_for_goes_from_raw_answers_to_an_object():
    answers = {PARTICIPANT_ID: '{"patrol": "Falken"}'}
    assert scoutnet_db.stored_for(answers, "Deltagare") == {"patrol": "Falken"}
    assert scoutnet_db.stored_for({}, "Deltagare") == {}


# --- ensure_values: the guard that keeps per-request callers off Scoutnet ---
#
# Driven through asyncio.run() rather than an async-test plugin, which this
# suite does not have and does not need for five tests.


class _FakeProject:
    def __init__(self, participants):
        self.project_id = 1
        self.participants = participants


@pytest.fixture
def cached(monkeypatch):
    """A one-member participant cache, as scoutnet_db reads it."""
    participants = {1000000: {"member_no": 1000000, scoutnet_db.FIELD: {"avatar_url": "https://x/old"}}}
    monkeypatch.setattr(scoutnet_db, "get_single_project", lambda: _FakeProject(participants))
    return participants


@pytest.fixture
def writes(monkeypatch, cached):
    """Record what would have gone to Scoutnet, and update the cache as a real write does."""
    sent = []

    async def _fake_write(member_no, data):
        sent.append((member_no, data))
        cached[member_no][scoutnet_db.FIELD] = data

    monkeypatch.setattr(scoutnet_db, "_write", _fake_write)
    return sent


def test_ensure_values_does_not_write_when_the_value_already_matches(cached, writes):
    assert asyncio.run(scoutnet_db.ensure_values(1000000, {"avatar_url": "https://x/old"})) is False
    assert writes == []


def test_ensure_values_writes_when_the_value_changed(cached, writes):
    assert asyncio.run(scoutnet_db.ensure_values(1000000, {"avatar_url": "https://x/new"})) is True
    assert writes == [(1000000, {"avatar_url": "https://x/new"})]


def test_ensure_values_keeps_the_other_stored_keys(cached, writes):
    cached[1000000][scoutnet_db.FIELD]["patrol"] = "Falken"
    asyncio.run(scoutnet_db.ensure_values(1000000, {"avatar_url": "https://x/new"}))
    assert writes == [(1000000, {"avatar_url": "https://x/new", "patrol": "Falken"})]


def test_ensure_values_is_a_quiet_no_op_for_someone_who_is_not_a_participant(cached, writes):
    """Authenticated callers outside the project hit this on every request."""
    assert asyncio.run(scoutnet_db.ensure_values(999, {"avatar_url": "https://x/new"})) is False
    assert writes == []


def test_a_burst_of_identical_writes_collapses_to_one(cached, writes):
    """The check is repeated under the lock, so concurrent callers do not each send it."""

    async def burst():
        await asyncio.gather(*(scoutnet_db.ensure_values(1000000, {"avatar_url": "https://x/new"}) for _ in range(10)))

    asyncio.run(burst())
    assert len(writes) == 1
