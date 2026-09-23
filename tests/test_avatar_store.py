"""The avatar URL reaches this app in the token and nowhere else.

So `_store_avatar` runs on every authenticated request, which is only tenable
because it is cheap and because it cannot take a request down with it. These
cover both halves: what it declines to do, and that a failed write stays a log
line rather than an exception on the auth path.

Whether a write is actually sent is scoutnet_db's decision (`ensure_values`,
tested in test_scoutnet_db.py); here it is stubbed out to record the call.
"""

import asyncio
import logging

import pytest

from app import scoutnet_db
from app.authenctication import AuthUser, _avatar_tasks, _store_avatar

URL = "https://scoutnet.se/avatars/3073781.jpg"


def _user(**overrides) -> AuthUser:
    base = {
        "name": "Håkan Persson",
        "preferred_username": "scoutnet|3073781",
        "given_name": "Håkan",
        "family_name": "Persson",
        "member_no": "3073781",
        "roles": ["wsj27:cmt"],
        "picture": URL,
    }
    base.update(overrides)
    return AuthUser(**base)


@pytest.fixture
def calls(monkeypatch):
    """Record what _store_avatar hands to scoutnet_db, instead of writing.

    `would_change` says yes by default, so each test states its own reason for
    expecting nothing to be offered.
    """
    recorded = []

    async def _fake_ensure(member_no, values):
        recorded.append((member_no, values))
        return True

    monkeypatch.setattr(scoutnet_db, "would_change", lambda member_no, values: True)
    monkeypatch.setattr(scoutnet_db, "ensure_values", _fake_ensure)
    return recorded


def _serve(user: AuthUser) -> None:
    """One authenticated request, waiting for the background task it spawns."""

    async def run():
        _store_avatar(user)
        if _avatar_tasks:
            await asyncio.gather(*list(_avatar_tasks), return_exceptions=True)

    asyncio.run(run())


def test_a_picture_claim_is_offered_to_the_store(calls):
    _serve(_user())
    assert calls == [(3073781, {"avatar_url": URL})]


@pytest.mark.parametrize(
    "overrides",
    [{"picture": None}, {"picture": ""}, {"member_no": ""}, {"member_no": "not-a-number"}],
    ids=["no-claim", "empty-claim", "no-member-no", "unparseable-member-no"],
)
def test_nothing_is_offered_when_there_is_nothing_usable(calls, overrides):
    """user_id would raise on a non-numeric member_no, once per request."""
    _serve(_user(**overrides))
    assert calls == []


def test_no_task_is_created_when_the_stored_url_already_matches(calls, monkeypatch):
    """The per-request fast path: a dict lookup, and no Task at all."""
    monkeypatch.setattr(scoutnet_db, "would_change", lambda member_no, values: False)

    _serve(_user())

    assert calls == []
    assert not _avatar_tasks


def test_a_failed_write_is_logged_rather_than_raised(monkeypatch, caplog):
    """Scoutnet being down must not turn into a failed authentication."""

    async def _boom(member_no, values):
        raise scoutnet_db.ScoutnetDbError("Scoutnet rejected the write")

    monkeypatch.setattr(scoutnet_db, "would_change", lambda member_no, values: True)
    monkeypatch.setattr(scoutnet_db, "ensure_values", _boom)

    with caplog.at_level(logging.WARNING):
        _serve(_user())  # Must not raise.

    assert "Failed to store avatar URL" in caplog.text
