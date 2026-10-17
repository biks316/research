"""JWT bearer-token verification."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import jwt
import structlog

from app.config import HMAC_ALGORITHMS, Settings
from app.core.errors import Unauthorized

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Principal:
    user_id: str


class TokenVerifier:
    def __init__(self, settings: Settings) -> None:
        self._algorithm = settings.JWT_ALGORITHM
        self._issuer = settings.JWT_ISSUER
        self._audience = settings.JWT_AUDIENCE
        self._leeway = settings.JWT_LEEWAY_SECONDS
        self._secret = settings.JWT_SECRET.get_secret_value() if settings.JWT_SECRET else None
        self._jwks = (
            jwt.PyJWKClient(settings.JWT_JWKS_URL, cache_keys=True, lifespan=3600)
            if settings.JWT_JWKS_URL and self._algorithm not in HMAC_ALGORITHMS
            else None
        )

    async def _key_for(self, token: str) -> object:
        if self._algorithm in HMAC_ALGORITHMS:
            return self._secret
        assert self._jwks is not None
        # PyJWKClient does blocking HTTP; keep it off the event loop.
        signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
        return signing_key.key

    async def verify(self, token: str) -> Principal:
        try:
            key = await self._key_for(token)
            claims = jwt.decode(
                token,
                key=key,  # type: ignore[arg-type]
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise Unauthorized("Token has expired.") from None
        except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
            log.info("token_rejected", reason=type(exc).__name__)
            raise Unauthorized("Missing or invalid bearer token.") from None
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise Unauthorized("Token has no subject.")
        return Principal(user_id=sub)
