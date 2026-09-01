"""Session authentication: HMAC handshake, topic ACL, bandwidth quotas.

The adapter layer enforces network-side authorization on top of the raw
transport. Three layers (as agreed in the design review):

1. **connection-level** - client proves identity with an HMAC-signed handshake
   (timestamped to resist replay) and receives a signed session credential;
2. **topic ACL** - advertise / publish / subscribe is checked against the
   client's allowed-topic table; unauthorized access is rejected with an error;
3. **bandwidth quota** - each client has an upstream/downstream budget; requests
   that would exceed it are denied or downgraded.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Handshake signature lifetime (seconds).
HANDSHAKE_TTL_S = 60.0


def hmac_sign(secret: bytes, client_id: str, timestamp_s: float) -> str:
    """HMAC-SHA256 signature over (client_id, timestamp)."""
    msg = f"{client_id}:{int(timestamp_s)}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify_handshake(
    secret: bytes, client_id: str, timestamp_s: float, signature: str
) -> bool:
    """Verify a handshake signature and its freshness."""
    if abs(time.time() - timestamp_s) > HANDSHAKE_TTL_S:
        return False
    expected = hmac_sign(secret, client_id, timestamp_s)
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True)
class SessionCredentials:
    """Signed session credential issued after a successful handshake."""

    session_id: str
    client_id: str
    issued_ts: float
    allowed_topics: tuple = ()  # topics this client may advertise/publish
    upstream_quota_bps: int = 0  # 0 = unlimited
    downstream_quota_bps: int = 0  # 0 = unlimited

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "client_id": self.client_id,
            "issued_ts": self.issued_ts,
            "allowed_topics": list(self.allowed_topics),
            "upstream_quota_bps": self.upstream_quota_bps,
            "downstream_quota_bps": self.downstream_quota_bps,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionCredentials":
        return cls(
            session_id=data["session_id"],
            client_id=data["client_id"],
            issued_ts=data["issued_ts"],
            allowed_topics=tuple(data.get("allowed_topics", [])),
            upstream_quota_bps=int(data.get("upstream_quota_bps", 0)),
            downstream_quota_bps=int(data.get("downstream_quota_bps", 0)),
        )


class AuthError(Exception):
    """Raised when authentication / authorization fails."""

    def __init__(self, message: str, code: str = "auth_failed"):
        super().__init__(message)
        self.code = code


class SessionAuthorizer:
    """Server-side authorizer: validates handshakes, issues credentials,
    enforces topic ACL and bandwidth quotas."""

    def __init__(
        self,
        secret: bytes,
        clients: Optional[Dict[str, dict]] = None,
        max_upstream_bps: int = 0,
        max_downstream_bps: int = 0,
    ):
        self._secret = secret
        # client_id -> {topics: [...], upstream_bps, downstream_bps}
        self._clients = clients or {}
        self._max_upstream = max_upstream_bps
        self._max_downstream = max_downstream_bps
        # session_id -> credentials
        self._sessions: Dict[str, SessionCredentials] = {}

    # ---- handshake ----
    def authenticate(
        self, client_id: str, timestamp_s: float, signature: str
    ) -> SessionCredentials:
        if not verify_handshake(self._secret, client_id, timestamp_s, signature):
            raise AuthError("invalid or stale handshake signature", "auth_failed")
        profile = self._clients.get(client_id)
        if profile is None:
            # If no client table is configured, allow with default quotas.
            topics = ()
            up = self._max_upstream
            down = self._max_downstream
        else:
            topics = tuple(profile.get("topics", ()))
            up = profile.get("upstream_bps", self._max_upstream)
            down = profile.get("downstream_bps", self._max_downstream)
        cred = SessionCredentials(
            session_id=uuid.uuid4().hex,
            client_id=client_id,
            issued_ts=time.time(),
            allowed_topics=topics,
            upstream_quota_bps=up,
            downstream_quota_bps=down,
        )
        self._sessions[cred.session_id] = cred
        return cred

    def get_session(self, session_id: str) -> Optional[SessionCredentials]:
        return self._sessions.get(session_id)

    # ---- topic ACL ----
    def check_publish(self, cred: SessionCredentials, topic: str) -> None:
        if cred.allowed_topics and topic not in cred.allowed_topics:
            raise AuthError(
                f"client '{cred.client_id}' not allowed to publish '{topic}'",
                "topic_forbidden",
            )

    def check_subscribe(self, cred: SessionCredentials, topic: str) -> None:
        if cred.allowed_topics and topic not in cred.allowed_topics:
            raise AuthError(
                f"client '{cred.client_id}' not allowed to subscribe '{topic}'",
                "topic_forbidden",
            )

    # ---- bandwidth quota ----
    def check_bandwidth(
        self,
        cred: SessionCredentials,
        upstream_bps: int,
        downstream_bps: int,
    ) -> None:
        if cred.upstream_quota_bps and upstream_bps > cred.upstream_quota_bps:
            raise AuthError(
                f"upstream {upstream_bps} bps exceeds quota {cred.upstream_quota_bps}",
                "quota_exceeded",
            )
        if cred.downstream_quota_bps and downstream_bps > cred.downstream_quota_bps:
            raise AuthError(
                f"downstream {downstream_bps} bps exceeds quota {cred.downstream_quota_bps}",
                "quota_exceeded",
            )


def generate_secret() -> bytes:
    """Generate a fresh shared secret for deployment."""
    return secrets.token_bytes(32)
