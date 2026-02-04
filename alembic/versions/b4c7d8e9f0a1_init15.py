"""init15 - Fix template_dependencies to query pods across all tenant schemas

Revision ID: b4c7d8e9f0a1
Revises: ecbca391a887
Create Date: 2026-01-28 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision = 'b4c7d8e9f0a1'
down_revision = 'ecbca391a887'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    """
    Drop old per-schema template_dependencies objects from init9,
    then create a single cross-schema view in siteadmintable with auto-refresh triggers.
    """
    schema = op.get_context().dialect.default_schema_name
    connection = op.get_bind()
    
    # First, drop any init9 objects from this schema
    op.execute(f'''
        DROP TRIGGER IF EXISTS refresh_template_dependencies_after_pod_change ON "{schema}".pod;
        DROP TRIGGER IF EXISTS refresh_template_dependencies_after_templatetag_change ON "{schema}".templatetag;
        DROP FUNCTION IF EXISTS "{schema}".refresh_template_dependencies_trigger();
        DROP FUNCTION IF EXISTS "{schema}".refresh_template_dependencies_view();
        DROP MATERIALIZED VIEW IF EXISTS "{schema}".template_dependencies;
    ''')
    
    # Also drop any cross-schema trigger we may have added
    op.execute(f'''
        DROP TRIGGER IF EXISTS refresh_siteadmin_template_deps_on_pod ON "{schema}".pod;
        DROP FUNCTION IF EXISTS "{schema}".refresh_siteadmin_template_deps();
    ''')
    
    # Only create the new cross-schema view when processing siteadmintable
    if schema != 'siteadmintable':
        return
    
    # Get all tenant schemas that have a pod table WITH a template column
    result = connection.execute(sa.text("""
        SELECT DISTINCT c.table_schema 
        FROM information_schema.columns c
        WHERE c.table_name = 'pod' 
        AND c.column_name = 'template'
        AND c.table_schema NOT IN ('information_schema', 'pg_catalog', 'public')
        ORDER BY c.table_schema
    """))
    tenant_schemas = [row[0] for row in result]
    
    if not tenant_schemas:
        print("No tenant schemas with pod tables found")
        return
    
    print(f"Building cross-schema template_dependencies view for schemas: {tenant_schemas}")
    
    # Build UNION query for all tenant pod tables
    union_parts = [
        f'SELECT pod_id, template FROM "{ts}".pod WHERE template IS NOT NULL'
        for ts in tenant_schemas
    ]
    all_pods_union = " UNION ALL ".join(union_parts)
    
    # Create the cross-schema materialized view
    op.execute(f'''
        CREATE MATERIALIZED VIEW "siteadmintable".template_dependencies AS
        WITH all_pods AS ({all_pods_union})
        SELECT 
            tt.template_id, 
            tt.tag_timestamp, 
            tt.template_id || ':' || tt.tag_timestamp as full_tag, 
            array_agg(p.pod_id) FILTER (WHERE p.pod_id IS NOT NULL) AS dependant_pods, 
            COUNT(p.pod_id) AS dependant_pod_count, 
            deps.dependant_tags, 
            deps.dependant_tags_count 
        FROM "siteadmintable".templatetag tt 
        LEFT JOIN LATERAL (
            SELECT 
                array_agg(DISTINCT tt2.template_id || ':' || tt2.tag_timestamp) AS dependant_tags, 
                COUNT(DISTINCT tt2.template_id || ':' || tt2.tag_timestamp) AS dependant_tags_count 
            FROM "siteadmintable".templatetag tt2 
            WHERE tt2.pod_definition->>'template' = tt.template_id || ':' || tt.tag_timestamp
        ) deps ON true 
        LEFT JOIN all_pods p ON p.template = tt.template_id || ':' || tt.tag_timestamp 
        GROUP BY tt.template_id, tt.tag_timestamp, deps.dependant_tags, deps.dependant_tags_count;

        CREATE INDEX template_dependencies_template_idx 
            ON "siteadmintable".template_dependencies (template_id, tag_timestamp);
        CREATE INDEX template_dependencies_full_tag_idx 
            ON "siteadmintable".template_dependencies (full_tag);
    ''')
    
    # Create refresh function in siteadmintable
    op.execute('''
        CREATE FUNCTION "siteadmintable".refresh_template_dependencies_view() RETURNS void AS $$
        BEGIN
            REFRESH MATERIALIZED VIEW "siteadmintable".template_dependencies;
        END;
        $$ LANGUAGE plpgsql;
    ''')
    
    # Create trigger function and trigger on siteadmintable.templatetag
    op.execute('''
        CREATE FUNCTION "siteadmintable".refresh_template_dependencies_trigger() RETURNS trigger AS $$
        BEGIN
            PERFORM "siteadmintable".refresh_template_dependencies_view();
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER refresh_template_dependencies_after_templatetag_change
            AFTER INSERT OR UPDATE OR DELETE ON "siteadmintable".templatetag
            FOR EACH STATEMENT EXECUTE FUNCTION "siteadmintable".refresh_template_dependencies_trigger();
    ''')
    
    # Create triggers on each tenant's pod table to refresh the siteadmintable view
    for ts in tenant_schemas:
        op.execute(f'''
            CREATE FUNCTION "{ts}".refresh_siteadmin_template_deps() RETURNS trigger AS $$
            BEGIN
                PERFORM "siteadmintable".refresh_template_dependencies_view();
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER refresh_siteadmin_template_deps_on_pod
                AFTER INSERT OR UPDATE OR DELETE ON "{ts}".pod
                FOR EACH STATEMENT EXECUTE FUNCTION "{ts}".refresh_siteadmin_template_deps();
        ''')
        print(f"  Created auto-refresh trigger on {ts}.pod")
    
    # Initial refresh to populate the view
    op.execute('SELECT "siteadmintable".refresh_template_dependencies_view()')
    print("Created and populated cross-schema template_dependencies view in siteadmintable")


def downgrade_alltenants():
    """Revert - drop triggers and siteadmintable objects."""
    schema = op.get_context().dialect.default_schema_name
    connection = op.get_bind()
    
    # Drop cross-schema trigger from this schema's pod table
    op.execute(f'''
        DROP TRIGGER IF EXISTS refresh_siteadmin_template_deps_on_pod ON "{schema}".pod;
        DROP FUNCTION IF EXISTS "{schema}".refresh_siteadmin_template_deps();
    ''')
    
    # Only drop the main objects when processing siteadmintable
    if schema == 'siteadmintable':
        op.execute('''
            DROP TRIGGER IF EXISTS refresh_template_dependencies_after_templatetag_change ON "siteadmintable".templatetag;
            DROP FUNCTION IF EXISTS "siteadmintable".refresh_template_dependencies_trigger();
            DROP FUNCTION IF EXISTS "siteadmintable".refresh_template_dependencies_view();
            DROP MATERIALIZED VIEW IF EXISTS "siteadmintable".template_dependencies;
        ''')
