"""RD10: série diária URA e arquivos de origem imutáveis."""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = '20261017_rd10'
down_revision = '20261017_rd09'
branch_labels = None
depends_on = None


def upgrade():
    existing = set() if context.is_offline_mode() else set(sa.inspect(op.get_bind()).get_table_names())
    if 'ura_import_runs' not in existing:
        op.create_table('ura_import_runs',
            sa.Column('id',sa.Integer,primary_key=True),
            sa.Column('project',sa.String(80),nullable=False),
            sa.Column('source_name',sa.String(255),nullable=False),
            sa.Column('sha256',sa.String(64),nullable=False),
            sa.Column('raw_csv',LONGTEXT,nullable=False),
            sa.Column('imported_at',sa.DateTime,nullable=False),
            sa.Column('username',sa.String(150),nullable=False),
            sa.Column('record_count',sa.Integer,nullable=False),
            sa.Column('inserted_count',sa.Integer,nullable=False),
            sa.Column('unchanged_count',sa.Integer,nullable=False))
        op.create_index('ix_ura_import_runs_project','ura_import_runs',['project'])
        op.create_index('ix_ura_import_runs_sha256','ura_import_runs',['sha256'])
    if 'ura_daily' not in existing:
        op.create_table('ura_daily',
            sa.Column('id',sa.Integer,primary_key=True),
            sa.Column('project',sa.String(80),nullable=False),
            sa.Column('service_id',sa.Integer,nullable=False),
            sa.Column('service_name',sa.String(255),nullable=False),
            sa.Column('day',sa.Date,nullable=False),
            *(sa.Column(name,sa.Integer,nullable=False) for name in ('incoming','answered','abandoned','blocked','answered_30','abandoned_30','answered_after_30','abandoned_after_30')),
            sa.Column('import_id',sa.Integer,sa.ForeignKey('ura_import_runs.id'),nullable=False),
            sa.UniqueConstraint('project','service_id','day',name='uq_ura_project_service_day'))
        for name in ('project','day','import_id'):
            op.create_index('ix_ura_daily_'+name,'ura_daily',[name])


def downgrade():
    op.drop_table('ura_daily')
    op.drop_table('ura_import_runs')
