"""Participant lookup, at three levels of detail.

Every route is gated twice: `require_auth_user` says who is asking, and the
rules below say how much of the answer they get. "name" and "basic" need basic
access; "full" adds the health and dietary answers, so it needs a grant of its
own.

  * Avdelningsledare (`wsj27:al:<troop>`) may read their own troop at any level
    and nothing else. A health role raises how much of their troop they see,
    never whose records they can reach.
  * Kontingentledning (`wsj27:cmt...`) may read every participant at basic, and
    full with either of the health roles below.

Both can be true of one person, and then each rule applies where it applies.
"""

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .authenctication import AuthUser, require_auth_user
from .config import get_settings
from .scoutnet import get_single_project

settings = get_settings()
logger = logging.getLogger(__name__)

InfoLevel = Literal["name", "basic", "full"]

TROOP_MAPPER = {"cmt": "Kontingentledning", "al": "Avdelningsledare", "ist": "IST"}

# Ordered, so `granted < required` is the whole check. A name is participant
# data like any other, hence the same cost as basic: someone with no access to a
# person must not be able to confirm they exist by asking for their name.
NO_ACCESS, BASIC_ACCESS, FULL_ACCESS = 0, 1, 2
REQUIRED_ACCESS = {"name": BASIC_ACCESS, "basic": BASIC_ACCESS, "full": FULL_ACCESS}

# Either one unlocks health data. The first is the Support function's health
# people, slugified out of the Funktion/Roll CSV ("Hälsa" -> "halsa"); the
# second is granted per person in the Scoutnet form and cuts across functions.
HEALTH_ROLES = frozenset({"wsj27:cmt:support:halsa", "wsj27:access:Hälsa plus intern information"})


def _troop_access(user: AuthUser, troop: str | None) -> int:
    """How much of `troop`'s participants this caller may see.

    Pass None for a collection that is no troop — a member-type listing — so
    that only the contingent-wide grant can apply. Holding no leader troops
    means no troops, never all of them.
    """
    if troop and troop in user.role_suffixes("wsj27:al"):
        return FULL_ACCESS
    if not user.has_role("wsj27:cmt"):
        return NO_ACCESS
    return FULL_ACCESS if user.has_any_role(HEALTH_ROLES) else BASIC_ACCESS


def _authorize(user: AuthUser, troop: str | None, infolevel: InfoLevel, subject: str, not_found: str) -> None:
    """Raise unless the caller may read `troop` at `infolevel`.

    No access at all is a 404 carrying `not_found` — the same answer a record
    that genuinely is not there gets, so a refusal cannot be used to discover
    who exists. Access but not this much of it is a 403 rather than a quietly
    downgraded 200: a client has to be able to tell "no health answers
    recorded" from "you may not see them".
    """
    granted = _troop_access(user, troop)
    if granted == NO_ACCESS:
        logger.warning("Denied %s access to %s", user, subject)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=not_found)
    if granted < REQUIRED_ACCESS[infolevel]:
        logger.warning("Denied %s '%s' access to %s", user, infolevel, subject)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Info level '{infolevel}' requires authorisation for health and internal information.",
        )


def _project(participant: dict[str, Any], infolevel: InfoLevel) -> dict[str, Any]:
    """One participant cut down to `infolevel`, always as a new dict."""
    if infolevel == "name":
        return {"member_no": participant["member_no"], "name": participant["name"]}
    if infolevel == "full":
        return dict(participant)
    return {key: value for key, value in participant.items() if key != "forms_data"}


# --- API routes ---

router = APIRouter()


@router.get(
    "/troopinfo/{troop_id}",
    response_model=list,
    status_code=status.HTTP_200_OK,
    response_description="Troop info",
)
async def troopinfo(
    troop_id: str,
    infolevel: InfoLevel = Query("basic"),
    user: AuthUser = Depends(require_auth_user),
):
    """Every participant in one troop, or in one member type."""
    troop_id = TROOP_MAPPER.get(troop_id, troop_id)

    # Authorised before the lookup, so an unauthorised caller gets the same
    # answer whether or not the troop exists. Only a numbered troop can be a
    # leader's own; a member-type listing is nobody's troop, hence None.
    _authorize(
        user, troop_id if troop_id.isdigit() else None, infolevel, f"troop {troop_id}", "Troop not found in project."
    )

    pdata = get_single_project()
    if troop_id.isdigit():
        tinfo = [p for p in pdata.participants.values() if p["troop"] == troop_id]
    elif troop_id != "Deltagare":
        tinfo = [p for p in pdata.participants.values() if p["member_type"] == troop_id]
    else:
        tinfo = []

    if not tinfo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Troop not found in project.",
        )

    return [_project(p, infolevel) for p in tinfo]


@router.get(
    "/individual/{member_id}",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    response_description="Individual info",
)
async def individualinfo(
    member_id: int,
    infolevel: InfoLevel = Query("basic"),
    user: AuthUser = Depends(require_auth_user),
):
    """One participant, by member number."""
    pdata = get_single_project()
    meminfo = pdata.participants.get(member_id)
    if not meminfo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Participant not found in project.",
        )

    # The participant's own troop is what decides this, so unlike troopinfo the
    # lookup has to come first. Same 404 detail as above, for the same reason.
    _authorize(user, meminfo["troop"], infolevel, f"member {member_id}", "Participant not found in project.")

    if infolevel == "name":
        # Narrower than "name" elsewhere: the caller asked by member number, so
        # the number is not news. Kept as it was, to not move a response shape.
        return {"name": meminfo["name"]}

    return _project(meminfo, infolevel)
