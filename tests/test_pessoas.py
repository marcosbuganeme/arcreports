import asyncio
from datetime import date
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from app import pessoas
from app.models import (
    AdminActionLog,
    PessoasAusencia,
    PessoasCertificacao,
    PessoasParametroMes,
    PessoasPosicao,
    PessoasProfissional,
)
from app.routes import pessoas as rotas


class FakeRequest:
    def __init__(self, form=None, query=None):
        self._body = urlencode(form or {}).encode()
        self.query_params = query or {}
        self.headers = {}
        self.cookies = {}

    async def body(self):
        return self._body


@pytest.fixture
def quadro(db_session):
    pessoas.ensure_pessoas_posicoes(db_session)
    return {p.numero: p for p in db_session.query(PessoasPosicao).all()}


@pytest.fixture
def admin(monkeypatch, db_session, admin_local_user):
    monkeypatch.setattr(rotas, "require_admin", lambda request, db: admin_local_user)
    monkeypatch.setattr(rotas, "render", lambda request, template, context, db: SimpleNamespace(template=template, context=context))
    return admin_local_user


def _prof(db, posicao, nome="Fulano", entrada=date(2026, 9, 1), saida=None, motivo=None):
    prof = PessoasProfissional(nome=nome, posicao_id=posicao.id, data_entrada=entrada, data_saida=saida, motivo_saida=motivo)
    db.add(prof)
    db.commit()
    return prof


# ---------------------------------------------------------------- quadro e datas


def test_quadro_minimo_tem_35_posicoes_e_noc_fora(quadro):
    assert len(quadro) == 35
    assert sum(1 for p in quadro.values() if p.item == 1) == 13
    assert sum(1 for p in quadro.values() if p.item == 2 and not p.fora_ins11) == 18
    noc = [p for p in quadro.values() if p.fora_ins11]
    assert len(noc) == 4 and all(p.equipe == "04" and p.horas_dia == 12 for p in noc)


def test_seed_e_idempotente(db_session, quadro):
    pessoas.ensure_pessoas_posicoes(db_session)
    assert db_session.query(PessoasPosicao).count() == 35


def test_dias_uteis_descontam_fim_de_semana_e_feriado():
    feriados = pessoas.parse_feriados("12/10/2026, 2026-11-02, lixo")
    assert feriados == {date(2026, 10, 12), date(2026, 11, 2)}
    assert len(pessoas.dias_uteis_mes(2026, 10, set())) == 22
    assert len(pessoas.dias_uteis_mes(2026, 10, feriados)) == 21


def test_prazo_de_comunicacao_pula_fim_de_semana():
    # sexta 02/10 + 5 dias úteis = sexta 09/10
    assert pessoas.somar_dias_uteis(date(2026, 10, 2), 5, set()) == date(2026, 10, 9)
    assert pessoas.somar_dias_uteis(date(2026, 10, 9), 1, {date(2026, 10, 12)}) == date(2026, 10, 13)


@pytest.mark.parametrize("mes,fator", [(1, 0.5), (2, 0.5), (3, 0.7), (4, 0.7), (5, 0.8), (6, 0.8), (7, 1.0)])
def test_fator_do_pao(mes, fator):
    assert pessoas.fator_pao(mes) == fator


# ---------------------------------------------------------------- INS11


def _dias_outubro():
    return pessoas.dias_uteis_mes(2026, 10, set())


def test_di_conta_vaga_e_ausencia_descoberta(db_session, quadro):
    dias = _dias_outubro()
    ocupada, vaga = quadro[1], quadro[2]
    prof = _prof(db_session, ocupada)
    ausencia = PessoasAusencia(
        posicao_id=ocupada.id, profissional_id=prof.id, tipo="Atestado",
        data_inicio=date(2026, 10, 5), data_fim=date(2026, 10, 11),  # seg a dom: 5 dias úteis
    )
    db_session.add(ausencia)
    db_session.commit()

    linhas = pessoas.calcular_posicoes([ocupada, vaga], [prof], [ausencia], dias)

    assert (linhas[0].faltas, linhas[0].cobertos, linhas[0].di) == (5, 0, 5)
    assert linhas[0].titular.nome == "Fulano"
    assert (linhas[1].faltas, linhas[1].di) == (22, 22)
    assert linhas[1].titular is None


def test_substituto_so_cobre_com_mesmo_perfil_e_qualificado(db_session, quadro):
    dias = _dias_outubro()
    posicao = quadro[1]
    prof = _prof(db_session, posicao)
    base = dict(posicao_id=posicao.id, profissional_id=prof.id, tipo="Férias", substituto_nome="Beltrano")
    so_perfil = PessoasAusencia(**base, data_inicio=date(2026, 10, 5), data_fim=date(2026, 10, 6), substituto_mesmo_perfil=True)
    completo = PessoasAusencia(
        **base, data_inicio=date(2026, 10, 7), data_fim=date(2026, 10, 9),
        substituto_mesmo_perfil=True, substituto_qualificado=True,
    )

    linha = pessoas.calcular_posicoes([posicao], [prof], [so_perfil, completo], dias)[0]

    assert (linha.faltas, linha.cobertos, linha.di) == (5, 3, 2)


def test_entrada_no_meio_do_mes_conta_dias_anteriores_como_vaga(db_session, quadro):
    prof = _prof(db_session, quadro[1], entrada=date(2026, 10, 15))
    linha = pessoas.calcular_posicoes([quadro[1]], [prof], [], _dias_outubro())[0]
    assert linha.di == 10  # 01, 02, 05–09, 12–14


def test_noc_nao_entra_no_ins11(quadro):
    noc = [p for p in quadro.values() if p.fora_ins11]
    linhas = pessoas.calcular_posicoes(noc, [], [], _dias_outubro())
    assert all(l.faltas == 22 and l.di == 0 for l in linhas)


def test_ins11_e_glosa_conferem_com_a_calculadora(quadro):
    """Exemplo da calculadora: 5 Di no Item 01, mês 1, leitura C."""
    linhas = [pessoas.ResultadoPosicao(posicao=p, titular=None) for p in quadro.values()]
    linhas[0].di = 5  # posição 01, Item 01
    itens = pessoas.calcular_itens(linhas, 21, 1, "C", {1: 108004.76, 2: None})
    item1, item2 = itens

    assert item1.n == 13 and item2.n == 18
    assert item1.ins11 == pytest.approx(100 * (1 - 5 / (13 * 21)))  # 98,168...
    assert item1.meta == pytest.approx(49.0)
    assert item1.pontos == 0 and item1.glosa == 0
    leitura_a = next(l for l in item1.leituras if l["chave"] == "A")
    assert leitura_a["pontos"] == 0  # 98,17% está acima de 98%
    assert item2.ins11 == 100 and item2.glosa is None


def test_glosa_na_leitura_a_abaixo_da_meta(quadro):
    linhas = [pessoas.ResultadoPosicao(posicao=p, titular=None) for p in quadro.values() if p.item == 1]
    linhas[0].di = 21
    linhas[1].di = 21  # 42 Di em 13 × 21 = 84,615%
    item1 = pessoas.calcular_itens(linhas, 21, 7, "A", {1: 100000.0, 2: None})[0]
    ins = 100 * (1 - 42 / 273)
    assert item1.pontos == pytest.approx((98 - ins) * 25)
    assert item1.glosa == pytest.approx(100000 * item1.pontos / 1000)


# ---------------------------------------------------------------- rotatividade e alertas


def test_rotatividade_separa_ins09_ins10_e_conta_so_sem_justa_causa(db_session, quadro):
    inicio, fim = pessoas.janela_rotatividade(2026, 1)
    assert (inicio, fim) == (date(2025, 11, 1), date(2026, 1, 31))

    entrada = date(2026, 1, 1)
    profs = [_prof(db_session, quadro[n], f"Central {n}", entrada) for n in (1, 2, 3, 4)]
    profs += [
        _prof(db_session, quadro[5], "Desligado", entrada, date(2026, 9, 10), "Desligamento sem justa causa"),
        _prof(db_session, quadro[6], "Pediu", entrada, date(2026, 9, 11), "Pedido de demissão"),
        _prof(db_session, quadro[14], "NOC", entrada, date(2026, 9, 12), "Desligamento sem justa causa"),
        _prof(db_session, quadro[18], "Infra", entrada),
        _prof(db_session, quadro[19], "Antigo", entrada, date(2026, 5, 1), "Desligamento sem justa causa"),
    ]
    inicio, fim = pessoas.janela_rotatividade(2026, 10)
    ins10, ins09 = pessoas.calcular_rotatividade(list(quadro.values()), profs, inicio, fim)

    assert ins10["indicador"] == "INS10" and ins10["equipes"] == "01–03"
    assert (ins10["saidas"], ins10["alocados"]) == (1, 4)
    assert ins10["percentual"] == pytest.approx(25.0) and ins10["na_meta"] is False
    assert ins09["indicador"] == "INS09" and ins09["equipes"] == "05–12"
    assert (ins09["saidas"], ins09["alocados"]) == (0, 1) and ins09["na_meta"] is True


def test_prazo_comunicacao_planejada_e_imprevista(quadro):
    ferias = PessoasAusencia(tipo="Férias", data_inicio=date(2026, 10, 19), data_fim=date(2026, 10, 30))
    atestado = PessoasAusencia(tipo="Atestado", data_inicio=date(2026, 10, 16), data_fim=date(2026, 10, 16))
    # 5 dias úteis antes de seg 19/10, com feriado em 12/10 = sex 09/10
    assert pessoas.prazo_comunicacao(ferias, {date(2026, 10, 12)}) == date(2026, 10, 9)
    # imprevista: dia útil seguinte (sex 16/10 -> seg 19/10)
    assert pessoas.prazo_comunicacao(atestado, set()) == date(2026, 10, 19)


def test_alertas(db_session, quadro):
    hoje = date(2026, 10, 20)
    posicoes = [quadro[1], quadro[2]]
    prof = _prof(db_session, quadro[1])
    vencida = PessoasCertificacao(profissional_id=prof.id, nome="ITIL", data_validade=date(2026, 10, 1))
    a_vencer = PessoasCertificacao(profissional_id=prof.id, nome="COBIT", data_validade=date(2026, 11, 5))
    base = dict(posicao_id=quadro[1].id, profissional_id=prof.id)
    sem_comunicar = PessoasAusencia(**base, tipo="Férias", data_inicio=date(2026, 10, 26), data_fim=date(2026, 10, 30))
    ferias_futuras = PessoasAusencia(**base, tipo="Férias", data_inicio=date(2026, 11, 3), data_fim=date(2026, 11, 6))
    falta_hoje = PessoasAusencia(**base, tipo="Falta", data_inicio=date(2026, 10, 20), data_fim=date(2026, 10, 20))
    atrasada = PessoasAusencia(**base, tipo="Atestado", data_inicio=date(2026, 10, 1), data_fim=date(2026, 10, 2),
                               comunicado_em=date(2026, 10, 15))
    ausencias = [sem_comunicar, ferias_futuras, falta_hoje, atrasada]
    db_session.add_all([vencida, a_vencer, *ausencias])
    db_session.commit()

    alertas = pessoas.calcular_alertas(posicoes, [prof], ausencias, [vencida, a_vencer], hoje, set())
    tipos = sorted(a["tipo"] for a in alertas)

    assert tipos == sorted([
        "Posição vaga",
        "Certificação vencida (TRS5)",
        "Certificação a vencer",
        "Substituição não comunicada (TRS13)",
        "Comunicação fora do prazo (TRS13)",
    ])


# ---------------------------------------------------------------- rotas


def test_cadastro_de_profissional_bloqueia_posicao_ocupada(db_session, quadro, admin):
    form = {"nome": "Fulano", "posicao_id": quadro[1].id, "data_entrada": "2026-09-22"}
    resposta = asyncio.run(rotas.pessoas_profissional_create(FakeRequest(form), db_session))
    assert resposta.status_code == 303 and "message=" in resposta.headers["location"]

    form = {"nome": "Ciclano", "posicao_id": quadro[1].id, "data_entrada": "2026-10-01"}
    resposta = asyncio.run(rotas.pessoas_profissional_create(FakeRequest(form), db_session))
    assert "error=" in resposta.headers["location"]
    assert db_session.query(PessoasProfissional).count() == 1
    assert db_session.query(AdminActionLog).filter_by(action="pessoas_profissional_create").count() == 1


def test_saida_exige_motivo_e_libera_posicao(db_session, quadro, admin):
    prof = _prof(db_session, quadro[1])
    form = {"nome": prof.nome, "posicao_id": quadro[1].id, "data_entrada": "2026-09-01", "data_saida": "2026-10-10"}
    resposta = asyncio.run(rotas.pessoas_profissional_update(prof.id, FakeRequest(form), db_session))
    assert "error=" in resposta.headers["location"]

    form["motivo_saida"] = "Desligamento sem justa causa"
    asyncio.run(rotas.pessoas_profissional_update(prof.id, FakeRequest(form), db_session))
    assert prof.data_saida == date(2026, 10, 10)

    novo = {"nome": "Substituto", "posicao_id": quadro[1].id, "data_entrada": "2026-10-13"}
    resposta = asyncio.run(rotas.pessoas_profissional_create(FakeRequest(novo), db_session))
    assert "message=" in resposta.headers["location"]


def test_ausencia_usa_posicao_do_profissional_e_ignora_flags_sem_substituto(db_session, quadro, admin):
    prof = _prof(db_session, quadro[5])
    form = {
        "profissional_id": prof.id, "posicao_id": quadro[9].id, "tipo": "Férias",
        "data_inicio": "2026-10-05", "data_fim": "", "substituto_mesmo_perfil": "on", "substituto_qualificado": "on",
    }
    asyncio.run(rotas.pessoas_ausencia_create(FakeRequest(form), db_session))
    ausencia = db_session.query(PessoasAusencia).one()
    assert ausencia.posicao_id == quadro[5].id
    assert ausencia.data_fim == date(2026, 10, 5)
    assert not ausencia.substituto_mesmo_perfil and not ausencia.substituto_qualificado


def test_parametros_aceitam_valor_em_formato_brasileiro(db_session, admin):
    form = {
        "competencia": "2026-10", "mes_execucao": "1", "leitura_pao": "C",
        "feriados": "12/10/2026, 02/11/2026", "valor_item1": "108.004,76", "valor_item2": "",
    }
    asyncio.run(rotas.pessoas_parametros_save(FakeRequest(form), db_session))
    parametro = db_session.get(PessoasParametroMes, "2026-10")
    assert float(parametro.valor_item1) == pytest.approx(108004.76)
    assert parametro.valor_item2 is None
    assert parametro.feriados == "2026-10-12"  # feriado de outro mês descartado


def test_painel_monta_contexto(db_session, quadro, admin):
    _prof(db_session, quadro[1])
    resposta = rotas.pessoas_painel(FakeRequest(query={"competencia": "2026-10"}), db_session)
    assert resposta.template == "pessoas_painel.html"
    assert resposta.context["dias_uteis"] == 22
    assert len(resposta.context["linhas"]) == 35
    assert resposta.context["glosa_total"] is None


def test_templates_renderizam(db_session, quadro, admin_local_user):
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("templates"))
    env.globals.update(
        url_for=lambda name, path="": f"/{name}/{path}",
        menu_dashboards_for_user=lambda user: [],
        appearance={},
    )
    env.filters["portal_dt"] = lambda value, fmt="": str(value)
    base = {"request": SimpleNamespace(), "current_user": admin_local_user, "csrf_token": "t", "message": None, "error": None,
            "app_name": "ArcReports", "sql_console_enabled": True}

    prof = _prof(db_session, quadro[1])
    db_session.add(PessoasCertificacao(profissional_id=prof.id, nome="ITIL", data_validade=date(2026, 1, 1)))
    db_session.add(PessoasAusencia(posicao_id=quadro[1].id, profissional_id=prof.id, tipo="Férias",
                                   data_inicio=date(2026, 10, 5), data_fim=date(2026, 10, 9)))
    db_session.add(PessoasParametroMes(competencia="2026-10", feriados="2026-10-12", mes_execucao=1, leitura_pao="C",
                                       valor_item1=108004.76))
    db_session.commit()

    painel = pessoas.montar_painel(db_session, "2026-10", hoje=date(2026, 10, 20))
    html = env.get_template("pessoas_painel.html").render(**base, active="pessoas", active_tab="painel", **painel)
    assert "Item 01" in html and "Gera glosa" in html and "Certificação vencida" in html

    ctx = dict(base, active="pessoas", active_tab="profissionais", profissionais=[prof], posicoes=list(quadro.values()),
               motivos=pessoas.MOTIVOS_SAIDA, mostrar="ativos", editar_id=prof.id, hoje=date(2026, 10, 20))
    assert "Excluir definitivamente" in env.get_template("pessoas_profissionais.html").render(**ctx)

    ctx = dict(base, active="pessoas", active_tab="ausencias", competencia="2026-10",
               ausencias=db_session.query(PessoasAusencia).all(), posicoes=list(quadro.values()),
               profissionais=[prof], tipos=pessoas.TIPOS_AUSENCIA)
    assert "Fulano" in env.get_template("pessoas_ausencias.html").render(**ctx)

    parametro = db_session.get(PessoasParametroMes, "2026-10")
    ctx = dict(base, active="pessoas", active_tab="parametros", competencia="2026-10", parametro=parametro,
               dias_uteis=21, leituras=pessoas.LEITURAS_PAO, historico=[parametro])
    html = env.get_template("pessoas_parametros.html").render(**ctx)
    assert "108.004,76" in html and "12/10/2026" in html

    ctx = dict(base, active="pessoas", active_tab="posicoes", posicoes=list(quadro.values()))
    assert "fora do INS11" in env.get_template("pessoas_posicoes.html").render(**ctx)
