"""node routes (publish v0) — route table: publish a node port at <route_id>.pods.<domain>
through central traefik, with the full per-route tapis_auth config (same field set as
pod networking; allowed-user groups resolve against the route's own permissions).

Revision ID: a7c3d9e2f1b4
Revises: f0b2c8d1e9a3
Create Date: 2026-07-30

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'a7c3d9e2f1b4'
down_revision = 'f0b2c8d1e9a3'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    # nullability mirrors the model metadata exactly (plain str/bool/int fields with
    # defaults are NOT NULL; sa_column JSON/ARRAY + Optional datetimes are nullable),
    # so `make check` stays drift-free.
    op.create_table(
        'route',
        sa.Column('route_id',                       sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('node_id',                        sa.String(),                        nullable=False),
        sa.Column('port',                           sa.Integer(),                       nullable=False),
        sa.Column('backend_host',                   sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('description',                    sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('tapis_auth',                     sa.Boolean(),                       nullable=False),
        sa.Column('tapis_auth_response_headers',    sa.JSON(),                          nullable=True),
        sa.Column('tapis_auth_allowed_users',       postgresql.ARRAY(sa.String()),      nullable=True),
        sa.Column('tapis_auth_return_path',         sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('tapis_auth_excluded_paths',      postgresql.ARRAY(sa.String()),      nullable=True),
        sa.Column('tapis_auth_excluded_path_regex', postgresql.ARRAY(sa.String()),      nullable=True),
        sa.Column('url',                            sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('creation_ts',                    sa.DateTime(),                      nullable=True),
        sa.Column('update_ts',                      sa.DateTime(),                      nullable=True),
        sa.Column('tenant_id',                      sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('site_id',                        sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('permissions',                    postgresql.ARRAY(sa.String()),      nullable=True),
        sa.Column('action_logs',                    postgresql.ARRAY(sa.String()),      nullable=True),
        sa.PrimaryKeyConstraint('route_id'),
    )
    op.create_index('ix_route_node_id', 'route', ['node_id'])


def downgrade_alltenants():
    op.drop_index('ix_route_node_id', table_name='route')
    op.drop_table('route')
