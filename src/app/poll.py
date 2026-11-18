"""Polls: questions CMT puts to the troops or to people, answered in Campfire.

CMT creates a poll with `POST /polls`. It opens at `opens_at` and closes at
`closes_at`, and responses are taken only in between. Whether a poll is
upcoming, open or closed is never stored; it is worked out from the clock each
time.

Each poll has its own `options`, the answers it accepts, e.g. `grön`, `gul`,
`röd`. Campfire chooses them and what they mean, also which need a comment; the
API only checks that a response is one of them.

There are two types of poll, and a poll's type never changes:

- `troop`: each troop gives one response, an option and a comment. Any leader of
  the troop may give it and change it while the poll is open. A new response
  replaces the old one, and the row says which leader gave it. All of CMT sees
  every troop's response, a leader only their own troop's.
- `individual`: each person in the poll's `audience` gives their own response,
  and may change it while the poll is open. The audience is troop leaders, CMT,
  or both. The head of contingent and the poll's creator see every
  response, everyone else only their own. Troop leaders do not see an individual
  poll whose audience leaves them out.

A poll can be changed until it closes, but once it has opened its `opens_at`,
`options` and `audience` stay. It can be removed only before it opens.

Messages: a loop runs every minute and sends each open poll's messages as they
fall due, at the points in `MESSAGES`. The first tells everyone in the poll that
it has opened; the later ones remind those who have not responded, unless the
poll has `reminders` turned off. The poll's `messages_sent` counts how many have
gone out. A message is claimed by counting it before it is sent, so it goes out
once, also across restarts and with several pods. After downtime every message
that fell due is sent, in order.

A troop poll's messages are meant for the troop's leader channel on Discord,
which is #38's `notify.post_to_troop`. An individual poll's go to each person:
a Discord direct message for leaders, Teams for CMT, through a service that does
not exist yet. Until they are there, `_post_to_troop` and `_notify_person` below
stand in for them and only log.

Value rules are enforced here in code, not as DB CHECK constraints, for the same
reason as in case.py: `db_init_tables()` only ever runs `CREATE TABLE IF NOT EXISTS`.
"""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .authenctication import AuthUser, has_any_role, require_auth_user, role_suffixes
from .config import get_settings
from .db import db_execute, db_fetch, db_fetchrow
from .scoutnet import get_single_project

# --- Settings and data classes ---

settings = get_settings()
logger = logging.getLogger(__name__)

# Who may create, change and remove polls, and who sees every troop's response.
# All of CMT for now; two names so that either can be narrowed on its own.
POLL_ADMIN_ROLE = "wsj27:cmt"
RESULT_ROLE = "wsj27:cmt"
# Who sees every response to an individual poll, besides the poll's creator: the
# head of contingent. The role is minted from Scoutnet's Funktion and Roll.
INDIVIDUAL_RESULT_ROLE = "wsj27:cmt:hoc:hoc"

LOOP_SECONDS = 60
STOCKHOLM = ZoneInfo("Europe/Stockholm")
WEEKDAYS = ("mån", "tis", "ons", "tors", "fre", "lör", "sön")
MONTHS = ("jan", "feb", "mars", "april", "maj", "juni", "juli", "aug", "sep", "okt", "nov", "dec")

# The messages, in the order they go out. Each is due at a point in the poll's open
# period, from 0 (when it opens) to 1 (when it closes). The first goes to everyone in
# the poll, the others only to those who have not responded. The same texts go to a
# troop's leader channel and to a person. The channel is read by the troop's leaders
# and by unit support, so no text names or points to anyone.
MESSAGES = (
    (0.0, "Ny avstämning: {title}. Svara i Campfire senast {closes}."),
    (0.5, "Påminnelse: {title} väntar på ditt/ert svar. Svara i Campfire senast {closes}."),
    (0.95, "Sista påminnelsen: {title} stänger {closes}. Svara i Campfire innan dess."),
)
# The poll's page in Campfire, put after CAMPFIRE_URL at the end of each message.
# Campfire decides the path; this is the one it is expected to have.
CAMPFIRE_POLL_PATH = "/polls/{poll_id}"


# One of the answers a poll accepts: not empty, and not only spaces.
Option = Annotated[str, Field(min_length=1, pattern=r"\S")]
# Who an individual poll can be put to: troop leaders (wsj27:al) and CMT (wsj27:cmt).
# Each name is the second part of its role, which is how the code turns one into the other.
Audience = Literal["al", "cmt"]


class PollCreate(BaseModel):
    type: Literal["troop", "individual"] = Field(
        "troop",
        description="Who answers. `troop`: one response per troop. `individual`: one per person in `audience`.",
    )
    title: str = Field(min_length=1, pattern=r"\S")
    description: str = ""
    opens_at: AwareDatetime
    closes_at: AwareDatetime
    options: list[Option] = Field(min_length=1, description="The answers the poll accepts, e.g. `grön`, `gul`, `röd`.")
    audience: list[Audience] = Field(
        [],
        description="Who answers an individual poll: `al` (troop leaders), `cmt`, or both. Empty for a troop poll.",
    )
    reminders: bool = Field(True, description="False sends only the message when the poll opens.")

    @model_validator(mode="after")
    def _check(self):
        if self.closes_at <= self.opens_at:
            raise ValueError("closes_at must be after opens_at")
        if len(set(self.options)) != len(self.options):
            raise ValueError("options must not repeat")
        if len(set(self.audience)) != len(self.audience):
            raise ValueError("audience must not repeat")
        if self.type == "individual" and not self.audience:
            raise ValueError("an individual poll needs an audience")
        if self.type == "troop" and self.audience:
            raise ValueError("a troop poll has no audience")
        return self


class PollUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, pattern=r"\S")
    description: str | None = None
    opens_at: AwareDatetime | None = None
    closes_at: AwareDatetime | None = None
    options: list[Option] | None = Field(None, min_length=1)
    audience: list[Audience] | None = Field(
        None, description="Not empty for an individual poll, and empty for a troop poll."
    )
    reminders: bool | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.options is not None and len(set(self.options)) != len(self.options):
            raise ValueError("options must not repeat")
        if self.audience is not None and len(set(self.audience)) != len(self.audience):
            raise ValueError("audience must not repeat")
        return self


class Poll(BaseModel):
    id: int
    created_at: datetime
    creator_id: int
    type: Literal["troop", "individual"]
    title: str
    description: str
    opens_at: datetime
    closes_at: datetime
    options: list[str]
    audience: list[str]
    reminders: bool
    state: Literal["upcoming", "open", "closed"]


class ResponseUpdate(BaseModel):
    status: str = Field(description="One of the poll's `options`.")
    comment: str = ""


class PollResponse(BaseModel):
    poll_id: int
    troop: str | None = Field(description="The troop a troop poll's response is for. Null in an individual poll.")
    status: str
    comment: str
    respondent_id: int = Field(description="Member ID of the person who gave the response, or last changed it.")
    responded_at: datetime


# --- Database functions ---


async def db_init_tables() -> None:
    logger.info("Initializing poll database tables")
    await db_execute("""
        CREATE TABLE IF NOT EXISTS polls (
            id             BIGSERIAL    PRIMARY KEY,
            created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            creator_id     BIGINT       NOT NULL,
            type           TEXT         NOT NULL,
            title          TEXT         NOT NULL,
            description    TEXT         NOT NULL DEFAULT '',
            opens_at       TIMESTAMPTZ  NOT NULL,
            closes_at      TIMESTAMPTZ  NOT NULL,
            options        TEXT[]       NOT NULL,
            audience       TEXT[]       NOT NULL DEFAULT '{}',
            reminders      BOOLEAN      NOT NULL DEFAULT TRUE,
            messages_sent  INTEGER      NOT NULL DEFAULT 0
        )
    """)
    # A troop poll's response has a troop, and the troop has one response per poll.
    # An individual poll's has troop NULL, and the person has one; the index below
    # keeps that, as UNIQUE (poll_id, troop) never compares two NULLs.
    await db_execute("""
        CREATE TABLE IF NOT EXISTS poll_responses (
            id             BIGSERIAL    PRIMARY KEY,
            poll_id        BIGINT       NOT NULL REFERENCES polls (id),
            troop          TEXT,
            respondent_id  BIGINT       NOT NULL,
            responded_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            status         TEXT         NOT NULL,
            comment        TEXT         NOT NULL DEFAULT '',
            UNIQUE (poll_id, troop)
        )
    """)
    await db_execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS poll_responses_person
        ON poll_responses (poll_id, respondent_id) WHERE troop IS NULL
    """)
    logger.info("Poll database tables ready")


# Polls


async def db_insert_poll(creator_id: int, poll: PollCreate):
    return await db_fetchrow(
        """
        INSERT INTO polls (creator_id, type, title, description, opens_at, closes_at, options, audience, reminders)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING *
        """,
        creator_id,
        poll.type,
        poll.title,
        poll.description,
        poll.opens_at,
        poll.closes_at,
        poll.options,
        poll.audience,
        poll.reminders,
    )


async def db_get_poll(poll_id: int):
    return await db_fetchrow("SELECT * FROM polls WHERE id = $1", poll_id)


async def db_list_polls():
    """Every poll, the one that opens last first."""
    return await db_fetch("SELECT * FROM polls ORDER BY opens_at DESC, id DESC")


async def db_update_poll(
    poll_id: int,
    title: str,
    description: str,
    opens_at: datetime,
    closes_at: datetime,
    options: list[str],
    audience: list[str],
    reminders: bool,
):
    return await db_fetchrow(
        """
        UPDATE polls SET title = $2, description = $3, opens_at = $4, closes_at = $5, options = $6,
            audience = $7, reminders = $8
        WHERE id = $1
        RETURNING *
        """,
        poll_id,
        title,
        description,
        opens_at,
        closes_at,
        options,
        audience,
        reminders,
    )


async def db_delete_poll(poll_id: int) -> None:
    await db_execute("DELETE FROM polls WHERE id = $1", poll_id)


async def db_claim_message(poll_id: int, number: int) -> bool:
    """Count message `number` (0 is the first) as sent. False if it already was, by another pass or pod."""
    row = await db_fetchrow(
        "UPDATE polls SET messages_sent = $2 + 1 WHERE id = $1 AND messages_sent = $2 RETURNING id",
        poll_id,
        number,
    )
    return row is not None


# Responses


async def db_upsert_response(poll_id: int, troop: str | None, respondent_id: int, response: ResponseUpdate):
    """The troop's response if a troop is given, else the person's own."""
    conflict = "(poll_id, troop)" if troop is not None else "(poll_id, respondent_id) WHERE troop IS NULL"
    return await db_fetchrow(
        f"""
        INSERT INTO poll_responses (poll_id, troop, respondent_id, status, comment)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT {conflict} DO UPDATE
        SET respondent_id = EXCLUDED.respondent_id, status = EXCLUDED.status,
            comment = EXCLUDED.comment, responded_at = NOW()
        RETURNING *
        """,
        poll_id,
        troop,
        respondent_id,
        response.status,
        response.comment,
    )


async def db_list_responses(poll_id: int):
    """The poll's responses: a troop poll's in troop number order (2 before 10), an individual poll's oldest first."""
    return await db_fetch(
        "SELECT * FROM poll_responses WHERE poll_id = $1 ORDER BY length(troop), troop, responded_at, id", poll_id
    )


async def db_list_troop_responses(troop: str):
    """Every response the troop has given, the poll that opened first first."""
    return await db_fetch(
        """
        SELECT r.* FROM poll_responses r JOIN polls p ON p.id = r.poll_id
        WHERE r.troop = $1
        ORDER BY p.opens_at, p.id
        """,
        troop,
    )


# --- Internal helpers ---

_message_task: asyncio.Task | None = None


def _now() -> datetime:
    """The time now. A function of its own so that tests can set the clock."""
    return datetime.now(tz=UTC)


def _state(poll, now: datetime) -> str:
    if now < poll["opens_at"]:
        return "upcoming"
    return "open" if now < poll["closes_at"] else "closed"


def _recipients(poll) -> list:
    """Who is in the poll: for a troop poll every troop that has leaders, in number order,
    for an individual poll the member ID of everyone in its audience, in order."""
    participants = get_single_project().participants
    if poll["type"] == "troop":
        troops = set()
        for info in participants.values():
            troops |= role_suffixes(info.get("roles") or [], "wsj27:al")
        return sorted(troops, key=lambda troop: (len(troop), troop))
    roles = [f"wsj27:{name}" for name in poll["audience"]]
    return sorted(member for member, info in participants.items() if has_any_role(info.get("roles") or [], roles))


async def _post_to_troop(troop: str, message: str) -> None:
    """Stands in for #38's notify.post_to_troop, which posts in the troop's leader channel on Discord."""
    logger.info("Message to troop %s not posted, no Discord yet: %s", troop, message)


async def _notify_person(member_id: int, message: str) -> None:
    """Stands in for the service that will message one person: a Discord direct message for leaders, Teams for CMT."""
    logger.info("Message to member %s not sent, no service yet: %s", member_id, message)


async def _send_due_messages() -> None:
    """One pass of the message loop: send each open poll's messages that have fallen due, oldest first."""
    now = _now()
    for poll in await db_list_polls():
        if _state(poll, now) != "open":
            continue
        troop_poll = poll["type"] == "troop"
        # How far into its open period the poll is, from 0 to 1.
        progress = (now - poll["opens_at"]) / (poll["closes_at"] - poll["opens_at"])
        # Without reminders, only the first message is ever due.
        for number in range(poll["messages_sent"], len(MESSAGES) if poll["reminders"] else 1):
            point, text = MESSAGES[number]
            if progress < point:
                break
            if not await db_claim_message(poll["id"], number):
                break  # Another pod got there first and sends it.

            recipients = _recipients(poll)
            if number > 0:  # A reminder goes only to those who have not responded.
                rows = await db_list_responses(poll["id"])
                responded = {row["troop"] if troop_poll else row["respondent_id"] for row in rows}
                recipients = [recipient for recipient in recipients if recipient not in responded]

            closes = poll["closes_at"].astimezone(STOCKHOLM)
            closes_text = f"{WEEKDAYS[closes.weekday()]} {closes.day} {MONTHS[closes.month - 1]} kl. {closes:%H:%M}"
            message = text.format(title=poll["title"], closes=closes_text)
            if settings.CAMPFIRE_URL:
                message += " " + settings.CAMPFIRE_URL.rstrip("/") + CAMPFIRE_POLL_PATH.format(poll_id=poll["id"])

            logger.info("Poll %d: sending message %d to %d recipients", poll["id"], number + 1, len(recipients))
            for recipient in recipients:
                if troop_poll:
                    await _post_to_troop(recipient, message)
                else:
                    await _notify_person(recipient, message)


async def _message_loop() -> None:
    while True:
        try:
            await _send_due_messages()
        except Exception:
            # The database or Discord being away must not end the loop; the next pass tries again.
            logger.exception("Sending poll messages failed")
        await asyncio.sleep(LOOP_SECONDS)


async def poll_init() -> None:
    """Create the tables and start sending messages."""
    global _message_task
    await db_init_tables()
    _message_task = asyncio.create_task(_message_loop())


async def poll_shutdown() -> None:
    if _message_task:
        _message_task.cancel()
        with suppress(asyncio.CancelledError):
            await _message_task


async def _require_poll_user(user: AuthUser = Depends(require_auth_user)) -> AuthUser:
    """CMT or a troop leader; everyone else is refused before the database is asked."""
    if not (user.has_role(RESULT_ROLE) or user.has_role(POLL_ADMIN_ROLE) or user.role_suffixes("wsj27:al")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to polls")
    return user


async def _require_poll_admin(user: AuthUser = Depends(require_auth_user)) -> AuthUser:
    if not user.has_role(POLL_ADMIN_ROLE):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to schedule polls")
    return user


def _may_see(user: AuthUser, poll) -> bool:
    """CMT sees every poll, a leader every troop poll and the individual polls put to leaders."""
    if user.has_any_role({POLL_ADMIN_ROLE, RESULT_ROLE}) or poll["type"] == "troop":
        return True
    return user.has_any_role([f"wsj27:{name}" for name in poll["audience"]])


async def _poll(poll_id: int, user: AuthUser = Depends(_require_poll_user)):
    """The poll's row, or 404, also for a poll the caller may not see."""
    row = await db_get_poll(poll_id)
    if row is None or not _may_see(user, row):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Poll not found")
    return row


# --- API routes ---

router = APIRouter(responses={403: {"description": "The caller's roles give no access to polls, or not to this."}})


@router.post(
    "",
    response_model=Poll,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule a poll",
    description=(
        "Adds a poll that opens at `opens_at` and closes at `closes_at`. Both need a time zone. "
        "A poll whose `opens_at` has passed is open at once, and those in it are told within a minute. "
        "An individual poll needs an `audience`, and a troop poll has none."
    ),
    responses={
        201: {"description": "The scheduled poll."},
        403: {"description": "The caller may not schedule polls."},
        422: {
            "description": "A time without a time zone, `closes_at` not after `opens_at`, a blank title, no, blank or "
            "repeated options, a repeated audience, or an audience that does not match the type."
        },
    },
)
async def create_poll(poll: PollCreate, user: AuthUser = Depends(_require_poll_admin)):
    row = await db_insert_poll(user.user_id, poll)
    return Poll(**row, state=_state(row, _now()))


@router.get(
    "",
    response_model=list[Poll],
    status_code=status.HTTP_200_OK,
    summary="List polls",
    description=(
        "Every poll the caller may see, the one that opens last first: CMT sees all, a leader the troop "
        "polls and the individual polls put to leaders. `state` keeps only the upcoming, open or closed ones."
    ),
    responses={200: {"description": "The polls."}},
)
async def list_polls(
    state: Literal["upcoming", "open", "closed"] | None = None, user: AuthUser = Depends(_require_poll_user)
):
    now = _now()
    polls = [Poll(**row, state=_state(row, now)) for row in await db_list_polls() if _may_see(user, row)]
    return [poll for poll in polls if state is None or poll.state == state]


@router.get(
    "/{poll_id}",
    response_model=Poll,
    status_code=status.HTTP_200_OK,
    summary="Get a poll",
    description="One poll, with its state right now.",
    responses={
        200: {"description": "The poll."},
        404: {"description": "No poll with this id that the caller may see."},
    },
)
async def get_poll(poll_id: int, poll=Depends(_poll)):
    return Poll(**poll, state=_state(poll, _now()))


@router.put(
    "/{poll_id}",
    response_model=Poll,
    status_code=status.HTTP_200_OK,
    summary="Change a poll",
    description=(
        "Changes the fields that are given and leaves the others. A poll that is open can still get a new "
        "`closes_at`, also one that closes it now, and have `reminders` turned on or off, but not get a new "
        "`opens_at`, `options` or `audience`. Messages that have already gone out are not sent again. The "
        "type never changes, and only an individual poll has an audience. A field sent with the value the poll "
        "already has is no change, so a poll read with GET can be sent back with only `closes_at` changed."
    ),
    responses={
        200: {"description": "The changed poll."},
        403: {"description": "The caller may not schedule polls."},
        404: {"description": "No poll with this id."},
        409: {
            "description": "The poll has closed, or has opened and the change is to `opens_at`, `options` or `audience`."
        },
        422: {
            "description": "A time without a time zone, `closes_at` not after `opens_at`, a blank title, no, blank or "
            "repeated options, a repeated audience, an empty audience for an individual poll, or a non-empty "
            "audience for a troop poll."
        },
    },
)
async def update_poll(
    poll_id: int, update: PollUpdate, user: AuthUser = Depends(_require_poll_admin), poll=Depends(_poll)
):
    state = _state(poll, _now())
    if state == "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Poll has closed")
    # An audience equal to the one the poll has is no change, so a poll read with GET can be sent back as it is.
    if poll["type"] == "troop" and update.audience:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="A troop poll has no audience")
    if poll["type"] == "individual" and update.audience == []:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="An individual poll needs an audience"
        )
    opens_at = update.opens_at or poll["opens_at"]
    closes_at = update.closes_at or poll["closes_at"]
    options = update.options or poll["options"]
    audience = poll["audience"] if update.audience is None else update.audience
    # Once open, it may have responses that only the options and audience it has make sense of.
    if state == "open" and (opens_at != poll["opens_at"] or options != poll["options"] or audience != poll["audience"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Poll has opened")
    if closes_at <= opens_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="closes_at must be after opens_at"
        )
    row = await db_update_poll(
        poll_id,
        poll["title"] if update.title is None else update.title,
        poll["description"] if update.description is None else update.description,
        opens_at,
        closes_at,
        options,
        audience,
        poll["reminders"] if update.reminders is None else update.reminders,
    )
    return Poll(**row, state=_state(row, _now()))


@router.delete(
    "/{poll_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a poll",
    description="Removes a poll that has not opened yet. One that has opened stays, with its responses.",
    responses={
        204: {"description": "The poll is removed."},
        403: {"description": "The caller may not schedule polls."},
        404: {"description": "No poll with this id."},
        409: {"description": "The poll has opened."},
    },
)
async def delete_poll(poll_id: int, user: AuthUser = Depends(_require_poll_admin), poll=Depends(_poll)):
    if _state(poll, _now()) != "upcoming":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Poll has opened")
    await db_delete_poll(poll_id)


@router.get(
    "/{poll_id}/responses",
    response_model=list[PollResponse],
    status_code=status.HTTP_200_OK,
    summary="List a poll's responses",
    description=(
        "The responses given so far. Whoever has not responded is not in the list. For a troop poll they come "
        "in troop number order, and CMT gets every troop's response, a leader only their own troop's. For an "
        "individual poll they come oldest first, and the head of contingent and the poll's creator get every "
        "response, everyone else only their own."
    ),
    responses={
        200: {"description": "The responses."},
        404: {"description": "No poll with this id that the caller may see."},
    },
)
async def list_responses(poll_id: int, poll=Depends(_poll), user: AuthUser = Depends(_require_poll_user)):
    rows = await db_list_responses(poll_id)
    if poll["type"] == "troop" and not user.has_role(RESULT_ROLE):
        own_troops = user.role_suffixes("wsj27:al")
        rows = [row for row in rows if row["troop"] in own_troops]
    if poll["type"] == "individual" and not (
        user.has_role(INDIVIDUAL_RESULT_ROLE) or user.user_id == poll["creator_id"]
    ):
        rows = [row for row in rows if row["respondent_id"] == user.user_id]
    return [PollResponse(**row) for row in rows]


@router.get(
    "/troops/{troop}/responses",
    response_model=list[PollResponse],
    status_code=status.HTTP_200_OK,
    summary="List a troop's responses",
    description=(
        "Every response the troop has given, in all polls, the poll that opened first first. A poll the "
        "troop has not responded to is not in the list; `GET /polls` has them all. CMT may ask for any "
        "troop, a leader only for their own."
    ),
    responses={
        200: {"description": "The troop's responses."},
        403: {"description": "The caller is neither CMT nor a leader of this troop."},
    },
)
async def list_troop_responses(troop: str, user: AuthUser = Depends(_require_poll_user)):
    if not (user.has_role(RESULT_ROLE) or troop in user.role_suffixes("wsj27:al")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Not a leader of troop {troop}")
    return [PollResponse(**row) for row in await db_list_troop_responses(troop)]


@router.put(
    "/{poll_id}/response",
    response_model=PollResponse,
    status_code=status.HTTP_200_OK,
    summary="Respond to a poll",
    description=(
        "Sets the caller's response while the poll is open, replacing the one there was. For a troop poll it "
        "is the response of the caller's own troop: any of the troop's leaders may give it, and the response "
        "records who did it last. For an individual poll it is the caller's own, and the caller must be in "
        "the poll's audience."
    ),
    responses={
        200: {"description": "The response."},
        403: {"description": "The caller is not a troop leader (troop poll) or not in the audience (individual poll)."},
        404: {"description": "No poll with this id that the caller may see."},
        409: {"description": "The poll has not opened yet or has closed."},
        422: {"description": "`status` is not one of the poll's `options`."},
    },
)
async def respond(
    poll_id: int, response: ResponseUpdate, poll=Depends(_poll), user: AuthUser = Depends(_require_poll_user)
):
    if poll["type"] == "troop":
        # Answered for the caller's own troop, which their leader role names. A person has one
        # member type and one troop in Scoutnet, so there is never more than one such role.
        troops = user.role_suffixes("wsj27:al")
        if not troops:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a troop leader")
        (troop,) = troops
    else:
        if not user.has_any_role([f"wsj27:{name}" for name in poll["audience"]]):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not in the poll's audience")
        troop = None
    if _state(poll, _now()) != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Poll is not open")
    if response.status not in poll["options"]:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Not one of the poll's options")
    return PollResponse(**await db_upsert_response(poll_id, troop, user.user_id, response))
