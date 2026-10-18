from datetime import date

import pytest

from app.billing_rules import Competence


@pytest.mark.parametrize("label,start,end", [
    ("2026-10", date(2026,9,22), date(2026,10,21)),
    ("2026-11", date(2026,10,22), date(2026,11,21)),
    ("2027-01", date(2026,12,22), date(2027,1,21)),
    ("2028-03", date(2028,2,22), date(2028,3,21)),
])
def test_competence_boundaries(label, start, end):
    period = Competence.parse(label)
    assert (period.start, period.end, period.label) == (start, end, label)
    assert Competence.containing(start) == Competence.containing(end) == period


@pytest.mark.parametrize("value", ["", "2026-13", "2026-00", "2026-1", "2026-10-01", "0000-01"])
def test_invalid_competence(value):
    with pytest.raises(ValueError):
        Competence.parse(value)


def test_previous_competence_and_first_month():
    from app.routes.dashboard import calcular_periodo_comparativo
    assert calcular_periodo_comparativo(date(2026,10,22), date(2026,11,21), 'competencia_anterior') == (date(2026,9,22), date(2026,10,21))
    assert calcular_periodo_comparativo(date(2026,9,22), date(2026,10,21), 'competencia_anterior') is None
    assert calcular_periodo_comparativo(date(2026,11,1), date(2026,11,21), 'competencia_anterior') is None


@pytest.mark.parametrize('label,factor', [('2026-10','0.5'),('2026-11','0.5'),('2026-12','0.7'),('2027-01','0.7'),('2027-02','0.8'),('2027-03','0.8'),('2027-04','1'),('2028-04','1')])
def test_pao_historical(label, factor):
    from app.billing_rules import pao_factor
    from decimal import Decimal
    assert pao_factor(Competence.parse(label)) == Decimal(factor)


def test_pao_rejects_before_contract():
    from app.billing_rules import pao_factor
    with pytest.raises(ValueError):
        pao_factor(Competence.parse('2026-09'))


@pytest.mark.parametrize('label,base,direction,expected', [('2026-10','95','minimum','47.5'),('2026-10','8','maximum','16'),('2026-12','8','maximum','11.42857142857142857142857143'),('2027-02','8','maximum','10')])
def test_adjusted_goals(label, base, direction, expected):
    from app.billing_rules import adjusted_goal
    from decimal import Decimal
    assert adjusted_goal(base, direction, Competence.parse(label)) == Decimal(expected)


@pytest.mark.parametrize('value', ['NaN','Infinity','-1','1e100','0.0000000000001'])
def test_reject_invalid_decimal(value):
    from app.billing_rules import decimal_value
    with pytest.raises(ValueError):
        decimal_value(value)
