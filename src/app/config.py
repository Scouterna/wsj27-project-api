"""Application settings, read from the environment (and a local .env file).

`env_file=".env"` resolves relative to the working directory, so run the app from
`src/` — as start.py's launch config and the container CMD both do.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProjectConfig(BaseModel):
    """Configuration for a single Scoutnet project."""

    id: int
    name: str
    member_key: str
    question_key: str
    group_key: str = ""  # Optional; empty string = no groups for this project


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    # --- Scoutnet ---
    SCOUTNET_PROJECTS: list[ProjectConfig]
    SCOUTNET_BODYLIST_ID: int = 692
    SCOUTNET_BODYLIST_KEY: str = ""
    # Development only: cache raw Scoutnet GET responses in .dev_cache/ and serve
    # them instead of calling Scoutnet, for a fast startup. Delete the directory
    # to fetch fresh data. Never set this in a deployment — it would freeze the
    # app on whatever data happened to be cached first.
    SCOUTNET_DEV_CACHE: bool = False
    PERSIST_DIR: Path = Path("/app/persist")  # Must match volume mountPath
    # --- CMT detail roles ---
    # CSV mapping member number -> CMT function and role, mounted from a
    # ConfigMap rather than baked into the image so it can change without a
    # rebuild. Unset (or a missing file) means CMT members just get the plain
    # wsj27:cmt role, as they did before this file existed.
    CMT_ROLES_FILE: Path | None = None
    # --- Database ---
    # Optional: unset means no database. The app then starts without a
    # connection and drops the /cases router, which is the only feature that
    # needs one; participants, roles and Scoutnet data are unaffected.
    # Temporary, for clusters that have no Postgres yet (scoutweb) — set this
    # as soon as one is available.
    POSTGRES_DSN: str = ""
    # --- Serving ---
    ROOT_PATH: str = ""
    PORT: int = 8000
    DEBUG: bool = False
    AUTH_DISABLED: bool = False
    # --- Fake user used when AUTH_DISABLED is set ---
    # member_no is derived from the part after "scoutnet|" in preferred_username.
    FAKE_USER_PREFERRED_USERNAME: str = "scoutnet|1234567"
    # Roles are matched case-sensitively, and the ones roles.py mints are
    # lowercase, so this default has to be too — "wsj27:CMT:Admin" is not the
    # same role as "wsj27:cmt:admin" and satisfies no check at all. Override to
    # develop as someone else: add "wsj27:access:Hälsa plus intern information"
    # for health data, or use ["wsj27:al:18"] to be an Avdelningsledare.
    FAKE_USER_ROLES: list[str] = ["wsj27:cmt:admin"]
    # --- OIDC server DNS ---
    OIDC_SERVER_PATH: str = ""  # Default is to get the path from the request
    # Drop the Secure attribute so cookies work over plain HTTP locally.
    INSECURE_COOKIES: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
