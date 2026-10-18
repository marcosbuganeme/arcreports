"""Apuração da série importada, sem médias de percentuais ou acesso à origem externa."""
from datetime import timedelta

from sqlalchemy import func, select

from app.billing_rules import adjusted_goal, pao_factor
from app.metrics import measure
from app.models import UraDaily, UraImportRun
from app.ura import normalize_project


def ura_report(db, period, project, service_id=None):
    project = normalize_project(project)
    filters = [UraDaily.project == project, UraDaily.day >= period.start, UraDaily.day <= period.end]
    if service_id is not None:
        filters.append(UraDaily.service_id == service_id)
    incoming, answered, answered_30, count, first, cut = db.execute(select(
        func.sum(UraDaily.incoming),func.sum(UraDaily.answered),func.sum(UraDaily.answered_30),
        func.count(),func.min(UraDaily.day),func.max(UraDaily.day)).where(*filters)).one()
    days = db.execute(select(UraDaily.service_id,UraDaily.service_name,UraDaily.day,UraDaily.import_id).where(*filters)).all()
    services = {}
    for sid,name,day,run_id in days:
        services.setdefault(sid, {'id':sid,'name':name,'days':set()})['days'].add(day)
    expected = (period.end - period.start).days + 1
    coverage = [{'id':sid,'name':item['name'],'registered_days':len(item['days']),
                 'missing_days':expected-len(item['days'])} for sid,item in sorted(services.items())]
    run_ids = sorted({row.import_id for row in days})
    sources = db.scalars(select(UraImportRun).where(UraImportRun.id.in_(run_ids)).order_by(UraImportRun.id)).all() if run_ids else []
    metrics = []
    for code,numerator,denominator,formula,reference in (
        ('INS12',answered,incoming,'Σatendidas ÷ Σentrantes × 100','Nat 6; TR INS12'),
        ('INS13',answered_30,answered,'Σatendidas em até 30 s ÷ Σatendidas × 100','Nat 5.A; TR INS13'),
    ):
        metric = measure(numerator,denominator,goal=adjusted_goal('95','minimum',period))
        metrics.append({'code':code,'formula':formula,'reference':reference,'base_goal':'95',
                        'factor':pao_factor(period),**metric})
    return {'project':project,'service_id':service_id,'period':period,'count':count,'first':first,'cut':cut,
            'partial':not count or first != period.start or cut != period.end or any(c['missing_days'] for c in coverage),
            'coverage':coverage, 'sources':sources,'metrics':metrics}
