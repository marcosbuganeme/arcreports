"""RD09: operações e direção dos medidores; compatível com atualização no startup."""
from alembic import context, op
import sqlalchemy as sa

revision = '20261017_rd09'
down_revision = '74d249fd4a23'
branch_labels = None
depends_on = None


def upgrade():
    existing = set() if context.is_offline_mode() else {c['name'] for c in sa.inspect(op.get_bind()).get_columns('dashboard_widgets')}
    for column in (
        sa.Column('gauge_mode', sa.String(20), nullable=False, server_default='legacy'),
        sa.Column('goal_direction', sa.String(20), nullable=False, server_default='minimum'),
        sa.Column('denominator_field', sa.String(120)),
    ):
        if column.name not in existing:
            op.add_column('dashboard_widgets', column)


def downgrade():
    for name in ('denominator_field', 'goal_direction', 'gauge_mode'):
        op.drop_column('dashboard_widgets', name)
