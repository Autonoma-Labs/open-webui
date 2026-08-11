"""Autonoma Environment Factory endpoint.

Autonoma seeds isolated test data before each end-to-end run and removes it
afterwards by calling one signed endpoint - mounted at ``/api/autonoma`` - which
dispatches ``discover`` / ``up`` / ``down`` through the SDK handler into the
factories in :mod:`open_webui.autonoma.factories`.

Requests are authenticated by an HMAC-SHA256 signature over the raw body, keyed
with ``AUTONOMA_SHARED_SECRET``; the SDK verifies it and rejects anything that
does not match, so the route serves nothing without the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

from autonoma.types import HandlerConfig
from autonoma_fastapi import create_fastapi_handler

from open_webui.autonoma.factories import FACTORIES, SEEDED_PASSWORD
from open_webui.env import (
    WEBUI_AUTH_COOKIE_SAME_SITE,
    WEBUI_AUTH_COOKIE_SECURE,
    WEBUI_SECRET_KEY,
)
from open_webui.models.config import Config
from open_webui.utils.auth import create_token
from open_webui.utils.misc import parse_duration

log = logging.getLogger(__name__)

# Open WebUI has no tenant/organization table - every user-owned row is scoped
# by `user_id`, so that is the scope field reported to the dashboard.
SCOPE_FIELD = 'user_id'


def _signing_secret() -> str:
    """The private key that signs the teardown (refs) token.

    Unlike the shared secret this one never leaves the server, so it does not
    have to be provisioned: when ``AUTONOMA_SIGNING_SECRET`` is unset it is
    derived from the instance's own ``WEBUI_SECRET_KEY``. Deriving rather than
    generating keeps it stable across restarts, so a refs token issued by
    ``up`` still verifies at ``down``.
    """
    configured = os.environ.get('AUTONOMA_SIGNING_SECRET')
    if configured:
        return configured
    return hmac.new(
        str(WEBUI_SECRET_KEY).encode('utf-8'),
        b'autonoma-refs-signing',
        hashlib.sha256,
    ).hexdigest()


async def _auth(user: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    """Hand the test runner real credentials for the seeded user.

    Returns all three shapes the runner can use:

    * a ``token`` cookie - what the SvelteKit frontend reads, so the runner
      starts already signed in;
    * an ``Authorization: Bearer`` header - accepted by every API route;
    * the email/password pair, for a test that drives the real ``/auth`` login
      screen instead of arriving pre-authenticated.

    The JWT is minted by the app's own ``create_token`` with the instance's
    configured expiry, so it is the same token a real sign-in issues.
    """
    if user is None:
        # Not every scenario seeds a User; there is nobody to sign in as.
        return {}

    expires_delta = parse_duration(await Config.get('auth.jwt_expiry'))
    token = create_token(data={'id': user['id']}, expires_delta=expires_delta)

    return {
        'cookies': [
            {
                'name': 'token',
                'value': token,
                'httpOnly': True,
                'sameSite': (WEBUI_AUTH_COOKIE_SAME_SITE or 'lax').lower(),
                'secure': bool(WEBUI_AUTH_COOKIE_SECURE),
                'path': '/',
            }
        ],
        'headers': {'Authorization': f'Bearer {token}'},
        'credentials': {
            'email': user.get('email', ''),
            'password': user.get('password', SEEDED_PASSWORD),
        },
    }


config = HandlerConfig(
    scope_field=SCOPE_FIELD,
    shared_secret=os.environ.get('AUTONOMA_SHARED_SECRET', ''),
    signing_secret=_signing_secret(),
    factories=FACTORIES,
    auth=_auth,
    sdk={'orm': 'sqlalchemy', 'server': 'fastapi'},
)

router = create_fastapi_handler(config)
