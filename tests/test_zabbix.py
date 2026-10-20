import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from app import zabbix_ind as z
from app.models import SystemConfig, ZabbixSli
from app.routes import zabbix as rotas


DIA = 86400


def _ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=z.TZ).timestamp())


class FakeCliente:
    """Simula a API: 2 serviços; o serviço 2 fica 1 hora fora em 23/09."""

    def __init__(self):
        self.chamadas = []

    def chamar(self, metodo, params, autenticar=True):
        self.chamadas.append(metodo)
        if metodo == "service.get":
            return [{"serviceid": s, "name": {"1": "Portal Detran", "2": "SEI"}[s]} for s in map(str, params["serviceids"])]
        if metodo == "sla.getsli":
            inicio, fim = params["period_from"], params["period_to"]
            periodos = [{"period_from": t, "period_to": t + DIA} for t in range(inicio, fim, DIA)]
            linhas = []
            for p in periodos:
                dia = datetime.fromtimestamp(p["period_from"], z.TZ).date()
                fora = 3600 if dia == date(2026, 9, 23) else 0
                linhas.append([{"uptime": DIA, "downtime": 0, "excluded_downtimes": []},
                               {"uptime": DIA - fora, "downtime": fora, "excluded_downtimes": []}])
            return {"periods": periodos, "serviceids": [1, 2], "sli": linhas}
        raise AssertionError(metodo)


@pytest.fixture
def configurado(db_session, monkeypatch):
    z.salvar_config(db_session, "https://zabbix.exemplo", "token-secreto", True, "22", "")
    fake = FakeCliente()
    monkeypatch.setattr(z, "cliente", lambda db: fake)
    return fake


def test_token_guardado_criptografado(db_session):
    z.salvar_config(db_session, "https://zabbix.exemplo/", "token-secreto", False, "22", "23")
    bruto = db_session.get(SystemConfig, z.CFG_TOKEN).value
    assert bruto != "token-secreto" and z._token(db_session) == "token-secreto"
    cfg = z.config(db_session)
    assert cfg["tem_token"] and not cfg["verificar"] and cfg["sla"] == {"IND01": "22", "IND02": "23"}
    z.salvar_config(db_session, "https://zabbix.exemplo/", None, True, "22", "23")
    assert z._token(db_session) == "token-secreto"  # token em branco mantém o atual


def test_endpoint_da_api():
    assert z.Cliente("https://zabbix.detran.go.gov.br/", "t").endpoint == "https://zabbix.detran.go.gov.br/api_jsonrpc.php"
    assert z.Cliente("https://x/api_jsonrpc.php", "t").endpoint == "https://x/api_jsonrpc.php"


def test_coleta_grava_sli_diario_e_recoleta_sem_duplicar(db_session, configurado):
    c = configurado
    n = z.coletar_indicador(db_session, c, "IND01", 22, date(2026, 9, 22), datetime(2026, 9, 25, tzinfo=z.TZ))
    assert n == 6  # 3 dias x 2 serviços
    sei = db_session.query(ZabbixSli).filter_by(servico="SEI", data=date(2026, 9, 23)).one()
    assert sei.downtime == 3600 and sei.mes == "2026-09" and sei.sli == pytest.approx(100 * 82800 / 86400, abs=1e-3)
    z.coletar_indicador(db_session, c, "IND01", 22, date(2026, 9, 23), datetime(2026, 9, 25, tzinfo=z.TZ))
    assert db_session.query(ZabbixSli).count() == 6


def test_coletar_registra_status_e_ignora_sla_vazio(db_session, configurado):
    resumo = z.coletar(db_session)
    assert isinstance(resumo["IND01"], int) and resumo["IND01"] > 0
    assert resumo["IND02"] == "SLA não configurado"
    assert z.config(db_session)["ultima_coleta"]


def test_resumo_mes_media_aritmetica_por_servico(db_session, configurado):
    z.coletar_indicador(db_session, configurado, "IND01", 22, date(2026, 9, 22), datetime(2026, 9, 25, tzinfo=z.TZ))
    r = {x["indicador"]: x for x in z.resumo_mes(db_session, "2026-09")}
    ind01 = r["IND01"]
    sei = 100 * (3 * DIA - 3600) / (3 * DIA)
    assert ind01["servicos"] == 2 and ind01["dias"] == 3
    assert ind01["valor"] == pytest.approx((100 + sei) / 2)
    assert ind01["piores"][0][0] == "SEI" and ind01["na_meta"] is False  # 99,305% < 99,4%
    assert r["IND02"]["valor"] is None


def test_job_sem_configuracao_nao_faz_nada(db_session, monkeypatch):
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    monkeypatch.setattr(z, "cliente", lambda db: (_ for _ in ()).throw(AssertionError("não devia chamar")))
    z.run_zabbix_job()


class FakeRequest:
    def __init__(self, form=None, query=None):
        self._body = urlencode(form or {}).encode()
        self.query_params = query or {}
        self.headers = {}
        self.cookies = {}

    async def body(self):
        return self._body


def test_rota_salvar_valida_url_e_sla(db_session, admin_local_user, monkeypatch):
    monkeypatch.setattr(rotas, "require_admin", lambda request, db: admin_local_user)
    r = asyncio.run(rotas.zabbix_salvar(FakeRequest({"url": "zabbix", "sla_ind01": "22"}), db_session))
    assert "error=" in r.headers["location"]
    r = asyncio.run(rotas.zabbix_salvar(FakeRequest({"url": "https://z", "sla_ind01": "abc"}), db_session))
    assert "error=" in r.headers["location"]
    r = asyncio.run(rotas.zabbix_salvar(FakeRequest({"url": "https://z", "token": "tk", "sla_ind01": "22", "sla_ind02": "23", "verificar": "on"}), db_session))
    assert "message=" in r.headers["location"] and "testar=1" in r.headers["location"]
    assert z._token(db_session) == "tk"


def test_tela_renderiza(db_session, configurado, admin_local_user):
    from jinja2 import Environment, FileSystemLoader

    z.coletar_indicador(db_session, configurado, "IND01", 22, date(2026, 9, 22), datetime(2026, 9, 24, tzinfo=z.TZ))
    env = Environment(loader=FileSystemLoader("templates"))
    env.globals.update(url_for=lambda name, path="": f"/{name}/{path}", menu_dashboards_for_user=lambda user: [], appearance={})
    env.filters["portal_dt"] = lambda value, fmt="": str(value)
    teste = {"versao": "7.0.31", "hosts": 457, "slas": [{"slaid": "22", "name": "SLA - IND01", "slo": "99.4", "period": "0", "status": "1",
                                                          "service_tags": [{"tag": "tipo_sla", "value": "IND01"}], "excluded_downtimes": "0"}]}
    html = env.get_template("admin_zabbix.html").render(
        request=SimpleNamespace(), current_user=admin_local_user, csrf_token="t", message=None, error=None, app_name="ArcReports",
        sql_console_enabled=True, active="zabbix", cfg=z.config(db_session), competencia="2026-09",
        resumo=z.resumo_mes(db_session, "2026-09"), teste=teste, hoje=date(2026, 9, 30))
    assert "IND01" in html and "SEI" in html and "7.0.31" in html and "token-secreto" not in html


def test_resumo_aceita_decimal_do_mariadb(db_session, monkeypatch):
    """No MariaDB, SUM() devolve Decimal (no SQLite, int)."""
    from decimal import Decimal

    class Q:
        def filter(self, *a): return self
        def group_by(self, *a): return self
        def all(self): return [("Portal", Decimal("86400"), Decimal("0"), 1), ("SEI", Decimal("82800"), Decimal("3600"), 1)]

    monkeypatch.setattr(db_session, "query", lambda *a: Q())
    r = z.resumo_mes(db_session, "2026-10")[0]
    assert r["valor"] == pytest.approx((100 + 100 * 82800 / 86400) / 2)
