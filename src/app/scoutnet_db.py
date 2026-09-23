"""A small key/value store kept inside a Scoutnet question field.

Scoutnet has one free-text question per form reserved for this app ("Campfire
data", see `QUESTION_IDS`), and this module treats it as a JSON object: values
this app needs to keep next to the rest of a member's Scoutnet data, rather than
in a database of our own.

Storage only. What any particular key *means* — which ones surface on the
participant record, which grant roles — is the forms decoder's business, the
same as for every other Scoutnet answer; see `_apply_scout_db()` there. This
module never looks inside the object it stores.

Nothing else may use the field, and nothing else writes it: a human editing it
in the Scoutnet admin UI would be editing a JSON blob by hand.

**Writes replace the whole object.** Scoutnet stores one string, so changing a
single key means re-encoding every key. That makes the cached copy on the
participant record (`FIELD`) load-bearing rather than a convenience: it is what
a write merges into. `_LOCK` serialises the read-modify-write so two concurrent
callers cannot each merge into the same starting dict and lose one update.

The copy is refreshed from Scoutnet on every cache rebuild — `decode()` is
called from the forms decoder — so a value written outside this app still wins
eventually. One race is not covered: a cache refresh that fetched participants
*before* a write committed will rebuild the record from that older fetch and
drop the new value from the cache, though Scoutnet itself keeps it and the next
refresh brings it back. Refreshes are hourly and writes are rare, so this is
left as a known gap rather than paid for with a lock across the whole refresh.
"""

import asyncio
import json
import logging
from typing import Any

import httpx

from .config import get_settings
from .scoutnet import get_single_project

settings = get_settings()
logger = logging.getLogger(__name__)

CHECKIN_API = "https://www.scoutnet.se/api/project/checkin"

# The two forms share no question ids, so each member has exactly one of these
# and it follows from their member_type. Writing the other one is accepted by
# Scoutnet — it validates only that the question belongs to the *project* — and
# would silently park the data on a form the member is not registered on, where
# nothing reads it back. Hence the mapping rather than a single id.
QUESTION_IDS = {
    "Deltagare": "119387",  # form 39188, "Campfire data 39188"
    "IST": "119387",
    "Avdelningsledare": "119388",  # form 47115, "Campfire data 47115"
    "Kontingentledning": "119388",
}

# Where the decoded object sits on the participant record.
FIELD = "scout_db"

# Well under the 64 KiB proven to round-trip intact, but large enough that
# hitting it means something is being stored here that does not belong.
MAX_ENCODED_LEN = 16384

_LOCK = asyncio.Lock()


class ScoutnetDbError(RuntimeError):
    """A write was rejected, or the field could not be resolved for a member."""


# --- Read path (called from the forms decoder) ---


def decode(raw: Any) -> dict:
    """One member's stored object, or {} when unset or unreadable.

    Never raises: a member whose field holds something that is not a JSON object
    must not take a whole cache rebuild down with them.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        logger.error("Unparseable %s value, ignoring: %.80r", FIELD, raw)
        return {}
    if not isinstance(data, dict):
        logger.error("%s value is %s, not an object, ignoring", FIELD, type(data).__name__)
        return {}
    return data


def raw_answer(answers: dict, member_type: str = "") -> Any:
    """This member's stored string, picked out of their raw Scoutnet answers.

    Normally the id follows from member_type, but both are checked when it is
    unknown (or when the member somehow answered the other form's id), since
    only one of the two can hold anything for a given member anyway.
    """
    question_id = QUESTION_IDS.get(member_type)
    if question_id is not None:
        return answers.get(question_id)
    for question_id in set(QUESTION_IDS.values()):
        if value := answers.get(question_id):
            return value
    return None


def stored_for(answers: dict, member_type: str = "") -> dict:
    """One member's stored object, straight from their raw Scoutnet answers."""
    return decode(raw_answer(answers, member_type))


def _participant(member_no: int) -> dict | None:
    """One member's cached record, or None if there is none - including when the
    project cache has not been filled yet, which get_single_project() reports by
    raising. Read paths run on every authenticated request (see the avatar write
    in authenctication.py) and must stay quiet in both cases."""
    try:
        project = get_single_project()
    except StopIteration:
        logger.debug("No project cached yet, nothing stored for member %s", member_no)
        return None
    return project.participants.get(member_no)


def get(member_no: int) -> dict:
    """The stored object for one member, from the cache. {} if unknown."""
    participant = _participant(member_no)
    return dict(participant.get(FIELD) or {}) if participant else {}


def get_value(member_no: int, key: str, default: Any = None) -> Any:
    """One stored key for one member, from the cache."""
    return get(member_no).get(key, default)


# --- Write path ---


def would_change(member_no: int, values: dict[str, Any]) -> bool:
    """Would `values` change anything, judged against the cached copy?

    Synchronous and cheap - a dict lookup - so a caller on a hot path can ask
    before deciding to await anything at all; `_store_avatar` in
    authenctication.py does exactly that, on every authenticated request.
    `ensure_values` asks again under the lock, so this is an optimisation and
    never the thing that makes a write correct.

    False for a member who is not a confirmed participant: there is nowhere to
    write them, and callers that ask on every request must not raise once per
    request for everyone who is not in the project.
    """
    participant = _participant(member_no)
    if participant is None:
        logger.debug("No participant record for member %s, nothing to store", member_no)
        return False
    stored = participant.get(FIELD) or {}
    return any(key in stored if value is None else stored.get(key) != value for key, value in values.items())


async def ensure_values(member_no: int, values: dict[str, Any]) -> bool:
    """Write `values` only if the stored object does not already agree.

    For values this app re-derives on every request — an avatar URL that exists
    nowhere but the caller's JWT — where writing each time would be absurd but
    the value still has to reach Scoutnet whenever it actually changes. The
    common case costs one dict lookup and no request at all.

    Returns True if a write was sent. A member with no participant record is a
    quiet False, not an error.
    """
    if not would_change(member_no, values):
        return False
    async with _LOCK:
        # Checked again under the lock. A burst of concurrent requests carrying
        # the same new value would otherwise all pass the check above before any
        # of them committed, and each send the same write.
        if not would_change(member_no, values):
            return False
        await _write(member_no, _merged(member_no, values))
        return True


async def set_values(member_no: int, values: dict[str, Any]) -> dict:
    """Merge `values` into the member's stored object and write it back.

    Always writes, even when nothing changes — Scoutnet then commits nothing and
    reports the answer unchanged. Use `ensure_values` where a write per call
    would be wasteful.

    Returns the object as it now stands. A key set to None is removed, which is
    how you delete: there is no separate delete call, because every write sends
    the whole object anyway.
    """
    async with _LOCK:
        data = _merged(member_no, values)
        await _write(member_no, data)
        return data


def _merged(member_no: int, values: dict[str, Any]) -> dict:
    """The member's cached object with `values` applied; None removes a key."""
    data = get(member_no)
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


async def replace(member_no: int, data: dict[str, Any]) -> dict:
    """Overwrite the member's stored object wholesale, discarding what was there."""
    async with _LOCK:
        await _write(member_no, dict(data))
        return dict(data)


async def _write(member_no: int, data: dict[str, Any]) -> None:
    """PUT the encoded object to Scoutnet, then update the cached copy.

    The cache is only updated once Scoutnet has committed, so a failed write
    leaves the record reading exactly what Scoutnet still holds.
    """
    try:
        project = get_single_project()
    except StopIteration:
        raise ScoutnetDbError("No project cached yet") from None
    config = next((p for p in settings.SCOUTNET_PROJECTS if p.id == project.project_id), None)
    if config is None or not config.update_key:
        raise ScoutnetDbError(f"Project {project.project_id} has no update_key configured")

    participant = project.participants.get(member_no)
    if participant is None:
        raise ScoutnetDbError(f"Member {member_no} is not a confirmed participant")
    question_id = QUESTION_IDS.get(participant.get("member_type", ""))
    if question_id is None:
        raise ScoutnetDbError(f"Member {member_no} has member_type {participant.get('member_type')!r}, no field for it")

    # An empty object clears the field rather than storing "{}", so a member
    # with nothing stored looks the same whether this app ever wrote to them.
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True) if data else ""
    if len(encoded) > MAX_ENCODED_LEN:
        raise ScoutnetDbError(f"{FIELD} for member {member_no} would be {len(encoded)} chars, over {MAX_ENCODED_LEN}")

    # Only `questions` — sending `checked_in` would force attended=1, which
    # cannot be undone through this endpoint.
    body = {str(member_no): {"questions": {question_id: {"value": encoded}}}}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.put(
                CHECKIN_API,
                params={"id": str(project.project_id), "key": config.update_key},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise ScoutnetDbError(f"Scoutnet write failed: {type(exc).__name__}") from exc

    if response.status_code != httpx.codes.OK:
        raise ScoutnetDbError(f"Scoutnet rejected the write: {_error_detail(response)}")

    # An entry of null means "not updated", which for this endpoint means the
    # stored value already matched — not a failure. Anything else carries the
    # before/after pair, logged because this field is written rarely and by
    # hand often enough that the history is worth having.
    entry = (response.json().get("updated_questions") or {}).get(str(member_no), {}).get(question_id)
    keys = ", ".join(sorted(data)) or "(empty)"
    logger.info(
        "%s for member %s %s: %s (%d chars)",
        FIELD,
        member_no,
        "unchanged" if entry is None else entry.get("action"),
        keys,
        len(encoded),
    )

    participant[FIELD] = data


def _error_detail(response: httpx.Response) -> str:
    """The useful part of a Scoutnet error body, in either shape it comes in."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    if isinstance(body, dict):
        # Either {"error": "..."} or {"message": "...", "errors": {member: {qid: msg}}}.
        detail = body.get("error") or body.get("message") or ""
        if errors := body.get("errors"):
            detail = f"{detail} {json.dumps(errors, ensure_ascii=False)}"
        if detail:
            return f"HTTP {response.status_code}: {detail.strip()[:300]}"
    return f"HTTP {response.status_code}"
