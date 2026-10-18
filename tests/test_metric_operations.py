from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.metrics import measure
from app.routes import dashboard


def test_maximum_and_weighted_ratio():
    assert measure(5, mode='value', goal=16, direction='maximum')['meets_goal'] is True
    assert measure(17, mode='value', goal=16, direction='maximum')['meets_goal'] is False
    assert measure(767,779,goal=95)['value'] == Decimal(767)/779*100
    assert measure(0, 100, goal=1)['value'] == 0


@pytest.mark.parametrize('num,den', [(0,0),(1,0),(None,None)])
def test_no_sample_not_approved(num, den):
    result = measure(num,den,goal=0)
    assert result['state'] == 'no_sample'
    assert result['value'] is None and result['meets_goal'] is None


def test_config_preserves_mode_direction_and_denominator(monkeypatch):
    from app.models import DashboardWidget, DashboardSource
    source = DashboardSource(name='Calls', source_table='calls', default_time_field='day')
    widget = DashboardWidget()
    monkeypatch.setattr(dashboard,'dashboard_column_names',lambda _: {'answered','incoming','day'})
    dashboard.apply_dashboard_widget(widget, source, None, {'widget_type':'gauge','gauge_mode':'ratio','goal_direction':'maximum','value_field':'answered','denominator_field':'incoming','time_field':'day','meta_gauge':'47.5'})
    assert (widget.gauge_mode, widget.aggregation,widget.goal_direction,widget.denominator_field,widget.meta_gauge) == ('ratio','sum','maximum','incoming',47.5)
    with pytest.raises(ValueError):
        dashboard.apply_dashboard_widget(widget,source,None, {'widget_type':'gauge','gauge_mode':'ratio'})


def test_gauge_uses_new_metric_instead_of_partition(monkeypatch):
    widget = SimpleNamespace(gauge_mode='ratio',goal_direction='minimum',meta_gauge=95)
    monkeypatch.setattr(dashboard, 'execute_gauge_values', lambda *_: (767,779))
    result = {}
    dashboard.apply_widget_gauge(result,widget,'day',date(2026,9,22),date(2026,10,21))
    assert result['status'] == 'ok'
    assert result['metric']['numerator'] == 767
    assert result['metric']['denominator'] == 779


def test_numeric_average_keeps_measured_precision(monkeypatch):
    widget = SimpleNamespace(gauge_mode='value',goal_direction='maximum',meta_gauge=1)
    monkeypatch.setattr(dashboard, 'execute_gauge_values', lambda *_: (Decimal('0.3333333333333333333333333333'),))
    result = {}
    dashboard.apply_widget_gauge(result,widget,'day',date(2026,9,22),date(2026,10,21))
    assert result['metric']['state'] == 'measured'
    assert result['metric']['value'] == Decimal('0.3333333333333333333333333333')
    assert result['status'] == 'ok'
