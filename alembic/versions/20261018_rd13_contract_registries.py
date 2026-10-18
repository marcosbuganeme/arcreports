"""RD13: revisão da URA, série da API, feriados, cadastros do contrato e mapas de fatura."""
from alembic import context, op
import sqlalchemy as sa

revision = '20261018_rd13'
down_revision = '20261017_rd10'
branch_labels = None
depends_on = None

USER = lambda name='username': sa.Column(name, sa.String(150), nullable=False)


def tables():
    return {
        'ura_daily_revisions': (
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('import_id', sa.Integer, sa.ForeignKey('ura_import_runs.id'), nullable=False, index=True),
            sa.Column('previous_import_id', sa.Integer, nullable=False),
            sa.Column('project', sa.String(80), nullable=False),
            sa.Column('service_id', sa.Integer, nullable=False),
            sa.Column('day', sa.Date, nullable=False),
            sa.Column('before', sa.Text, nullable=False),
            sa.Column('after', sa.Text, nullable=False)),
        'ura_api_daily': (
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('project', sa.String(80), nullable=False, index=True),
            sa.Column('service_name', sa.String(255), nullable=False),
            sa.Column('day', sa.Date, nullable=False, index=True),
            sa.Column('answered', sa.Integer, nullable=False),
            sa.Column('answered_30', sa.Integer, nullable=False),
            sa.Column('fetched_at', sa.DateTime, nullable=False),
            USER(),
            sa.UniqueConstraint('project', 'service_name', 'day', name='uq_ura_api_project_service_day')),
        'contract_holidays': (
            sa.Column('day', sa.Date, primary_key=True),
            sa.Column('name', sa.String(160), nullable=False),
            sa.Column('kind', sa.String(40), nullable=False),
            sa.Column('source', sa.String(160), nullable=False),
            USER('updated_by')),
        'contract_absences': (
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('day', sa.Date, nullable=False, index=True),
            sa.Column('team', sa.Integer, nullable=False),
            sa.Column('profile', sa.String(80), nullable=False),
            sa.Column('person_ref', sa.String(80), nullable=False),
            sa.Column('covered', sa.Boolean, nullable=False),
            sa.Column('note', sa.String(255)),
            USER(),
            sa.UniqueConstraint('day', 'team', 'profile', 'person_ref', name='uq_contract_absence')),
        'contract_staff': (
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('person_ref', sa.String(80), nullable=False, index=True),
            sa.Column('team', sa.Integer, nullable=False),
            sa.Column('profile', sa.String(80), nullable=False),
            sa.Column('start_date', sa.Date, nullable=False),
            sa.Column('end_date', sa.Date),
            sa.Column('departure', sa.String(40)),
            USER(),
            sa.UniqueConstraint('person_ref', 'team', 'start_date', name='uq_contract_staff')),
        'contract_availability': (
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('competence', sa.String(7), nullable=False, index=True),
            sa.Column('indicator', sa.String(10), nullable=False),
            sa.Column('asset', sa.String(160), nullable=False),
            *(sa.Column(name, sa.String(40), nullable=False) for name in ('total_hours', 'unavailable_hours', 'planned_hours', 'third_party_hours')),
            USER(),
            sa.UniqueConstraint('competence', 'indicator', 'asset', name='uq_contract_availability')),
        'contract_project_scores': (
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('competence', sa.String(7), nullable=False, index=True),
            sa.Column('project', sa.String(160), nullable=False),
            sa.Column('invoice', sa.String(10), nullable=False),
            *(sa.Column(name, sa.Integer, nullable=False) for name in ('deadline_score', 'quality_score', 'communication_score')),
            USER(),
            sa.UniqueConstraint('competence', 'project', name='uq_contract_project_score')),
        'contract_registry_status': (
            sa.Column('id', sa.Integer, primary_key=True),
            sa.Column('competence', sa.String(7), nullable=False),
            sa.Column('registry', sa.String(40), nullable=False),
            sa.Column('confirmed_by', sa.String(150), nullable=False),
            sa.Column('confirmed_at', sa.DateTime, nullable=False),
            sa.UniqueConstraint('competence', 'registry', name='uq_contract_registry_status')),
        'contract_invoice_map': (
            sa.Column('indicator', sa.String(10), primary_key=True),
            sa.Column('invoice', sa.String(20), nullable=False),
            sa.Column('confirmed', sa.Boolean, nullable=False),
            sa.Column('source', sa.String(255), nullable=False),
            USER('updated_by')),
        'contract_entity_map': (
            sa.Column('entity_id', sa.Integer, primary_key=True, autoincrement=False),
            sa.Column('invoice', sa.String(20), nullable=False),
            sa.Column('confirmed', sa.Boolean, nullable=False),
            sa.Column('source', sa.String(255), nullable=False),
            USER('updated_by')),
    }


def upgrade():
    offline = context.is_offline_mode()
    inspector = None if offline else sa.inspect(op.get_bind())
    existing = set() if offline else set(inspector.get_table_names())
    columns = set() if offline else {c['name'] for c in inspector.get_columns('ura_import_runs')}
    if 'replaced_count' not in columns:
        op.add_column('ura_import_runs', sa.Column('replaced_count', sa.Integer, nullable=False, server_default='0'))
    if 'reason' not in columns:
        op.add_column('ura_import_runs', sa.Column('reason', sa.Text))
    for name, definition in tables().items():
        if name not in existing:
            op.create_table(name, *definition)


def downgrade():
    for name in reversed(list(tables())):
        op.drop_table(name)
    op.drop_column('ura_import_runs', 'reason')
    op.drop_column('ura_import_runs', 'replaced_count')
