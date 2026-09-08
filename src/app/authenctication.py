import logging
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException, Request, status
from joserfc import jwt
from joserfc.jwk import KeySet
from pydantic import BaseModel, Field

from .config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

_jwks_keyset_cache: dict[str, KeySet] = {}


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

# The access level that unlocks health and other internal participant data.
# Named rather than spelled out at each call site: it is a Swedish sentence used
# as an identifier, and a typo in it fails open-endedly rather than loudly. It
# must match what roles.roles_for_participant() mints for that access level.
ACCESS_HEALTH_INTERNAL = "wsj27:access:Hälsa plus intern information"


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


async def get_jwks_keyset(request: Request) -> KeySet | None:
    """
    Convert the JWKS dictionary into a key set usable for verification.
    """
    cache_key = settings.OIDC_SERVER_PATH or (str(request.base_url) + "/auth")

    if cache_key in _jwks_keyset_cache:
        return _jwks_keyset_cache[cache_key]

    url = urljoin(str(cache_key), ".well-known/openid-configuration")
    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            response = await http_client.get(url)
            response.raise_for_status()
            oid_config = response.json()
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None

    url = oid_config["jwks_uri"]
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
        _jwks_keyset_cache[cache_key] = keyset
        return keyset
    except Exception as exc:
        logger.warning("Failed to parse JWKS: %s", exc)
        return None


async def decode_access_token(token: str, request: Request) -> dict[str, Any]:
    keyset = await get_jwks_keyset(request)
    if keyset is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Token validation unavailable")

    try:
        token_obj = jwt.decode(token, keyset)
        registry = jwt.JWTClaimsRegistry(leeway=30)
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


async def require_auth_user(request: Request) -> AuthUser:
    """
    FastAPI dependency that validates the auth cookie/bearer token and returns the WSJ27 user.
    """
    token = _extract_token(request)
    if not token:
        if not settings.AUTH_DISABLED:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        else:  # Authentication disabled. Return a fake user, configurable via env vars.
            _, _, member_no = settings.FAKE_USER_PREFERRED_USERNAME.partition("|")
            return AuthUser(
                name="Fake User",
                preferred_username=settings.FAKE_USER_PREFERRED_USERNAME,
                given_name="Fake",
                family_name="User",
                email="fake.user@scouterna.se",
                member_no=member_no,
                roles=settings.FAKE_USER_ROLES,
            )

    claims = await decode_access_token(token, request)
    roles = _extract_roles(claims)
    if not any(role.startswith("wsj27:") for role in roles):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No suitable roles")

    return AuthUser(
        name=claims.get("name") or "",
        preferred_username=claims.get("preferred_username") or "",
        given_name=claims.get("given_name") or "",
        family_name=claims.get("family_name") or "",
        email=claims.get("email"),
        member_no=claims.get("member_no") or "",
        roles=roles,
    )
