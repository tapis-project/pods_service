"""init9

Revision ID: d3006d3ab8d5
Revises: 6333debf3f60
Create Date: 2025-03-14 05:01:52.356785

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy


# revision identifiers, used by Alembic.
revision = 'd3006d3ab8d5'
down_revision = '6333debf3f60'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()




def upgrade_alltenants():
    # Get current schema name
    schema = f'"{op.get_context().dialect.default_schema_name}"'
    
    # Create the materialized view and supporting objects
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {schema}.refresh_template_dependencies_view() RETURNS void AS $$
        BEGIN
            REFRESH MATERIALIZED VIEW {schema}.template_dependencies;
        END;
        $$ LANGUAGE plpgsql;

        CREATE MATERIALIZED VIEW IF NOT EXISTS {schema}.template_dependencies AS
        SELECT 
            tt.template_id, 
            tt.tag_timestamp, 
            tt.template_id || ':' || tt.tag_timestamp as full_tag, 
            array_agg(p.pod_id) FILTER (WHERE p.pod_id IS NOT NULL) AS dependant_pods, 
            COUNT(p.pod_id) AS dependant_pod_count, 
            deps.dependant_tags, 
            deps.dependant_tags_count 
        FROM {schema}.templatetag tt 
        LEFT JOIN LATERAL (
            SELECT 
                array_agg(DISTINCT tt2.template_id || ':' || tt2.tag_timestamp) AS dependant_tags, 
                COUNT(DISTINCT tt2.template_id || ':' || tt2.tag_timestamp) AS dependant_tags_count 
            FROM {schema}.templatetag tt2 
            WHERE tt2.pod_definition->>'template' = tt.template_id || ':' || tt.tag_timestamp
        ) deps ON true 
        LEFT JOIN {schema}.pod p ON p.template = tt.template_id || ':' || tt.tag_timestamp 
        GROUP BY tt.template_id, tt.tag_timestamp, deps.dependant_tags, deps.dependant_tags_count;

        CREATE INDEX IF NOT EXISTS template_dependencies_template_idx 
            ON {schema}.template_dependencies (template_id, tag_timestamp);
        CREATE INDEX IF NOT EXISTS template_dependencies_full_tag_idx 
            ON {schema}.template_dependencies (full_tag);

        CREATE OR REPLACE FUNCTION {schema}.refresh_template_dependencies_trigger() RETURNS trigger AS $$
        BEGIN
            PERFORM {schema}.refresh_template_dependencies_view();
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS refresh_template_dependencies_after_pod_change ON {schema}.pod;
        CREATE TRIGGER refresh_template_dependencies_after_pod_change
            AFTER INSERT OR UPDATE OR DELETE ON {schema}.pod
            FOR EACH STATEMENT EXECUTE FUNCTION {schema}.refresh_template_dependencies_trigger();

        DROP TRIGGER IF EXISTS refresh_template_dependencies_after_templatetag_change ON {schema}.templatetag;
        CREATE TRIGGER refresh_template_dependencies_after_templatetag_change
            AFTER INSERT OR UPDATE OR DELETE ON {schema}.templatetag
            FOR EACH STATEMENT EXECUTE FUNCTION {schema}.refresh_template_dependencies_trigger();
    """)

    # Initial refresh of the materialized view
    op.execute(f"SELECT {schema}.refresh_template_dependencies_view()")


def downgrade_alltenants():
    schema = f'"{op.get_context().dialect.default_schema_name}"'
    
    # Remove triggers first
    op.execute(f"""
        DROP TRIGGER IF EXISTS refresh_template_dependencies_after_pod_change ON {schema}.pod;
        DROP TRIGGER IF EXISTS refresh_template_dependencies_after_templatetag_change ON {schema}.templatetag;
        DROP FUNCTION IF EXISTS {schema}.refresh_template_dependencies_trigger();
        DROP FUNCTION IF EXISTS {schema}.refresh_template_dependencies_view();
        DROP MATERIALIZED VIEW IF EXISTS {schema}.template_dependencies;
    """)
