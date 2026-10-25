"""A small key/value store kept inside a Scoutnet question field.

Scoutnet has one free-text question per form reserved for this app ("Campfire
data", see `QUESTION_IDS`), and this module treats it as a JSON object: values
this app needs to keep next to the rest of a member's Scoutnet data, rather than
in a database of our own.

Storage only. What any particular key *means* — which ones surface on the
participant record, which grant roles — is the forms decoder's business, the
same as for every other Scoutnet answer. This module never looks inside the
object it stores.

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

**Only prod writes this field.** Every environment reads the same Scoutnet
project, so dev and staging run with SCOUTNET_OVERLAY, which keeps their writes
out of Scoutnet (see scoutnet.py). Prod has a `SCOUTNET_DB_HMAC_KEY` and a local run does not, so a
value written unsigned by a local run with the overlay off - the one deliberate
way to test a real write - is rejected by prod rather than read back as its own.
"""

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from .config import get_settings
from .scoutnet import ScoutnetRequestError, get_single_project, set_answer

settings = get_settings()
logger = logging.getLogger(__name__)

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

# --- Tamper detection ---
#
# The field is an ordinary text question, so it is editable in the Scoutnet
# admin GUI, where it shows up as a wall of JSON that invites a well-meaning
# hand-edit. Anything this app writes is therefore signed, and a value whose
# signature does not check out is dropped with an error rather than read back as
# if we had written it.
#
# This is an accident detector, not a security boundary: anyone who can read the
# key can forge a value. It answers "did something else change this?", which is
# the question worth asking about a field only this app is supposed to write.
#
# Stored shape: {"v": 1, "mac": "<hex>", "d": {...the actual object...}}
ENVELOPE_VERSION = 1
_MAC_VERSION_KEY, _MAC_KEY, _DATA_KEY = "v", "mac", "d"

_warned_unsigned = False

_LOCK = asyncio.Lock()


class ScoutnetDbError(RuntimeError):
    """A write was rejected, or the field could not be resolved for a member."""


# --- Read path (called from the forms decoder) ---


def _canonical(data: dict) -> str:
    """The one encoding of `data` that both signing and writing agree on."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _mac(member_no: int, data: dict) -> str:
    """The signature for one member's object.

    Bound to the member number as well as the contents, so a blob copied from
    one member's field to another's in the GUI fails to verify rather than
    arriving as that member's own data.
    """
    message = f"{ENVELOPE_VERSION}:{member_no}:{_canonical(data)}".encode()
    return hmac.new(settings.SCOUTNET_DB_HMAC_KEY.encode(), message, hashlib.sha256).hexdigest()


def _encode(member_no: int, data: dict) -> str:
    """One member's object as the string Scoutnet stores, signed if we have a key.

    Written unsigned when no key is configured, so the field stays readable by
    both modes: a later key makes older values fail verification loudly rather
    than silently, which is the right way round for a guard against edits.
    """
    envelope = {_MAC_VERSION_KEY: ENVELOPE_VERSION, _DATA_KEY: data}
    if settings.SCOUTNET_DB_HMAC_KEY:
        envelope[_MAC_KEY] = _mac(member_no, data)
    else:
        _warn_unsigned_once()
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _warn_unsigned_once() -> None:
    global _warned_unsigned
    if not _warned_unsigned:
        _warned_unsigned = True
        logger.warning(
            "SCOUTNET_DB_HMAC_KEY is not set: %s values are neither signed nor verified, "
            "so a hand-edit in the Scoutnet GUI will be read back as if this app wrote it",
            FIELD,
        )


def decode(raw: Any, member_no: int) -> dict:
    """One member's stored object, or {} when unset, unreadable or unverified.

    Never raises: one member's mangled value must not take a whole cache rebuild
    down with them. Everything that is not a value this app wrote is dropped,
    and every drop is logged at error - the field is written by this app alone,
    so anything else in it is an accident someone needs to hear about.
    """
    if not raw:
        return {}
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        logger.error("%s for member %s is not JSON, dropping it: %.80r", FIELD, member_no, raw)
        return {}
    if not isinstance(envelope, dict):
        logger.error("%s for member %s is %s, not an object, dropping it", FIELD, member_no, type(envelope).__name__)
        return {}

    # Two separate questions: is this one of our envelopes at all, and does it
    # carry a signature? An envelope written while no key was configured has no
    # mac, and must still unwrap to its payload rather than to itself.
    data = envelope.get(_DATA_KEY)
    wrapped = isinstance(data, dict) and _MAC_VERSION_KEY in envelope
    signed = wrapped and _MAC_KEY in envelope

    if not settings.SCOUTNET_DB_HMAC_KEY:
        _warn_unsigned_once()
        # Unverifiable either way, so take the payload at face value - including
        # a bare object written before this app wrapped what it stored.
        return data if wrapped else envelope

    if not signed:
        # Whatever wrote it had no key, and has overwritten what was stored
        # here: a local run testing a real write (SCOUTNET_OVERLAY off), a
        # deployment that should have had the overlay on, or a hand-edit.
        logger.error(
            "%s for member %s carries no signature, so its previous contents are gone - written by "
            "an environment with no SCOUTNET_DB_HMAC_KEY and SCOUTNET_OVERLAY off, edited by hand in "
            "Scoutnet, or written before the key was set. Dropping it: %.80r",
            FIELD,
            member_no,
            raw,
        )
        return {}
    if not hmac.compare_digest(str(envelope.get(_MAC_KEY)), _mac(member_no, data)):
        logger.error(
            "%s for member %s fails its signature - the value was changed outside this app, "
            "most likely edited in the Scoutnet GUI. Dropping it: %.80r",
            FIELD,
            member_no,
            raw,
        )
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


def stored_for(answers: dict, member_no: int, member_type: str = "") -> dict:
    """One member's stored object, straight from their raw Scoutnet answers."""
    return decode(raw_answer(answers, member_type), member_no)


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
    before deciding to await anything at all; `_store_token_values` in
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
    """Write the encoded object to Scoutnet, then update the cached copy.

    Through scoutnet.set_answer(), so SCOUTNET_OVERLAY keeps it out of Scoutnet
    where it is on. The cache is only updated once the write has committed, so
    a failed write leaves the record reading exactly what Scoutnet still holds.
    """
    try:
        project = get_single_project()
    except StopIteration:
        raise ScoutnetDbError("No project cached yet") from None

    participant = project.participants.get(member_no)
    if participant is None:
        raise ScoutnetDbError(f"Member {member_no} is not a confirmed participant")
    question_id = QUESTION_IDS.get(participant.get("member_type", ""))
    if question_id is None:
        raise ScoutnetDbError(f"Member {member_no} has member_type {participant.get('member_type')!r}, no field for it")

    # An empty object clears the field rather than storing an empty envelope, so
    # a member with nothing stored looks the same whether this app ever wrote to
    # them. The signature goes in with the data; see "Tamper detection" above.
    encoded = _encode(member_no, data) if data else ""
    if len(encoded) > MAX_ENCODED_LEN:
        raise ScoutnetDbError(f"{FIELD} for member {member_no} would be {len(encoded)} chars, over {MAX_ENCODED_LEN}")

    try:
        await set_answer(member_no, question_id, encoded)
    except ScoutnetRequestError as exc:
        raise ScoutnetDbError(str(exc)) from exc
    # Logged because this field is written rarely and by hand often enough that
    # the history is worth having; set_answer() has logged how it went.
    logger.info(
        "%s for member %s written: %s (%d chars)", FIELD, member_no, ", ".join(sorted(data)) or "(empty)", len(encoded)
    )

    participant[FIELD] = data
