"""Which tokens decode_access_token() accepts.

A valid signature only proves which key signed a token. These pin down the rest:
the issuer must be the auth-api we discovered, the audience must be ours, and a
key we haven't seen makes us look again, but not on every request.

The discovery fetch is stubbed, so nothing here touches the network.
"""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from app import authenctication as auth

ISSUER = "https://campfire.example/api/auth"
OTHER_ISSUER = "https://campfire.other.example/api/auth"
REQUEST = SimpleNamespace(base_url="http://testserver")


def _key(kid: str) -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": kid})


KEY = _key("current")
NEW_KEY = _key("rotated")


def _token(key: RSAKey = KEY, **claims) -> str:
    now = int(time.time())
    base = {"iss": ISSUER, "aud": "wsj27", "sub": "x", "iat": now, "exp": now + 300}
    base.update(claims)
    base = {k: v for k, v in base.items() if v is not None}
    return jwt.encode({"alg": "RS256", "kid": key.kid}, base, key)


def _server(*keys: RSAKey, issuer: str = ISSUER) -> auth._AuthServer:
    keyset = KeySet.import_key_set({"keys": [k.as_dict(private=False) for k in keys]})
    return auth._AuthServer(issuer=issuer, keyset=keyset, fetched_at=time.monotonic())


@pytest.fixture
def discovery(monkeypatch):
    """Stub the discovery fetch. Set `.serves` to what auth-api publishes."""
    state = SimpleNamespace(serves=_server(KEY), fetches=0)

    async def _fake_fetch(base):
        state.fetches += 1
        return state.serves

    monkeypatch.setattr(auth, "_fetch_auth_server", _fake_fetch)
    monkeypatch.setattr(auth, "_auth_server_cache", {})
    return state


def _decode(token: str) -> dict:
    return asyncio.run(auth.decode_access_token(token, REQUEST))


def _status(token: str) -> int:
    with pytest.raises(HTTPException) as exc:
        _decode(token)
    return exc.value.status_code


def test_accepts_own_token(discovery):
    assert _decode(_token())["sub"] == "x"


def test_rejects_another_issuer_with_the_same_key(discovery):
    # The case that matters: another environment's auth-api signing with our key.
    assert _status(_token(iss=OTHER_ISSUER)) == 401


@pytest.mark.parametrize("claims", [{"iss": None}, {"aud": None}, {"aud": "wsj27-impersonation"}, {"aud": "other"}])
def test_rejects_missing_or_wrong_claims(discovery, claims):
    assert _status(_token(**claims)) == 401


def test_unknown_key_fetches_the_key_set_again(discovery):
    _decode(_token())  # caches the old key set
    discovery.serves = _server(NEW_KEY)
    # Pretend the cached copy is old enough to refresh.
    for k, v in auth._auth_server_cache.items():
        auth._auth_server_cache[k] = replace(v, fetched_at=v.fetched_at - auth._KEY_REFRESH_INTERVAL_SECONDS)

    assert _decode(_token(NEW_KEY))["sub"] == "x"
    assert discovery.fetches == 2


def test_unknown_keys_refetch_at_most_once_per_interval(discovery):
    _decode(_token())
    stranger = _key("stranger")
    for _ in range(5):
        assert _status(_token(stranger)) == 401
    assert discovery.fetches == 1  # the first fetch is younger than the interval


def test_failed_refresh_keeps_the_cached_keys(discovery):
    _decode(_token())
    discovery.serves = None
    for k, v in auth._auth_server_cache.items():
        auth._auth_server_cache[k] = replace(v, fetched_at=v.fetched_at - auth._KEY_REFRESH_INTERVAL_SECONDS)

    assert _status(_token(_key("stranger"))) == 401
    assert _decode(_token())["sub"] == "x"  # still verifying with what we had


def test_no_discovery_is_503(discovery):
    discovery.serves = None
    assert _status(_token()) == 503
