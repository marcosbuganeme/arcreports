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


def calculation_evidence(context):
    """Representação única, estável e sem arredondar o valor usado na avaliação."""
    period, report = context['period'], context['ura']
    def number(value):
        return str(value) if value is not None else None
    window = context['staffing_window']
    previous = context['previous_period']
    return {
        'schema_version':1, 'rule_set':'RD01-RD12 / TR e respostas Nat conferidas em 17/10/2026',
        'project':report['project'], 'competence':period.label,
        'period':{'start':period.start.isoformat(),'end_inclusive':period.end.isoformat(),
                  'end_exclusive':(period.end+timedelta(days=1)).isoformat()},
        'comparison':{'state':'disponivel' if previous else 'sem_base_contratual',
                      'competence':previous.label if previous else None,
                      'start':previous.start.isoformat() if previous else None,
                      'end':previous.end.isoformat() if previous else None},
        'pao':{'contract_month':context['contract_month'],'factor':number(context['factor']),
               'rule':'meses 1/2=0.5; 3/4=0.7; 5/6=0.8; 7+=1'},
        'knowledge':{'trimester':context['trimester'],'minimum_goal':number(context['knowledge_goal']),
                     'rule':'INS06/07: 60/70/80/90/95%; meta sem redução PAO; trimestre contínuo'},
        'staffing':{'state':'vigente' if window else 'nao_vigente',
                    'window_start':window[0].isoformat() if window else None,
                    'window_end':window[1].isoformat() if window else None,
                    'result':None,'rule':'INS09/10: desde mês 3; três competências; fórmula/dados pendentes'},
        'simulation':{'base_goal':number(context['base_goal']),'direction':context['direction'],
                      'adjusted_goal':number(context['goal']),'base_points':number(context['base_points']),
                      'weighted_points':number(context['weighted_points']),
                      'rule':'mínima × PAO; máxima ÷ PAO; pontos-base informados × PAO; não apura desvio nem fração'},
        'series':{'source':'CSV diário Comunix/4Biz importado localmente; sem consulta à API',
                  'service_id':report['service_id'],'row_count':report['count'],
                  'first_day':report['first'].isoformat() if report['first'] else None,
                  'cut_day':report['cut'].isoformat() if report['cut'] else None,
                  'partial':report['partial'],'coverage':report['coverage'],
                  'files':[{'import_id':run.id,'name':run.source_name,'sha256':run.sha256,
                            'imported_at_utc':run.imported_at.isoformat(),'username':run.username} for run in report['sources']]},
        'metrics':[{key:number(value) if key in {'value','numerator','denominator','goal','factor'} else value
                    for key,value in metric.items()} for metric in report['metrics']],
        'pending':['Conferir SQLs/widgets instalados no banco reports e conciliar GLPI.',
                   'Confirmar fórmulas/dados pendentes e política de pontos por desvio; sem total financeiro.',
                   'API Comunix, implantação e homologação ainda não executadas.'],
    }


def evidence_rows(value, path=''):
    """Folhas para CSV/Excel: preserva a precisão exportada no JSON e no detalhe."""
    if isinstance(value,dict):
        for key,item in value.items():
            yield from evidence_rows(item, f'{path}.{key}' if path else key)
    elif isinstance(value,list):
        for index,item in enumerate(value):
            yield from evidence_rows(item, f'{path}[{index}]')
    else:
        yield path, '' if value is None else str(value)
