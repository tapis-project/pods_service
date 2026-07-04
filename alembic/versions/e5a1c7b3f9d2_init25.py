"""init25 — create pod_access_tokens

New table backing the ingress "access gate": shared password/token credentials that
let a pod be gated behind a secret without every visitor being a Tapis user
(complements networking.tapis_auth). See models_pod_access_tokens.PodAccessToken.

Idempotent: the table is created from the current model metadata with checkfirst=True,
so this is a no-op on any schema where it already exists. env.py sets the per-tenant
search_path and calls upgrade_alltenants() once per schema.

Revision ID: e5a1c7b3f9d2
Revises: b7d4e1f9a2c3
Create Date: 2026-07-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy

from models_pod_access_tokens import PodAccessToken

# revision identifiers, used by Alembic.
revision = 'e5a1c7b3f9d2'
down_revision = 'b7d4e1f9a2c3'
branch_labels = None
depends_on = None

_TABLES = (PodAccessToken,)


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    bind = op.get_bind()
    for model in _TABLES:
        # checkfirst=True -> idempotent CREATE TABLE/INDEX IF NOT EXISTS in the
        # current tenant schema (search_path is set per-tenant by env.py).
        model.__table__.create(bind=bind, checkfirst=True)


def downgrade_alltenants():
    bind = op.get_bind()
    for model in reversed(_TABLES):
        model.__table__.drop(bind=bind, checkfirst=True)
