import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .authenctication import AuthUser, require_auth_user
from .config import get_settings
from .db import db_execute, db_fetch, db_fetchrow, db_transaction

settings = get_settings()
logger = logging.getLogger(__name__)

# Finite but extendable set of case types; each sets up a basic access level for the
# case. The type -> access-level mapping itself is not implemented yet.
CASE_TYPES = ["general", "medical", "safeguarding", "security", "logistics"]


# --- Database functions ---


async def db_init_tables() -> None:
    logger.info("Initializing case database tables")
    case_types_list = ", ".join(f"'{t}'" for t in CASE_TYPES)
    await db_execute(f"""
        CREATE TABLE IF NOT EXISTS cases (
            id                BIGSERIAL    PRIMARY KEY,
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            creator_id        BIGINT       NOT NULL,
            secrecy_level     SMALLINT     NOT NULL CHECK (secrecy_level BETWEEN 1 AND 5),
            title             TEXT         NOT NULL,
            type              TEXT         NOT NULL CHECK (type IN ({case_types_list})),
            about_person_id   BIGINT,
            assigned_to_id    BIGINT,
            troop             TEXT         NOT NULL,
            latest_note_at    TIMESTAMPTZ,
            closed            BOOLEAN      NOT NULL DEFAULT false,
            closed_at         TIMESTAMPTZ,
            closed_by_id      BIGINT,
            extra_access      BIGINT[]     NOT NULL DEFAULT '{{}}',
            tags              TEXT[]       NOT NULL DEFAULT '{{}}'
        )
    """)
    await db_execute("CREATE INDEX IF NOT EXISTS cases_about_person_idx  ON cases (about_person_id)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_assigned_to_idx   ON cases (assigned_to_id)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_troop_idx         ON cases (troop)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_created_at_idx    ON cases (created_at)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_closed_idx        ON cases (closed)")
    await db_execute("CREATE INDEX IF NOT EXISTS cases_type_idx          ON cases (type)")

    await db_execute("""
        CREATE TABLE IF NOT EXISTS case_notes (
            id             BIGSERIAL    PRIMARY KEY,
            case_id        BIGINT       NOT NULL REFERENCES cases (id),
            created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            creator_id     BIGINT       NOT NULL,
            secrecy_level  SMALLINT     NOT NULL CHECK (secrecy_level BETWEEN 1 AND 5),
            title          TEXT         NOT NULL,
            note           TEXT         NOT NULL,
            extra_access   BIGINT[]     NOT NULL DEFAULT '{}',
            tags           TEXT[]       NOT NULL DEFAULT '{}'
        )
    """)
    await db_execute("CREATE INDEX IF NOT EXISTS case_notes_case_id_idx ON case_notes (case_id)")

    await db_execute("""
        CREATE TABLE IF NOT EXISTS case_note_read_log (
            id         BIGSERIAL    PRIMARY KEY,
            case_id    BIGINT       NOT NULL REFERENCES cases (id),
            reader_id  BIGINT       NOT NULL,
            timestamp  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)
    await db_execute("CREATE INDEX IF NOT EXISTS case_note_read_log_case_id_idx ON case_note_read_log (case_id)")

    await db_execute("CREATE INDEX IF NOT EXISTS cases_tags_idx      ON cases USING GIN (tags)")
    await db_execute("CREATE INDEX IF NOT EXISTS case_notes_tags_idx ON case_notes USING GIN (tags)")

    logger.info("Case database tables ready")


# --- Models ---


class CaseCreate(BaseModel):
    secrecy_level: int = Field(ge=1, le=5)
    title: str
    type: str
    about_person_id: int | None = None
    troop: str
    extra_access: list[int] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Case(BaseModel):
    id: int
    created_at: datetime
    creator_id: int
    secrecy_level: int
    title: str
    type: str
    about_person_id: int | None
    assigned_to_id: int | None
    troop: str
    latest_note_at: datetime | None
    closed: bool
    closed_at: datetime | None
    closed_by_id: int | None
    extra_access: list[int]
    tags: list[str]


class ExtraAccessUpdate(BaseModel):
    extra_access: list[int]


class TagsUpdate(BaseModel):
    tags: list[str]


class AssigneeUpdate(BaseModel):
    assigned_to_id: int | None


class NoteCreate(BaseModel):
    secrecy_level: int = Field(ge=1, le=5)
    title: str
    note: str
    extra_access: list[int] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Note(BaseModel):
    id: int
    case_id: int
    created_at: datetime
    creator_id: int
    secrecy_level: int
    title: str
    note: str
    extra_access: list[int]
    tags: list[str]


# --- API routes ---

router = APIRouter()


@router.post(
    "",
    response_model=Case,
    status_code=status.HTTP_201_CREATED,
    response_description="The created case",
)
async def create_case(case: CaseCreate, user: AuthUser = Depends(require_auth_user)):
    if case.type not in CASE_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Invalid case type: {case.type}")

    row = await db_fetchrow(
        """
        INSERT INTO cases (creator_id, secrecy_level, title, type, about_person_id, troop, extra_access, tags)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING *
        """,
        user.user_id,
        case.secrecy_level,
        case.title,
        case.type,
        case.about_person_id,
        case.troop,
        case.extra_access,
        case.tags,
    )
    return Case(**row)


@router.get(
    "",
    response_model=list[Case],
    status_code=status.HTTP_200_OK,
    response_description="List of cases, newest first",
)
async def list_cases(
    about_person_id: int | None = None,
    troop: str | None = None,
    type: str | None = None,
    tag: str | None = None,
    not_older_than: datetime | None = None,
    include_closed: bool = False,
    user: AuthUser = Depends(require_auth_user),
):
    conditions = []
    args = []

    if about_person_id is not None:
        args.append(about_person_id)
        conditions.append(f"c.about_person_id = ${len(args)}")
    if troop is not None:
        args.append(troop)
        conditions.append(f"c.troop = ${len(args)}")
    if type is not None:
        args.append(type)
        conditions.append(f"c.type = ${len(args)}")
    if not_older_than is not None:
        args.append(not_older_than)
        conditions.append(f"c.created_at >= ${len(args)}")
    if not include_closed:
        conditions.append("NOT c.closed")
    if tag is not None:
        args.append(tag)
        conditions.append(f"c.tags @> ARRAY[${len(args)}]::TEXT[]")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = await db_fetch(
        f"SELECT c.* FROM cases c {where_clause} ORDER BY c.created_at DESC",
        *args,
    )
    return [Case(**row) for row in rows]


@router.post(
    "/{case_id}/close",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    response_description="The closed case",
)
async def close_case(case_id: int, user: AuthUser = Depends(require_auth_user)):
    row = await db_fetchrow(
        """
        UPDATE cases
        SET closed = true, closed_at = NOW(), closed_by_id = $2
        WHERE id = $1 AND NOT closed
        RETURNING *
        """,
        case_id,
        user.user_id,
    )
    if row is None:
        existing = await db_fetchrow("SELECT id FROM cases WHERE id = $1", case_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is already closed")
    return Case(**row)


@router.post(
    "/{case_id}/reopen",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    response_description="The reopened case",
)
async def reopen_case(case_id: int, user: AuthUser = Depends(require_auth_user)):
    row = await db_fetchrow(
        """
        UPDATE cases
        SET closed = false, closed_at = NULL, closed_by_id = NULL
        WHERE id = $1 AND closed
        RETURNING *
        """,
        case_id,
    )
    if row is None:
        existing = await db_fetchrow("SELECT id FROM cases WHERE id = $1", case_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is not closed")
    return Case(**row)


@router.post(
    "/{case_id}/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    response_description="The created note",
)
async def create_note(case_id: int, note: NoteCreate, user: AuthUser = Depends(require_auth_user)):
    case_row = await db_fetchrow("SELECT secrecy_level, closed FROM cases WHERE id = $1", case_id)
    if case_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    if case_row["closed"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Case is closed")
    if note.secrecy_level < case_row["secrecy_level"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Note secrecy level must be equal or higher than the case's secrecy level",
        )

    async with db_transaction() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO case_notes (case_id, creator_id, secrecy_level, title, note, extra_access, tags)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            case_id,
            user.user_id,
            note.secrecy_level,
            note.title,
            note.note,
            note.extra_access,
            note.tags,
        )
        await conn.execute(
            "UPDATE cases SET latest_note_at = $2 WHERE id = $1",
            case_id,
            row["created_at"],
        )
        return Note(**row)


@router.get(
    "/{case_id}/notes",
    response_model=list[Note],
    status_code=status.HTTP_200_OK,
    response_description="List of notes for the case, newest first",
)
async def get_case_notes(case_id: int, user: AuthUser = Depends(require_auth_user)):
    case_row = await db_fetchrow("SELECT id FROM cases WHERE id = $1", case_id)
    if case_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")

    rows = await db_fetch("SELECT * FROM case_notes WHERE case_id = $1 ORDER BY created_at DESC", case_id)
    await db_execute(
        "INSERT INTO case_note_read_log (case_id, reader_id) VALUES ($1, $2)",
        case_id,
        user.user_id,
    )
    return [Note(**row) for row in rows]


@router.put(
    "/{case_id}/extra_access",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    response_description="The case with its extra_access list replaced",
)
async def update_case_extra_access(
    case_id: int, update: ExtraAccessUpdate, user: AuthUser = Depends(require_auth_user)
):
    row = await db_fetchrow(
        "UPDATE cases SET extra_access = $2 WHERE id = $1 RETURNING *",
        case_id,
        update.extra_access,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return Case(**row)


@router.put(
    "/{case_id}/notes/{note_id}/extra_access",
    response_model=Note,
    status_code=status.HTTP_200_OK,
    response_description="The note with its extra_access list replaced",
)
async def update_note_extra_access(
    case_id: int, note_id: int, update: ExtraAccessUpdate, user: AuthUser = Depends(require_auth_user)
):
    row = await db_fetchrow(
        "UPDATE case_notes SET extra_access = $3 WHERE id = $2 AND case_id = $1 RETURNING *",
        case_id,
        note_id,
        update.extra_access,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return Note(**row)


@router.put(
    "/{case_id}/assignee",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    response_description="The case with its assignee updated",
)
async def update_case_assignee(case_id: int, update: AssigneeUpdate, user: AuthUser = Depends(require_auth_user)):
    row = await db_fetchrow(
        "UPDATE cases SET assigned_to_id = $2 WHERE id = $1 RETURNING *",
        case_id,
        update.assigned_to_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return Case(**row)


@router.put(
    "/{case_id}/tags",
    response_model=Case,
    status_code=status.HTTP_200_OK,
    response_description="The case with its tags replaced",
)
async def update_case_tags(case_id: int, update: TagsUpdate, user: AuthUser = Depends(require_auth_user)):
    row = await db_fetchrow(
        "UPDATE cases SET tags = $2 WHERE id = $1 RETURNING *",
        case_id,
        update.tags,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return Case(**row)


@router.put(
    "/{case_id}/notes/{note_id}/tags",
    response_model=Note,
    status_code=status.HTTP_200_OK,
    response_description="The note with its tags replaced",
)
async def update_note_tags(case_id: int, note_id: int, update: TagsUpdate, user: AuthUser = Depends(require_auth_user)):
    row = await db_fetchrow(
        "UPDATE case_notes SET tags = $3 WHERE id = $2 AND case_id = $1 RETURNING *",
        case_id,
        note_id,
        update.tags,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return Note(**row)


@router.get(
    "/tags",
    response_model=list[str],
    status_code=status.HTTP_200_OK,
    response_description="All existing tag names",
)
async def list_tags(user: AuthUser = Depends(require_auth_user)):
    rows = await db_fetch("""
        SELECT DISTINCT tag FROM (
            SELECT unnest(tags) AS tag FROM cases
            UNION
            SELECT unnest(tags) AS tag FROM case_notes
        ) all_tags
        ORDER BY tag
    """)
    return [row["tag"] for row in rows]


@router.get(
    "/types",
    response_model=list[str],
    status_code=status.HTTP_200_OK,
    response_description="All valid case types",
)
async def list_case_types(user: AuthUser = Depends(require_auth_user)):
    return CASE_TYPES
