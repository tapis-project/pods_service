"""init13

Revision ID: f8a9b3c2d1e0
Revises: 5162ee088365
Create Date: 2025-12-18 19:37:51.723310

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy


# revision identifiers, used by Alembic.
revision = 'f8a9b3c2d1e0'
down_revision = '5162ee088365'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()




def upgrade_alltenants(): 
    op.add_column('pod', sa.Column('template_overrides', sa.JSON(), nullable=True))

    connection = op.get_bind()
    schema_result = connection.execute(sa.text("SELECT current_schema()"))
    schema = schema_result.scalar()
    
    # POD TABLE: Convert old dict format to new mount_path-keyed format
    # Old: {"vol1": {"type": "tapisvolume", "mount_path": "/mnt", "sub_path": ""}}
    # New: {"/mnt": {"type": "tapisvolume", "source_id": "vol1", "sub_path": ""}}
    connection.execute(sa.text(f"""
        UPDATE "{schema}".pod
        SET volume_mounts = (
            SELECT COALESCE(
                jsonb_object_agg(
                    COALESCE(value->>'mount_path', '/tapis_volume_mount_' || key),
                    jsonb_build_object(
                        'type', COALESCE(value->>'type', 'tapisvolume'),
                        'source_id', key,
                        'sub_path', COALESCE(value->>'sub_path', ''),
                        'read_only', CASE 
                            WHEN COALESCE(value->>'type', 'tapisvolume') = 'tapissnapshot' THEN true
                            ELSE COALESCE((value->>'read_only')::boolean, false)
                        END
                    )
                ),
                '{{}}'::jsonb
            )
            FROM jsonb_each(volume_mounts::jsonb)
        )
        WHERE volume_mounts IS NOT NULL 
          AND volume_mounts::text != '{{}}'
          AND jsonb_typeof(volume_mounts::jsonb) = 'object';
    """))
    
    # TEMPLATETAG TABLE: Convert old dict format in pod_definition.volume_mounts
    connection.execute(sa.text(f"""
        UPDATE "{schema}".templatetag
        SET pod_definition = jsonb_set(
            pod_definition::jsonb,
            '{{volume_mounts}}',
            (
                SELECT COALESCE(
                    jsonb_object_agg(
                        COALESCE(value->>'mount_path', '/tapis_volume_mount_' || key),
                        jsonb_build_object(
                            'type', COALESCE(value->>'type', 'tapisvolume'),
                            'source_id', key,
                            'sub_path', COALESCE(value->>'sub_path', ''),
                            'read_only', CASE 
                                WHEN COALESCE(value->>'type', 'tapisvolume') = 'tapissnapshot' THEN true
                                ELSE COALESCE((value->>'read_only')::boolean, false)
                            END
                        )
                    ),
                    '{{}}'::jsonb
                )
                FROM jsonb_each((pod_definition::jsonb->'volume_mounts'))
            )::jsonb
        )
        WHERE pod_definition IS NOT NULL
          AND pod_definition::jsonb ? 'volume_mounts'
          AND jsonb_typeof((pod_definition::jsonb->'volume_mounts')) = 'object'
          AND (pod_definition::jsonb->>'volume_mounts') != '{{}}';
    """))


def downgrade_alltenants():
    op.drop_column('pod', 'template_overrides')

    """
    Revert: new mount_path-keyed format back to old source_id-keyed format.
    Note: Ephemeral mounts (no source_id) will be dropped.
    """
    connection = op.get_bind()
    schema_result = connection.execute(sa.text("SELECT current_schema()"))
    schema = schema_result.scalar()
    
    # POD TABLE: Revert to old format
    connection.execute(sa.text(f"""
        UPDATE "{schema}".pod
        SET volume_mounts = (
            SELECT COALESCE(
                jsonb_object_agg(
                    value->>'source_id',
                    jsonb_build_object(
                        'type', value->>'type',
                        'mount_path', key,
                        'sub_path', COALESCE(value->>'sub_path', '')
                    )
                ),
                '{{}}'::jsonb
            )
            FROM jsonb_each(volume_mounts::jsonb)
            WHERE value->>'source_id' IS NOT NULL
        )
        WHERE volume_mounts IS NOT NULL 
          AND volume_mounts::text != '{{}}'
          AND jsonb_typeof(volume_mounts::jsonb) = 'object';
    """))
    
    # TEMPLATETAG TABLE: Revert to old format
    connection.execute(sa.text(f"""
        UPDATE "{schema}".templatetag
        SET pod_definition = jsonb_set(
            pod_definition::jsonb,
            '{{volume_mounts}}',
            (
                SELECT COALESCE(
                    jsonb_object_agg(
                        value->>'source_id',
                        jsonb_build_object(
                            'type', value->>'type',
                            'mount_path', key,
                            'sub_path', COALESCE(value->>'sub_path', '')
                        )
                    ),
                    '{{}}'::jsonb
                )
                FROM jsonb_each((pod_definition::jsonb->'volume_mounts'))
                WHERE value->>'source_id' IS NOT NULL
            )
        )
        WHERE pod_definition IS NOT NULL
          AND pod_definition::jsonb ? 'volume_mounts'
          AND jsonb_typeof((pod_definition::jsonb->'volume_mounts')) = 'object'
          AND (pod_definition::jsonb->>'volume_mounts') != '{{}}';
    """))

