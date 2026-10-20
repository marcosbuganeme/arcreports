import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from app import pessoas, trs
from app.models import (
    PessoasAusencia,
    PessoasCertificacao,
    PessoasPosicao,
    PessoasProfissional,
    TrsOcorrencia,
    TrsTipo,
)
from app.routes import trs as rotas
from app.system_config import set_system_config_value


class FakeRequest:
    def __init__(self, form=None, query=None):
        self._body = urlencode(form or {}, doseq=True).encode()
        self.query_params = query or {}
        self.headers = {}
        self.cookies = {}

    async def body(self):
        return self._body


@pytest.fixture
def base(db_session):
    pessoas.ensure_pessoas_posicoes(db_session)
    trs.ensure_trs_tipos(db_session)
    return {p.numero: p for p in db_session.query(PessoasPosicao).all()}


@pytest.fixture
def admin(monkeypatch, admin_local_user):
    monkeypatch.setattr(rotas, "require_admin", lambda request, db: admin_local_user)
    monkeypatch.setattr(rotas, "render", lambda request, template, context, db: SimpleNamespace(template=template, context=context))
    return admin_local_user


def _prof(db, posicao, nome="Fulano", entrada=date(2026, 9, 22), saida=None):
    p = PessoasProfissional(nome=nome, posicao_id=posicao.id, data_entrada=entrada, data_saida=saida)
    db.add(p)
    db.commit()
    return p


def _oc(db, codigo, dia, **extra):
    dados = dict(codigo=codigo, data_ocorrencia=dia, competencia=trs.competencia_de(dia), evidencia="teste")
    dados.update(extra)
    oc = TrsOcorrencia(**dados)
    db.add(oc)
    db.commit()
    return oc


# ---------------------------------------------------------------- catálogo e pontos


def test_catalogo_segue_tabela_26(db_session, base):
    tipos = {t.codigo: t for t in db_session.query(TrsTipo).all()}
    assert len(tipos) == 48 and "TRS34" not in tipos
    assert (tipos["TRS4"].pontos, tipos["TRS4"].unidade) == (100, "indicador")
    assert (tipos["TRS5"].pontos, tipos["TRS5"].unidade) == (5, "dia_profissional")
    assert (tipos["TRS29"].pontos, tipos["TRS29"].unidade) == (20, "mes_progressivo")
    assert (tipos["TRS32"].pontos, tipos["TRS32"].unidade) == (10, "dia_excedente")
    assert (tipos["TRS40"].pontos, tipos["TRS41"].pontos) == (100, 200)
    assert {c for c, t in tipos.items() if t.regra == "reincidencia"} == {"TRS1", "TRS49"}
    trs.ensure_trs_tipos(db_session)
    assert db_session.query(TrsTipo).count() == 48


def test_pontos_por_quantidade_status_e_natureza(db_session, base):
    tipo5 = db_session.get(TrsTipo, "TRS5")
    tipo29 = db_session.get(TrsTipo, "TRS29")
    dia = date(2026, 10, 5)
    assert trs.pontos_brutos(_oc(db_session, "TRS5", dia, quantidade=10), tipo5) == 50
    assert trs.pontos_brutos(_oc(db_session, "TRS29", dia, quantidade=3), tipo29) == 60  # 3º mês de atraso
    assert trs.pontos_brutos(_oc(db_session, "TRS5", dia, quantidade=4, status="Cancelada"), tipo5) == 0
    assert trs.pontos_brutos(_oc(db_session, "TRS5", dia, quantidade=4, status="Contestada"), tipo5) == 20
    assert trs.pontos_brutos(_oc(db_session, "TRS1", dia, natureza="notificacao"), db_session.get(TrsTipo, "TRS1")) == 0


def test_mes_execucao_e_fator(db_session, base):
    assert trs.mes_execucao(db_session, "2026-09") == 1
    assert trs.mes_execucao(db_session, "2026-10") == 2
    assert trs.mes_execucao(db_session, "2027-03") == 7
    assert pessoas.fator_pao(trs.mes_execucao(db_session, "2026-11")) == 0.7


# ---------------------------------------------------------------- rotas: regras de registro


def _form(codigo, dia="2026-10-05", **extra):
    form = {"codigo": codigo, "data_ocorrencia": dia, "quantidade": "1", "evidencia": "Ofício SEI 123", "fonte": "Fiscalização (DETRAN)"}
    form.update(extra)
    return form


def test_trs1_exige_notificacao_previa(db_session, base, admin):
    r = asyncio.run(rotas.trs_ocorrencia_create(FakeRequest(_form("TRS1")), db_session))
    assert "error=" in r.headers["location"] and db_session.query(TrsOcorrencia).count() == 0

    asyncio.run(rotas.trs_ocorrencia_create(FakeRequest(_form("TRS1", "2026-10-01", natureza="notificacao")), db_session))
    r = asyncio.run(rotas.trs_ocorrencia_create(FakeRequest(_form("TRS1", "2026-10-05")), db_session))
    assert "message=" in r.headers["location"]
    oc = db_session.query(TrsOcorrencia).filter_by(natureza="ocorrencia").one()
    assert oc.competencia == "2026-10" and oc.registrado_por == admin.username


def test_notificacao_so_para_trs_de_reincidencia_e_evidencia_obrigatoria(db_session, base, admin):
    r = asyncio.run(rotas.trs_ocorrencia_create(FakeRequest(_form("TRS7", natureza="notificacao")), db_session))
    assert "error=" in r.headers["location"]
    r = asyncio.run(rotas.trs_ocorrencia_create(FakeRequest(_form("TRS7", evidencia="")), db_session))
    assert "error=" in r.headers["location"]
    assert db_session.query(TrsOcorrencia).count() == 0


def test_contestar_exige_justificativa(db_session, base, admin):
    asyncio.run(rotas.trs_ocorrencia_create(FakeRequest(_form("TRS31")), db_session))
    oc = db_session.query(TrsOcorrencia).one()
    r = asyncio.run(rotas.trs_ocorrencia_update(oc.id, FakeRequest(_form("TRS31", status="Contestada")), db_session))
    assert "error=" in r.headers["location"] and oc.status == "Em aberto"
    asyncio.run(rotas.trs_ocorrencia_update(oc.id, FakeRequest(_form("TRS31", status="Contestada", justificativa="Reunião desmarcada pelo DETRAN")), db_session))
    assert oc.status == "Contestada"


# ---------------------------------------------------------------- detecção automática


def test_detecta_trs5_dias_vencidos_no_mes(db_session, base):
    prof = _prof(db_session, base[18])
    db_session.add(PessoasCertificacao(profissional_id=prof.id, nome="CCNA", data_validade=date(2026, 10, 10)))
    db_session.commit()
    s = trs.detectar_trs5(db_session, "2026-10", date(2026, 10, 20))
    assert len(s) == 1 and s[0]["quantidade"] == 10 and s[0]["data"] == date(2026, 10, 11)  # 11 a 20/10
    s = trs.detectar_trs5(db_session, "2026-10", date(2026, 11, 5))
    assert s[0]["quantidade"] == 21  # 11 a 31/10


def test_detecta_trs13(db_session, base):
    prof = _prof(db_session, base[5])
    db_session.add(PessoasAusencia(posicao_id=base[5].id, profissional_id=prof.id, tipo="Férias",
                                   data_inicio=date(2026, 10, 19), data_fim=date(2026, 10, 23)))
    db_session.add(PessoasAusencia(posicao_id=base[5].id, profissional_id=prof.id, tipo="Atestado",
                                   data_inicio=date(2026, 10, 1), data_fim=date(2026, 10, 1), comunicado_em=date(2026, 10, 2)))
    db_session.commit()
    s = trs.detectar_trs13(db_session, "2026-10", date(2026, 10, 14))
    assert [x["codigo"] for x in s] == ["TRS13"] and "sem comunicação" in s[0]["evidencia"]


def test_detecta_trs29_progressivo(db_session, base):
    assert trs.detectar_trs29(db_session, "2026-11", date(2026, 11, 20)) == []
    s = trs.detectar_trs29(db_session, "2026-11", date(2026, 12, 5))
    assert s[0]["quantidade"] == 1 and s[0]["data"] == date(2026, 11, 22)
    assert trs.detectar_trs29(db_session, "2026-12", date(2027, 1, 10))[0]["quantidade"] == 2
    set_system_config_value(db_session, trs.CONFIG_CMDB, "2026-11-10")
    assert trs.detectar_trs29(db_session, "2026-12", date(2027, 1, 10)) == []


def test_detecta_trs17(db_session, base):
    # relatório de outubro: 5º dia útil de novembro = 06/11/2026 (sem feriados cadastrados)
    assert trs.detectar_trs17(db_session, "2026-11", date(2026, 11, 6)) == []
    s = trs.detectar_trs17(db_session, "2026-11", date(2026, 11, 9))
    assert s[0]["quantidade"] == 3 and "contagem parcial" in s[0]["evidencia"]
    set_system_config_value(db_session, trs.CONFIG_RELATORIO.format(competencia="2026-10"), "2026-11-10")
    s = trs.detectar_trs17(db_session, "2026-11", date(2026, 11, 30))
    assert s[0]["quantidade"] == 4 and s[0]["chave"] == "TRS17:2026-10"


def test_detecta_trs46_tres_meses(db_session, base, monkeypatch):
    valores = {"2026-10": 70.0, "2026-11": 75.0, "2026-12": 79.9}
    monkeypatch.setattr(trs, "ins11_geral", lambda db, comp: valores.get(comp))
    assert trs.detectar_trs46(db_session, "2026-12", date(2026, 12, 31)) == []  # mês não encerrado
    assert trs.detectar_trs46(db_session, "2026-12", date(2027, 1, 2))[0]["codigo"] == "TRS46"
    valores["2026-11"] = 80.0
    assert trs.detectar_trs46(db_session, "2026-12", date(2027, 1, 2)) == []


def test_detecta_trs45_pelo_glpi(db_session, base, monkeypatch):
    rows = [{"id": 140001, "titulo": "Sem acesso", "abertura": "2026-10-01 08:00:00", "solucao": None, "limite3": "2026-10-02 08:00:00"}]
    monkeypatch.setattr(trs, "consultar_glpi_trs45", lambda db, inicio, fim, agora: rows)
    s, erro = trs.detectar_trs45(db_session, "2026-10", date(2026, 10, 14))
    assert erro is None and s[0]["chave"] == "TRS45:t140001" and s[0]["data"] == date(2026, 10, 2)

    def falha(*a):
        raise RuntimeError("sem acesso ao glpi")
    monkeypatch.setattr(trs, "consultar_glpi_trs45", falha)
    s, erro = trs.detectar_trs45(db_session, "2026-10", date(2026, 10, 14))
    assert s == [] and "GLPI" in erro


def test_registrar_sugestoes_nao_duplica(db_session, base, admin, monkeypatch):
    rows = [{"id": 1, "titulo": "A", "abertura": "2026-10-01", "solucao": None, "limite3": "2026-10-03"},
            {"id": 2, "titulo": "B", "abertura": "2026-10-01", "solucao": None, "limite3": "2026-10-04"}]
    monkeypatch.setattr(trs, "consultar_glpi_trs45", lambda *a: rows)
    form = {"competencia": "2026-10", "chave": ["TRS45:t1", "TRS45:t2"]}
    r = asyncio.run(rotas.trs_sugestoes_registrar(FakeRequest(form), db_session))
    assert "2 ocorr" in r.headers["location"].replace("%20", " ")
    assert db_session.query(TrsOcorrencia).filter_by(origem="automatico").count() == 2
    pendentes, _ = trs.sugestoes_competencia(db_session, "2026-10", date(2026, 10, 14))
    assert [p for p in pendentes if p["codigo"] == "TRS45"] == []


# ---------------------------------------------------------------- apuração


def test_apuracao_soma_ins_trs_com_fator_e_teto(db_session, base, monkeypatch):
    monkeypatch.setattr(trs, "pontos_ins", lambda db, comp: ([{"indicador": "INS02 P1", "valor": "80", "meta": "95", "sentido": ">=",
                                                               "pontos": "75", "fator_pao": "0.5", "pontos_ponderados": "37.5",
                                                               "situacao": "Fora da meta"}], None))
    _oc(db_session, "TRS7", date(2026, 10, 5))                         # 50
    _oc(db_session, "TRS5", date(2026, 10, 6), quantidade=4)           # 20
    _oc(db_session, "TRS31", date(2026, 10, 7), status="Cancelada")    # 0
    _oc(db_session, "TRS1", date(2026, 10, 1), natureza="notificacao")  # 0
    a = trs.montar_apuracao(db_session, "2026-10")
    assert a["fator"] == 0.5 and a["trs_brutos"] == 70 and a["trs_ponderados"] == 35
    assert a["total_ponderado"] == pytest.approx(72.5) and a["glosa_pct"] == pytest.approx(7.25)
    assert [l["codigo"] for l in a["linhas_trs"]] == ["TRS1", "TRS5", "TRS7", "TRS31"]

    _oc(db_session, "TRS41", date(2026, 10, 8), quantidade=3)          # 600 -> 300 ponderados
    a = trs.montar_apuracao(db_session, "2026-10")
    assert a["no_teto"] and a["glosa_pct"] == 30.0


def test_painel_pessoas_comeca_em_22_09(db_session, base):
    _prof(db_session, base[1])
    painel = pessoas.montar_painel(db_session, "2026-09", hoje=date(2026, 10, 1))
    assert painel["dias_uteis"] == 7  # 22 a 30/09


# ---------------------------------------------------------------- telas


def test_telas_trs_renderizam(db_session, base, admin_local_user, monkeypatch):
    from jinja2 import Environment, FileSystemLoader

    monkeypatch.setattr(trs, "consultar_glpi_trs45", lambda *a: [])
    env = Environment(loader=FileSystemLoader("templates"))
    env.globals.update(url_for=lambda name, path="": f"/{name}/{path}", menu_dashboards_for_user=lambda user: [], appearance={})
    env.filters["portal_dt"] = lambda value, fmt="": str(value)
    comum = {"request": SimpleNamespace(), "current_user": admin_local_user, "csrf_token": "t", "message": None, "error": None,
             "app_name": "ArcReports", "sql_console_enabled": True, "active": "trs", "unidades": trs.UNIDADES, "competencia": "2026-10"}
    prof = _prof(db_session, base[18])
    db_session.add(PessoasCertificacao(profissional_id=prof.id, nome="CCNA", data_validade=date(2026, 10, 1)))
    db_session.commit()
    oc = _oc(db_session, "TRS5", date(2026, 10, 2), quantidade=3, profissional_id=prof.id, justificativa="x")
    tipos = db_session.query(TrsTipo).order_by(TrsTipo.ordem).all()

    html = env.get_template("trs_ocorrencias.html").render(**comum, active_tab="ocorrencias", filtro_status="", tipos=tipos,
                                                           ocorrencias=[(oc, 15)], profissionais=[prof], status_lista=trs.STATUS,
                                                           fontes=trs.FONTES, editar_id=oc.id, hoje=date(2026, 10, 14))
    assert "TRS5" in html and "Excluir definitivamente" in html and "Nº do mês de atraso" in html

    sug, avisos = trs.sugestoes_competencia(db_session, "2026-10", date(2026, 10, 14))
    html = env.get_template("trs_sugestoes.html").render(**comum, active_tab="sugestoes", sugestoes=sug, avisos=avisos,
                                                         tipos={t.codigo: t for t in tipos}, automaticos=sorted(trs.AUTOMATICOS))
    assert "Registrar selecionadas" in html or "Nenhuma sugestão" in html

    monkeypatch.setattr(trs, "pontos_ins", lambda db, comp: ([], "A apuração dos indicadores não está disponível."))
    ctx = {**comum, **trs.montar_apuracao(db_session, "2026-10")}
    html = env.get_template("trs_apuracao.html").render(**ctx, active_tab="apuracao", relatorio_competencia="2026-09",
                                                        relatorio_entregue="", cmdb_implantado="")
    assert "Glosa estimada" in html and "TRS5" in html

    html = env.get_template("trs_catalogo.html").render(**comum, active_tab="catalogo", tipos=tipos, automaticos=trs.AUTOMATICOS)
    assert html.count('action="/trs/catalogo/') == 48
