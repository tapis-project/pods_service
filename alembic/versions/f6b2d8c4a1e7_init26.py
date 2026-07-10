"""init26 — pod_access_tokens: add password_hash + session_secret

Hardening: human-chosen gate passwords are now bcrypt-hashed (password_hash) instead of
sha256, and password tokens carry an internal high-entropy session_secret that becomes the
cookie value so the per-request gate check stays on the fast sha256 path. Both columns are
nullable (link tokens leave them NULL). See models_pod_access_tokens.PodAccessToken.

Idempotent: skips a column that already exists (a fresh schema gets it from init25's
model-metadata table create; an existing schema gets it added here). env.py sets the
per-tenant search_path and calls upgrade_alltenants() once per schema.

Revision ID: f6b2d8c4a1e7
Revises: e5a1c7b3f9d2
Create Date: 2026-07-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy import inspect

revision = 'f6b2d8c4a1e7'
down_revision = 'e5a1c7b3f9d2'
branch_labels = None
depends_on = None

_TABLE = 'pod_access_tokens'
_NEW_COLUMNS = ('password_hash', 'session_secret')


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    bind = op.get_bind()
    existing = {c['name'] for c in inspect(bind).get_columns(_TABLE)}
    for col in _NEW_COLUMNS:
        if col not in existing:
            op.add_column(_TABLE, sa.Column(col, sa.VARCHAR(), nullable=True))


def downgrade_alltenants():
    bind = op.get_bind()
    existing = {c['name'] for c in inspect(bind).get_columns(_TABLE)}
    for col in reversed(_NEW_COLUMNS):
        if col in existing:
            op.drop_column(_TABLE, col)
