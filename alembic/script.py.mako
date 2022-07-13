<%!
import re

%>"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()

<%
    db_names = config.get_main_option("databases")
%>

## generate an "upgrade_<xyz>() / downgrade_<xyz>()" function
## for each database name in the ini file.

def upgrade_alltenants():
    ${context.get("upgrade_alltenants", "pass")}


def downgrade_alltenants():
    ${context.get("downgrade_alltenants", "pass")}
