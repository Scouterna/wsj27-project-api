import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException, Request, status
from joserfc import jws, jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from pydantic import BaseModel, Field

from .active_users import track_user
from .config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AuthServer:
    """What we trust about auth-api, read from its discovery document."""

    issuer: str
    keyset: KeySet
    fetched_at: float


_auth_server_cache: dict[str, _AuthServer] = {}

# A token whose `kid` we don't know makes us fetch the key set again, so a key
# rotation in auth-api is picked up without restarting us. Anyone can send such a
# token, so at most once per this many seconds.
_KEY_REFRESH_INTERVAL_SECONDS = 60


# --- Checking roles -----------------------------------------------------------
#
# Roles are colon-separated hierarchies ("wsj27:cmt:admin:it"), so an
# authorization check is a prefix match: holding "wsj27:cmt:admin:it" satisfies a
# requirement of "wsj27:cmt". These are the only two questions the hierarchy can
# answer, and they are different:
#
#   has_role()      "may this person do X at all"
#   role_suffixes() "which things is their authority scoped to"
#
# A troop-scoped role like "wsj27:al:38" is a permission *and* a scope, so
# has_role(user, "wsj27:al") — "is a leader" — is almost never the question worth
# asking. The question is which troop, and that is role_suffixes().
#
# These live here, with AuthUser, rather than in roles.py: this is the consuming
# side of the role model, and roles.py is deliberately a leaf that nothing
# imports. The cost is that the role *shape* is now known in two places, which
# tests/test_role_checks.py pins so the two cannot drift apart silently.


def _segments(role: str) -> list[str]:
    return role.split(":")


def has_role(held_roles: list[str], required: str) -> bool:
    """True if any held role is `required` or a more specific role beneath it.

    Compared segment-wise, never with str.startswith(): "wsj27:cmtx" starts with
    "wsj27:cmt" as a string but is an unrelated role, and treating it as a match
    would grant access nobody was given.

    An exact match counts, so this is a safe drop-in for an equality check.
    """
    required_segments = _segments(required)
    depth = len(required_segments)
    return any(_segments(role)[:depth] == required_segments for role in held_roles)


def has_any_role(held_roles: list[str], required: list[str] | set[str] | frozenset[str]) -> bool:
    """True if `has_role` holds for any one of `required`."""
    return any(has_role(held_roles, requirement) for requirement in required)


def role_suffixes(held_roles: list[str], prefix: str) -> set[str]:
    """The trailing segments of every held role strictly beneath `prefix`.

    `role_suffixes(roles, "wsj27:al")` -> `{"38", "44"}`: the troops this
    leader's authority covers. An empty set means no roles under that prefix,
    which is the answer for someone who is not a leader at all — so callers must
    treat "empty" as "nothing", never as "everything".

    The bare `prefix` itself yields nothing, since it names no specific scope.
    """
    prefix_segments = _segments(prefix)
    depth = len(prefix_segments)
    return {
        ":".join(segments[depth:])
        for role in held_roles
        if (segments := _segments(role))[:depth] == prefix_segments and len(segments) > depth
    }


class AuthUser(BaseModel):
    name: str
    preferred_username: str
    given_name: str
    family_name: str
    email: str | None = None
    member_no: str
    roles: list[str] = Field(default_factory=list)
    # The member's avatar. Null for plenty of people, and carried by the token
    # alone - Scoutnet's participant data has no such field, which is why
    # _store_token_values() below keeps a copy where the rest of the app can read it.
    picture: str | None = None
    # The member's @scouterna.se address, their Teams/Entra login. Scoutnet has it
    # in a profile field the project feed leaves out, so it too reaches this app
    # in the token alone and is kept the same way.
    scouterna_email: str | None = None

    def __str__(self) -> str:
        uid = self.preferred_username or self.member_no
        return f"{self.name} ({uid})"

    @property
    def user_id(self) -> int:
        """The scoutnet member ID, i.e. the `memberNo` claim."""
        return int(self.member_no)

    # Thin wrappers over the module-level checks below, so that endpoints read as
    # questions about the user rather than about a list of strings.

    def has_role(self, required: str) -> bool:
        """True if the user holds `required` or a more specific role beneath it."""
        return has_role(self.roles, required)

    def has_any_role(self, required: list[str] | set[str] | frozenset[str]) -> bool:
        """True if the user satisfies any one of `required`."""
        return has_any_role(self.roles, required)

    def role_suffixes(self, prefix: str) -> set[str]:
        """Scopes the user holds under `prefix`, e.g. their troops for wsj27:al."""
        return role_suffixes(self.roles, prefix)


async def get_auth_server(request: Request, *, refresh: bool = False) -> _AuthServer | None:
    """
    auth-api's issuer and key set, from its discovery document.

    Cached; `refresh` fetches again unless the cached copy is younger than
    _KEY_REFRESH_INTERVAL_SECONDS. A failed fetch returns the cached copy if there
    is one, so auth-api being briefly unreachable doesn't lock everyone out.
    """
    cache_key = settings.OIDC_SERVER_PATH or (str(request.base_url) + "/auth")

    cached = _auth_server_cache.get(cache_key)
    if cached and (not refresh or time.monotonic() - cached.fetched_at < _KEY_REFRESH_INTERVAL_SECONDS):
        return cached

    fetched = await _fetch_auth_server(cache_key)
    if fetched is None:
        if cached:
            # Keep throttling while auth-api is down, or every unknown-kid token
            # would wait out a fetch timeout.
            _auth_server_cache[cache_key] = replace(cached, fetched_at=time.monotonic())
        return cached
    _auth_server_cache[cache_key] = fetched
    return fetched


async def _fetch_auth_server(base: str) -> _AuthServer | None:
    url = urljoin(base, ".well-known/openid-configuration")
    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            response = await http_client.get(url)
            response.raise_for_status()
            oid_config = response.json()
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None

    # Without an issuer to compare against we could only check signatures, which
    # would accept a token from any auth-api instance that shares the key.
    issuer = oid_config.get("issuer")
    url = oid_config.get("jwks_uri")
    if not issuer or not url:
        logger.warning("Discovery document at %s lacks issuer or jwks_uri", base)
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            response = await http_client.get(url)
            response.raise_for_status()
            jwks_dict = response.json()
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None

    try:
        keyset = KeySet.import_key_set(jwks_dict)
    except Exception as exc:
        logger.warning("Failed to parse JWKS: %s", exc)
        return None
    return _AuthServer(issuer=issuer, keyset=keyset, fetched_at=time.monotonic())


def _token_kid(token: str) -> str | None:
    try:
        kid = jws.extract_compact(token.encode()).headers().get("kid")
    except JoseError:
        return None  # not a JWS at all; jwt.decode() below says so properly
    return kid if isinstance(kid, str) else None


def _knows_kid(keyset: KeySet, kid: str) -> bool:
    return any(key.kid == kid for key in keyset.keys)


async def decode_access_token(token: str, request: Request) -> dict[str, Any]:
    server = await get_auth_server(request)
    if server is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Token validation unavailable")

    kid = _token_kid(token)
    if kid and not _knows_kid(server.keyset, kid):
        server = await get_auth_server(request, refresh=True) or server

    try:
        token_obj = jwt.decode(token, server.keyset)
        # A valid signature only says which key signed it. iss pins the auth-api
        # instance, so another environment's tokens fail even if it shared our key;
        # aud excludes tokens auth-api mints for other purposes.
        registry = jwt.JWTClaimsRegistry(
            leeway=30,
            iss={"essential": True, "value": server.issuer},
            aud={"essential": True, "value": settings.OIDC_AUDIENCE},
        )
        registry.validate(token_obj.claims)
        return dict(token_obj.claims)
    except Exception as exc:
        logger.warning("Failed to validate JWT: %s", exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized") from exc


def _extract_token(request: Request) -> str | None:
    token = request.cookies.get("wsj27-auth_access-token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :]
    return token or None


def _extract_roles(claims: dict[str, Any]) -> list[str]:
    """Flatten our role claims back into a single list.

    Same rules as the consumer-side helper: realm roles bare, client roles
    namespaced "client:role".
    """
    roles: set[str] = set()

    realm_access = claims.get("realm_access") or {}
    for role in realm_access.get("roles") or []:
        if isinstance(role, str):
            roles.add(role)

    resource_access = claims.get("resource_access") or {}
    for client, resource in resource_access.items():
        if not isinstance(resource, dict):
            continue
        for role in resource.get("roles") or []:
            if isinstance(role, str):
                roles.add(f"{client}:{role}")

    return sorted(roles)


# --- Values only the token carries ------------------------------------------
#
# A member's avatar URL and Scouterna address reach this app in the token and
# nowhere else: the participant feed has neither, so the only moment we can
# learn them is while serving a request for that member. Every authenticated
# request therefore offers the current values to scoutnet_db, which keeps them
# where the rest of the app - and other readers of the participant record - can
# see them.
#
# That is only affordable because ensure_values() costs a dict lookup against
# the cached copy and sends nothing unless a value actually changed.

# create_task keeps only a weak reference to a running task, so a set holds them
# until they finish. Fire-and-forget on purpose: a Scoutnet round trip must not
# land on the request path, and Scoutnet being down must not break authentication.
_token_value_tasks: set[asyncio.Task] = set()


def _store_token_values(user: AuthUser) -> None:
    """Offer this caller's avatar URL and Scouterna address to scoutnet_db, in the background."""
    # Deferred: scoutnet_db -> scoutnet -> authenctication would be an import cycle.
    from . import scoutnet_db

    if not user.member_no.isdigit():
        return
    # A claim that is missing leaves the stored value alone rather than clearing it.
    claims = {"avatar_url": user.picture, "scouterna_email": user.scouterna_email}
    values = {key: value for key, value in claims.items() if value}
    if not values:
        return
    # Asked synchronously first, so the overwhelmingly common case - the stored
    # values already match - costs a dict lookup rather than a Task per request.
    # ensure_values() repeats the check under its lock, which is what actually
    # keeps concurrent callers from each writing.
    if not scoutnet_db.would_change(user.user_id, values):
        return
    task = asyncio.create_task(scoutnet_db.ensure_values(user.user_id, values))
    _token_value_tasks.add(task)
    task.add_done_callback(_token_value_tasks.discard)
    task.add_done_callback(_log_store_failure)


def _log_store_failure(task: asyncio.Task) -> None:
    """Report a failed write rather than leaving an unretrieved task exception."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.warning("Failed to store token values: %s: %s", type(exc).__name__, exc)


async def require_auth_user(request: Request) -> AuthUser:
    """
    FastAPI dependency that validates the auth cookie/bearer token and returns the WSJ27 user.
    """
    token = _extract_token(request)
    if not token:
        # Logged, because "no token at all" and "a token we rejected" are the same
        # 401 to the caller but completely different faults, and only the latter
        # reaches the warning in decode_access_token(). A burst of these means the
        # auth cookie lapsed before the browser renewed it -- an expiry race in the
        # front end, not a bad token -- and without this line the logs cannot say so.
        logger.info("No token presented for %s %s", request.method, request.url.path)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    claims = await decode_access_token(token, request)
    roles = _extract_roles(claims)
    if not any(role.startswith("wsj27:") for role in roles):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No suitable roles")

    user = AuthUser(
        name=claims.get("name") or "",
        preferred_username=claims.get("preferred_username") or "",
        given_name=claims.get("given_name") or "",
        family_name=claims.get("family_name") or "",
        email=claims.get("email"),
        member_no=claims.get("member_no") or "",
        roles=roles,
        picture=claims.get("picture"),
        scouterna_email=claims.get("scouterna_email"),
    )
    await track_user(user.preferred_username or user.member_no)
    _store_token_values(user)
    return user
