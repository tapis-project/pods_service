"""
Pod access tokens — shared password/token credentials for the ingress "access gate".

When a networking entry sets ``access_gate: true``, Traefik forwardAuth calls
``/pods/{pod_id}/gate``. That endpoint validates a bearer secret carried in a cookie
against the (unexpired, unrevoked) access tokens minted for the pod. This lets a pod
owner gate a site behind a shared secret WITHOUT every visitor being a Tapis user —
the complement to ``tapis_auth`` (real Tapis identities).

A token's raw secret is shown exactly once, at mint time; only its hash is stored, so
a DB read never reveals a working credential. Two "shapes" fall out of the same row:
  - password   — a human-typed value entered on the gate login form.
  - link       — a high-entropy value shared as ``https://<pod-url>/?access=<secret>``.
Both are just a ``token_hash``; the distinction is only how the visitor supplies it.

Use semantics:
  - ``max_uses`` limits how many times the secret may be *redeemed into a session*
    (each redeem sets a cookie). Once a visitor holds a valid session cookie it keeps
    working until the token expires or is revoked — gate checks do not spend a use.
  - ``expires_at`` / ``revoked`` are enforced on every gate check (live).
"""
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
import hashlib
import secrets as _secrets
from pydantic import Field, validator, create_model
import uuid

from tapisservice.logs import get_logger
from tapisservice.tapisfastapi.utils import g

from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, select, Column, String, delete, func, text
from models_base import TapisModel, TapisApiModel

logger = get_logger(__name__)


import bcrypt

# Minimum length for a human-chosen gate password. Short passwords are the only thing
# offline-crackable even with bcrypt, so we set a floor at mint time.
MIN_PASSWORD_LEN = 6


def hash_secret(pod_id: str, raw_secret: str) -> str:
    """Fast lookup hash for HIGH-ENTROPY values only — link secrets and the internal
    per-token session_secret. Salted by pod_id. This runs on every gated request
    (check_gate), so it must stay cheap; it is NEVER used for human passwords (those go
    through bcrypt at redeem time — see hash_password / _find_by_password)."""
    return hashlib.sha256(f"{pod_id}:{raw_secret}".encode("utf-8")).hexdigest()


def hash_password(raw_password: str) -> str:
    """Slow, per-hash-salted bcrypt for human-chosen passwords (protects against offline
    cracking / password reuse if the DB leaks). Only ever run at redeem time, never on
    the per-request gate path. bcrypt caps input at 72 bytes; longer is truncated."""
    pw = raw_password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("ascii")


def verify_password(raw_password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw_password.encode("utf-8")[:72], hashed.encode("ascii"))
    except Exception:
        return False


def generate_secret() -> str:
    """A URL-safe, high-entropy secret for redemption links / internal session secrets."""
    return _secrets.token_urlsafe(24)


class PodAccessTokenBase(TapisApiModel):
    pod_id: str = Field(..., description="Pod this access token unlocks.", index=True)
    token_hash: str = Field(..., description="sha256(pod_id:session_secret) — the fast lookup hash for the COOKIE value (a link's high-entropy secret, or a password token's internal session_secret). Checked on every gated request. Never a human password.", index=True)
    session_secret: Optional[str] = Field(default=None, description="For password tokens: the high-entropy value placed in the visitor's cookie after a successful bcrypt check. Not a human secret and not shown to anyone; lets check_gate stay on the fast sha256 path. None for link tokens (their shared secret IS the cookie value).")
    password_hash: Optional[str] = Field(default=None, description="For password tokens: bcrypt hash of the human-chosen password, verified only at redeem time. None for link tokens.")
    label: str = Field("", description="Human label for this credential, e.g. 'family link' or 'day-of password'.")
    kind: str = Field("link", description="'link' (high-entropy secret, shared as a redemption URL) or 'password' (human-typed on the gate form, bcrypt-hashed).")
    created_by: str = Field("", description="Tapis username that minted this token.")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="UTC mint time.")
    expires_at: Optional[datetime] = Field(default=None, description="UTC expiry; None = never expires.")
    max_uses: Optional[int] = Field(default=None, description="Max number of redemptions (cookie sessions established). None = unlimited.")
    uses: int = Field(default=0, description="Number of times this secret has been redeemed into a session.")
    revoked: bool = Field(default=False, description="True = token is dead; gate checks and redemptions are refused.")
    last_used_at: Optional[datetime] = Field(default=None, description="UTC time of the most recent successful redemption.")


class PodAccessTokenBaseRead(PodAccessTokenBase):
    id: Optional[str] = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Primary key.",
        sa_column=Column(UUID(as_uuid=False), primary_key=True, index=True, unique=True, nullable=False)
    )


class PodAccessTokenBaseFull(PodAccessTokenBaseRead):
    tenant_id: str = Field("", description="Tapis tenant.", index=True)
    site_id: str = Field("", description="Tapis site.")

    def display(self):
        """Metadata safe to return to the UI — NEVER includes a working secret."""
        d = self.dict()
        d.pop('tenant_id', None)
        d.pop('site_id', None)
        d.pop('token_hash', None)
        d.pop('session_secret', None)
        d.pop('password_hash', None)
        return d


TapisPodAccessTokenBaseFull = create_model(
    "TapisPodAccessTokenBaseFull",
    __base__=type("_ComboModel", (PodAccessTokenBaseFull, TapisModel), {})
)


class PodAccessToken(TapisPodAccessTokenBaseFull, table=True, validate=True):
    __tablename__ = "pod_access_tokens"

    @validator('tenant_id')
    def set_tenant_id(cls, v):
        if v:
            return v
        return g.request_tenant_id

    @validator('site_id')
    def set_site_id(cls, v):
        if v:
            return v
        return g.site_id

    # ── minting ──────────────────────────────────────────────────────────────
    @classmethod
    def mint(cls, pod_id: str, tenant: str, site: str,
             label: str = "", kind: str = "link", created_by: str = "",
             raw_secret: Optional[str] = None,
             expires_at: Optional[datetime] = None,
             max_uses: Optional[int] = None) -> Tuple['PodAccessToken', str]:
        """Create a token row and return (row, shared_secret). The shared secret is
        returned ONCE and is what the owner gives out:
          - kind='link':     a generated high-entropy secret; token_hash = sha256 of it,
                             and it is also the cookie value on redemption.
          - kind='password': the human-chosen `raw_secret`, bcrypt-hashed into
                             password_hash. A separate high-entropy session_secret becomes
                             the cookie value (token_hash = sha256 of THAT), so the typed
                             password never touches the per-request path or the cookie."""
        if kind == "password":
            if not raw_secret:
                raise ValueError("kind='password' requires a password value.")
            session = generate_secret()
            row = cls(
                pod_id=pod_id,
                token_hash=hash_secret(pod_id, session),
                session_secret=session,
                password_hash=hash_password(raw_secret),
                label=label, kind="password", created_by=created_by,
                expires_at=expires_at, max_uses=max_uses,
                tenant_id=tenant, site_id=site,
            )
            row.db_create(tenant=tenant, site=site)
            return row, raw_secret
        # link (default)
        raw = raw_secret if raw_secret else generate_secret()
        row = cls(
            pod_id=pod_id,
            token_hash=hash_secret(pod_id, raw),
            label=label, kind="link", created_by=created_by,
            expires_at=expires_at, max_uses=max_uses,
            tenant_id=tenant, site_id=site,
        )
        row.db_create(tenant=tenant, site=site)
        return row, raw

    # ── lookup / validation ──────────────────────────────────────────────────
    @classmethod
    def _find_by_secret(cls, pod_id: str, raw_secret: str, tenant: str, site: str) -> Optional['PodAccessToken']:
        """Return the token row matching this secret for the pod, or None. Does not
        enforce expiry/revocation/use limits — callers decide which rules apply."""
        if not raw_secret:
            return None
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = (
            select(cls)
            .where(
                cls.pod_id == pod_id,
                cls.token_hash == hash_secret(pod_id, raw_secret),
                cls.tenant_id == tenant,
                cls.site_id == site,
            )
            .limit(1)
        )
        return store.run("execute", stmt, scalars=True, first=True)

    @classmethod
    def _find_by_password(cls, pod_id: str, typed_password: str, tenant: str, site: str) -> Optional['PodAccessToken']:
        """Find a password token whose bcrypt hash matches the typed value. Iterates the
        pod's password tokens (there are only a handful) and bcrypt-verifies each — this
        is the ONLY place bcrypt runs, and only at redeem time. Includes revoked/expired
        tokens so redeem can report the accurate reason rather than 'not_found'."""
        if not typed_password:
            return None
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = select(cls).where(
            cls.pod_id == pod_id,
            cls.tenant_id == tenant,
            cls.site_id == site,
            cls.password_hash != None,  # noqa: E711
        )
        for row in (store.run("execute", stmt, scalars=True, all=True) or []):
            if verify_password(typed_password, row.password_hash):
                return row
        return None

    @classmethod
    def check_gate(cls, pod_id: str, cookie_value: str, tenant: str, site: str) -> Optional['PodAccessToken']:
        """Session cookie validation on every gated request (fast path): the cookie holds
        a high-entropy value (a link secret or a password token's session_secret), matched
        by sha256 lookup. Token must exist, be unrevoked and unexpired. Never runs bcrypt
        and does NOT spend a use."""
        tok = cls._find_by_secret(pod_id, cookie_value, tenant, site)
        if not tok or tok.revoked:
            return None
        if tok.expires_at and datetime.utcnow() >= tok.expires_at:
            return None
        return tok

    @classmethod
    def redeem(cls, pod_id: str, presented: str, tenant: str, site: str
               ) -> Tuple[Optional['PodAccessToken'], str, Optional[str]]:
        """Redeem a presented secret (link secret or typed password) into a session.
        Returns ``(token, reason, cookie_value)`` — cookie_value is what the caller should
        set as the session cookie (a link's own secret, or a password token's
        session_secret). Reasons: ok / empty / not_found / revoked / expired / exhausted.
        On success spends one use and stamps last_used_at."""
        if not presented:
            return None, "empty", None
        # Fast path: link secrets (and the internal session_secret) match token_hash.
        tok = cls._find_by_secret(pod_id, presented, tenant, site)
        cookie_value = presented
        if not tok:
            # Slow path: a typed password, bcrypt-verified against the pod's password tokens.
            tok = cls._find_by_password(pod_id, presented, tenant, site)
            cookie_value = tok.session_secret if tok else None
        if not tok:
            return None, "not_found", None
        if tok.revoked:
            return None, "revoked", None
        if tok.expires_at and datetime.utcnow() >= tok.expires_at:
            return None, "expired", None
        if tok.max_uses is not None and tok.uses >= tok.max_uses:
            return None, "exhausted", None
        tok.uses += 1
        tok.last_used_at = datetime.utcnow()
        tok.db_update(tenant=tenant, site=site)
        return tok, "ok", cookie_value

    # ── management ────────────────────────────────────────────────────────────
    @classmethod
    def list_for_pod(cls, pod_id: str, tenant: str, site: str) -> List['PodAccessToken']:
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = (
            select(cls)
            .where(cls.pod_id == pod_id, cls.tenant_id == tenant, cls.site_id == site)
            .order_by(cls.created_at.desc())
        )
        return store.run("execute", stmt, scalars=True, all=True)

    @classmethod
    def get(cls, token_id: str, pod_id: str, tenant: str, site: str) -> Optional['PodAccessToken']:
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = (
            select(cls)
            .where(
                cls.id == token_id,
                cls.pod_id == pod_id,
                cls.tenant_id == tenant,
                cls.site_id == site,
            )
            .limit(1)
        )
        return store.run("execute", stmt, scalars=True, first=True)

    @classmethod
    def has_active_tokens(cls, pod_id: str, tenant: str, site: str) -> bool:
        """True if the pod has at least one unrevoked, unexpired token (used to warn
        when a gate is enabled but nothing can open it)."""
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        stmt = (
            select(func.count(cls.id))
            .where(
                cls.pod_id == pod_id,
                cls.tenant_id == tenant,
                cls.site_id == site,
                cls.revoked == False,  # noqa: E712
            )
        )
        return (store.run("scalar", stmt) or 0) > 0


class AccessTokenMintRequest(TapisApiModel):
    label: str = Field("", description="Human label for this credential.")
    kind: str = Field("link", description="'link' (generated high-entropy secret, shared as ?access= URL) or 'password' (you supply a human-typed value below).")
    password: Optional[str] = Field(default=None, description="For kind='password': the value visitors will type. Leave null for kind='link' to auto-generate a strong secret.")
    expires_in_seconds: Optional[int] = Field(default=None, description="Lifetime in seconds from now; null = never expires. e.g. 86400 for a day.")
    max_uses: Optional[int] = Field(default=None, description="Max redemptions into a session; null = unlimited.")

    @validator('kind')
    def check_kind(cls, v):
        if v not in ("link", "password"):
            raise ValueError("kind must be 'link' or 'password'.")
        return v

    @validator('password')
    def check_password_kind(cls, v, values):
        # kind='link' must always get the auto-generated high-entropy secret —
        # a caller-supplied value here would silently weaken the link credential
        if v is not None and values.get('kind') == 'link':
            raise ValueError("password is only accepted for kind='password'; kind='link' secrets are auto-generated.")
        return v

    @validator('max_uses')
    def check_max_uses(cls, v):
        if v is not None and v < 1:
            raise ValueError("max_uses must be >= 1 when set.")
        return v

    @validator('expires_in_seconds')
    def check_expires(cls, v):
        if v is not None and v < 1:
            raise ValueError("expires_in_seconds must be >= 1 when set.")
        return v

    @validator('password')
    def check_password_strength(cls, v):
        # Short passwords are the one thing still offline-crackable even with bcrypt.
        if v is not None and len(v) < MIN_PASSWORD_LEN:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters.")
        return v


class PodAccessTokenMetaResponse(TapisApiModel):
    id: str
    pod_id: str
    label: str
    kind: str
    created_by: str
    created_at: datetime
    expires_at: Optional[datetime]
    max_uses: Optional[int]
    uses: int
    revoked: bool
    last_used_at: Optional[datetime]


class PodAccessTokensResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[PodAccessTokenMetaResponse]
    status: str
    version: str


class PodAccessTokenMintResult(PodAccessTokenMetaResponse):
    secret: str = Field(..., description="The raw secret — shown ONCE at mint time.")
    redemption_url: str = Field("", description="Shareable link that redeems this secret and sets the session cookie.")


class PodAccessTokenMintResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: PodAccessTokenMintResult
    status: str
    version: str
