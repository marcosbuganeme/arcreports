"""RD10: série diária URA e arquivos de origem imutáveis."""
from alembic import context, op
from sqlalchemy import inspect
from app.models import UraDaily, UraImportRun

revision = '20261017_rd10'
down_revision = '20261017_rd09'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    existing = set() if context.is_offline_mode() else set(inspect(bind).get_table_names())
    for table in (UraImportRun.__table__, UraDaily.__table__):
        if table.name not in existing:
            table.create(bind, checkfirst=False)


def downgrade():
    UraDaily.__table__.drop(op.get_bind())
    UraImportRun.__table__.drop(op.get_bind())
