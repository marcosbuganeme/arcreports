"""Regras do módulo Pessoas: quadro mínimo (TR Tabela 01), INS11 (Tabela 20) e rotatividade.

Cálculo isolado das rotas para poder ser testado sem banco.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models import PessoasAusencia, PessoasCertificacao, PessoasParametroMes, PessoasPosicao, PessoasProfissional


# Início da prestação dos serviços (PAO): nada antes disso entra na medição.
INICIO_CONTRATO = date(2026, 9, 22)
META_INS11 = 98.0
META_ROTATIVIDADE = 15.0
PONTOS_POR_PONTO_PERCENTUAL = 25
# TR 6.32.3: substituição comunicada com 5 dias úteis de antecedência (TRS13).
PRAZO_COMUNICACAO_DIAS_UTEIS = 5
# Ausências imprevistas não admitem aviso prévio: comunicar até o dia útil seguinte ao início.
PRAZO_COMUNICACAO_IMPREVISTA_DIAS_UTEIS = 1
AUSENCIAS_PLANEJADAS = {"Férias", "Licença", "Treinamento"}
AVISO_CERTIFICACAO_DIAS = 30

TIPOS_AUSENCIA = ["Férias", "Atestado", "Falta", "Licença", "Treinamento", "Vaga aberta", "Outro"]
MOTIVOS_SAIDA = [
    "Desligamento sem justa causa",
    "Desligamento com justa causa",
    "Pedido de demissão",
    "Solicitação do DETRAN",
    "Remanejamento interno",
    "Outro",
]
# TR Tabelas 18 e 19: INS09/INS10 contam só os "desligados sem justa causa".
MOTIVOS_ROTATIVIDADE = {"Desligamento sem justa causa"}
# (indicador, grupo, equipes) — TR Tabelas 18 e 19; a equipe 04 (NOC) fica fora.
GRUPOS_ROTATIVIDADE = [
    ("INS10", "Central de Serviços", ("01", "02", "03")),
    ("INS09", "Infraestrutura de TIC", ("05", "06", "07", "08", "09", "10", "11", "12")),
]

LEITURAS_PAO = {
    "A": "Meta cheia, glosa × fator",
    "B": "Só meta reduzida",
    "C": "Meta reduzida e glosa × fator",
}

# (item, equipe, nome da equipe, fora do INS11, [(perfil, quantidade, modalidade)])
QUADRO_MINIMO = [
    (1, "01", "Central de Serviços · Nível 1", False, [("Operador de Teleatendimento N1", 4, "Presencial")]),
    (1, "02", "Suporte técnico · Nível 2", False, [("Técnico de Suporte da Central Sênior N2", 4, "Presencial")]),
    (1, "03", "Governança de Serviços", False, [
        ("Gerente de Serviços de TIC", 1, "Presencial"),
        ("Analista ITIL Sênior", 1, "Presencial ou híbrido"),
        ("Administrador de Sistemas Sênior (ITSM)", 1, "Presencial ou híbrido"),
        ("Supervisor da Central", 1, "Presencial"),
        ("Gerente de Projetos da Central", 1, "Presencial ou híbrido"),
    ]),
    (2, "04", "NOC 24x7", True, [("Operador de Monitoramento", 4, "CCO Globalweb (autorizado)")]),
    (2, "05", "Redes", False, [
        ("Administrador de Rede Pleno", 1, "Presencial"),
        ("Administrador de Rede Sênior", 1, "Híbrido ou remoto"),
        ("Técnico de Infraestrutura Pleno", 1, "Presencial"),
        ("Técnico de Infraestrutura Sênior", 2, "Presencial"),
        ("Técnico Eletricista", 1, "Presencial"),
    ]),
    (2, "06", "Segurança da Informação", False, [("Administrador de SI Sênior", 1, "Híbrido ou remoto")]),
    (2, "07", "Cibersegurança", False, [("Administrador de SI Pleno", 1, "Presencial")]),
    (2, "08", "Linux", False, [
        ("Administrador Linux Pleno", 1, "Presencial"),
        ("Administrador Linux Sênior", 2, "Híbrido ou remoto"),
    ]),
    (2, "09", "Windows", False, [
        ("Administrador Windows Sênior", 1, "Híbrido ou remoto"),
        ("Técnico de Infraestrutura Sênior", 2, "Presencial"),
    ]),
    (2, "10", "Banco de Dados", False, [("DBA Sênior", 1, "Híbrido ou remoto")]),
    (2, "11", "Administração de Dados", False, [("Administrador de Dados Pleno", 1, "Presencial")]),
    (2, "12", "Gestão de Infraestrutura", False, [
        ("Gerente de Infraestrutura", 1, "Presencial"),
        ("Gerente de Projetos de Infraestrutura", 1, "Presencial ou híbrido"),
    ]),
]


def ensure_pessoas_posicoes(db: Session) -> None:
    """Cria as 35 posições da Tabela 01 na primeira execução."""
    if db.query(PessoasPosicao.id).first():
        return
    numero = 0
    for item, equipe, equipe_nome, fora, perfis in QUADRO_MINIMO:
        for perfil, quantidade, modalidade in perfis:
            for _ in range(quantidade):
                numero += 1
                db.add(
                    PessoasPosicao(
                        numero=numero,
                        item=item,
                        equipe=equipe,
                        equipe_nome=equipe_nome,
                        perfil=perfil,
                        modalidade=modalidade,
                        horas_dia=12 if fora else 8,
                        fora_ins11=fora,
                    )
                )
    db.commit()


# ---------------------------------------------------------------- datas


def parse_competencia(value: str | None, today: date | None = None) -> tuple[int, int]:
    today = today or date.today()
    try:
        ano, mes = (int(part) for part in (value or "").split("-"))
        if 2000 <= ano <= 2100 and 1 <= mes <= 12:
            return ano, mes
    except ValueError:
        pass
    return today.year, today.month


def parse_feriados(text_value: str | None) -> set[date]:
    feriados = set()
    for token in (text_value or "").replace(";", ",").replace("\n", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            if "/" in token:
                dia, mes, ano = token.split("/")
                feriados.add(date(int(ano), int(mes), int(dia)))
            else:
                feriados.add(date.fromisoformat(token))
        except ValueError:
            continue
    return feriados


def dias_uteis_mes(ano: int, mes: int, feriados: set[date]) -> list[date]:
    ultimo = calendar.monthrange(ano, mes)[1]
    dias = (date(ano, mes, dia) for dia in range(1, ultimo + 1))
    return [dia for dia in dias if dia.weekday() < 5 and dia not in feriados]


def somar_dias_uteis(inicio: date, quantidade: int, feriados: set[date]) -> date:
    atual = inicio
    restantes = quantidade
    while restantes > 0:
        atual += timedelta(days=1)
        if atual.weekday() < 5 and atual not in feriados:
            restantes -= 1
    return atual


def subtrair_dias_uteis(fim: date, quantidade: int, feriados: set[date]) -> date:
    atual = fim
    restantes = quantidade
    while restantes > 0:
        atual -= timedelta(days=1)
        if atual.weekday() < 5 and atual not in feriados:
            restantes -= 1
    return atual


def prazo_comunicacao(ausencia: PessoasAusencia, feriados: set[date]) -> date:
    if ausencia.tipo in AUSENCIAS_PLANEJADAS:
        return subtrair_dias_uteis(ausencia.data_inicio, PRAZO_COMUNICACAO_DIAS_UTEIS, feriados)
    return somar_dias_uteis(ausencia.data_inicio, PRAZO_COMUNICACAO_IMPREVISTA_DIAS_UTEIS, feriados)


def fator_pao(mes_execucao: int) -> float:
    if mes_execucao <= 2:
        return 0.5
    if mes_execucao <= 4:
        return 0.7
    if mes_execucao <= 6:
        return 0.8
    return 1.0


# ---------------------------------------------------------------- INS11


@dataclass
class ResultadoPosicao:
    posicao: PessoasPosicao
    titular: PessoasProfissional | None
    faltas: int = 0
    cobertos: int = 0
    di: int = 0


@dataclass
class ResultadoItem:
    item: int
    n: int
    d: int
    soma_di: int
    ins11: float
    meta: float
    pontos: float
    glosa: float | None
    leituras: list[dict] = field(default_factory=list)


def _ocupa(prof: PessoasProfissional, dia: date) -> bool:
    return prof.data_entrada <= dia and (prof.data_saida is None or prof.data_saida >= dia)


def _cobre(ausencia: PessoasAusencia) -> bool:
    return bool(ausencia.substituto_nome and ausencia.substituto_mesmo_perfil and ausencia.substituto_qualificado)


def calcular_posicoes(
    posicoes: list[PessoasPosicao],
    profissionais: list[PessoasProfissional],
    ausencias: list[PessoasAusencia],
    dias: list[date],
) -> list[ResultadoPosicao]:
    """Di = dias úteis em que a posição ficou abaixo do mínimo.

    Um dia conta como falta quando não há titular alocado ou quando há ausência registrada.
    A falta é coberta só se a ausência tiver substituto do mesmo perfil e qualificado (TR 6.34.7).
    """
    resultado = []
    for posicao in posicoes:
        profs = [p for p in profissionais if p.posicao_id == posicao.id]
        auss = [a for a in ausencias if a.posicao_id == posicao.id]
        titular = next((p for p in sorted(profs, key=lambda p: p.data_entrada, reverse=True) if _ocupa(p, dias[-1])), None) if dias else None
        linha = ResultadoPosicao(posicao=posicao, titular=titular)
        for dia in dias:
            ausentes = [a for a in auss if a.data_inicio <= dia <= a.data_fim]
            presente = any(_ocupa(p, dia) for p in profs) and not ausentes
            if presente:
                continue
            linha.faltas += 1
            if any(_cobre(a) for a in ausentes):
                linha.cobertos += 1
        linha.di = 0 if posicao.fora_ins11 else linha.faltas - linha.cobertos
        resultado.append(linha)
    return resultado


def pontos_glosa(ins11: float, meta: float, multiplicador: float) -> float:
    return max(0.0, meta - ins11) * PONTOS_POR_PONTO_PERCENTUAL * multiplicador


def calcular_itens(
    linhas: list[ResultadoPosicao],
    d: int,
    mes_execucao: int,
    leitura: str,
    valores: dict[int, float | None],
) -> list[ResultadoItem]:
    fator = fator_pao(mes_execucao)
    opcoes = {
        "A": (META_INS11, fator),
        "B": (META_INS11 * fator, 1.0),
        "C": (META_INS11 * fator, fator),
    }
    itens = []
    for item in (1, 2):
        do_item = [linha for linha in linhas if linha.posicao.item == item and not linha.posicao.fora_ins11]
        n = len(do_item)
        soma_di = sum(linha.di for linha in do_item)
        ins11 = 100.0 * (1 - soma_di / (n * d)) if n and d else 100.0
        valor = valores.get(item)
        leituras = []
        for chave, (meta, mult) in opcoes.items():
            pontos = pontos_glosa(ins11, meta, mult)
            leituras.append(
                {
                    "chave": chave,
                    "nome": LEITURAS_PAO[chave],
                    "meta": meta,
                    "pontos": pontos,
                    "glosa": float(valor) * pontos / 1000 if valor else None,
                }
            )
        escolhida = next(l for l in leituras if l["chave"] == leitura)
        itens.append(
            ResultadoItem(
                item=item,
                n=n,
                d=d,
                soma_di=soma_di,
                ins11=ins11,
                meta=escolhida["meta"],
                pontos=escolhida["pontos"],
                glosa=escolhida["glosa"],
                leituras=leituras,
            )
        )
    return itens


# ---------------------------------------------------------------- rotatividade


def janela_rotatividade(ano: int, mes: int) -> tuple[date, date]:
    """Três meses terminando na competência."""
    inicio_mes = mes - 2
    inicio_ano = ano
    if inicio_mes < 1:
        inicio_mes += 12
        inicio_ano -= 1
    return date(inicio_ano, inicio_mes, 1), date(ano, mes, calendar.monthrange(ano, mes)[1])


def calcular_rotatividade(
    posicoes: list[PessoasPosicao],
    profissionais: list[PessoasProfissional],
    inicio: date,
    fim: date,
) -> list[dict]:
    """INS09/INS10: desligados sem justa causa na janela ÷ profissionais alocados no grupo × 100."""
    equipe_da_posicao = {p.id: p.equipe for p in posicoes}
    resultado = []
    for indicador, grupo, equipes in GRUPOS_ROTATIVIDADE:
        do_grupo = [prof for prof in profissionais if equipe_da_posicao.get(prof.posicao_id) in equipes]
        alocados = sum(1 for prof in do_grupo if _ocupa(prof, fim))
        desligados = [
            prof
            for prof in do_grupo
            if prof.data_saida and inicio <= prof.data_saida <= fim and prof.motivo_saida in MOTIVOS_ROTATIVIDADE
        ]
        percentual = 100.0 * len(desligados) / alocados if alocados else 0.0
        resultado.append(
            {
                "indicador": indicador,
                "grupo": grupo,
                "equipes": f"{equipes[0]}–{equipes[-1]}",
                "alocados": alocados,
                "saidas": len(desligados),
                "percentual": percentual,
                "na_meta": percentual <= META_ROTATIVIDADE,
            }
        )
    return resultado


# ---------------------------------------------------------------- alertas


def calcular_alertas(
    posicoes: list[PessoasPosicao],
    profissionais: list[PessoasProfissional],
    ausencias: list[PessoasAusencia],
    certificacoes: list[PessoasCertificacao],
    hoje: date,
    feriados: set[date],
) -> list[dict]:
    alertas = []
    ativos = {p.id for p in profissionais if _ocupa(p, hoje)}

    for posicao in posicoes:
        if posicao.ativo and not any(p.posicao_id == posicao.id and _ocupa(p, hoje) for p in profissionais):
            alertas.append(
                {
                    "nivel": "erro",
                    "tipo": "Posição vaga",
                    "texto": f"Posição {posicao.numero:02d} · {posicao.perfil} (equipe {posicao.equipe}) sem profissional alocado.",
                }
            )

    for cert in certificacoes:
        if cert.profissional_id not in ativos or not cert.data_validade:
            continue
        nome = cert.profissional.nome if cert.profissional else ""
        if cert.data_validade < hoje:
            alertas.append(
                {
                    "nivel": "erro",
                    "tipo": "Certificação vencida (TRS5)",
                    "texto": f"{nome}: {cert.nome} venceu em {cert.data_validade:%d/%m/%Y}.",
                }
            )
        elif cert.data_validade <= hoje + timedelta(days=AVISO_CERTIFICACAO_DIAS):
            alertas.append(
                {
                    "nivel": "aviso",
                    "tipo": "Certificação a vencer",
                    "texto": f"{nome}: {cert.nome} vence em {cert.data_validade:%d/%m/%Y}.",
                }
            )

    for ausencia in ausencias:
        prazo = prazo_comunicacao(ausencia, feriados)
        quem = ausencia.profissional.nome if ausencia.profissional else f"posição {ausencia.posicao.numero:02d} (vaga)"
        regra = "5 dias úteis antes do início" if ausencia.tipo in AUSENCIAS_PLANEJADAS else "1 dia útil após o início"
        if ausencia.comunicado_em is None and hoje >= prazo:
            alertas.append(
                {
                    "nivel": "erro",
                    "tipo": "Substituição não comunicada (TRS13)",
                    "texto": f"{quem}: {ausencia.tipo.lower()} a partir de {ausencia.data_inicio:%d/%m/%Y}; comunicar até {prazo:%d/%m/%Y} ({regra}).",
                }
            )
        elif ausencia.comunicado_em and ausencia.comunicado_em > prazo:
            alertas.append(
                {
                    "nivel": "aviso",
                    "tipo": "Comunicação fora do prazo (TRS13)",
                    "texto": f"{quem}: comunicado em {ausencia.comunicado_em:%d/%m/%Y}, prazo era {prazo:%d/%m/%Y} ({regra}).",
                }
            )
    return alertas


# ---------------------------------------------------------------- painel


def montar_painel(db: Session, competencia: str | None, hoje: date | None = None) -> dict:
    hoje = hoje or date.today()
    ano, mes = parse_competencia(competencia, hoje)
    chave = f"{ano:04d}-{mes:02d}"
    parametro = db.get(PessoasParametroMes, chave)
    feriados = parse_feriados(parametro.feriados if parametro else "")
    todos_feriados = set(feriados)
    for outro in db.query(PessoasParametroMes).all():
        todos_feriados |= parse_feriados(outro.feriados)

    dias = [d for d in dias_uteis_mes(ano, mes, feriados) if d >= INICIO_CONTRATO]
    primeiro, ultimo = date(ano, mes, 1), date(ano, mes, calendar.monthrange(ano, mes)[1])
    posicoes = db.query(PessoasPosicao).filter(PessoasPosicao.ativo.is_(True)).order_by(PessoasPosicao.numero).all()
    profissionais = db.query(PessoasProfissional).all()
    ausencias_mes = (
        db.query(PessoasAusencia)
        .filter(PessoasAusencia.data_inicio <= ultimo, PessoasAusencia.data_fim >= primeiro)
        .all()
    )
    linhas = calcular_posicoes(posicoes, profissionais, ausencias_mes, dias)
    mes_execucao = parametro.mes_execucao if parametro else 1
    leitura = parametro.leitura_pao if parametro and parametro.leitura_pao in LEITURAS_PAO else "C"
    valores = {
        1: float(parametro.valor_item1) if parametro and parametro.valor_item1 is not None else None,
        2: float(parametro.valor_item2) if parametro and parametro.valor_item2 is not None else None,
    }
    itens = calcular_itens(linhas, len(dias), mes_execucao, leitura, valores)
    inicio_rot, fim_rot = janela_rotatividade(ano, mes)
    rotatividade = calcular_rotatividade(posicoes, profissionais, inicio_rot, fim_rot)
    ausencias_abertas = db.query(PessoasAusencia).filter(PessoasAusencia.data_fim >= hoje - timedelta(days=60)).all()
    alertas = calcular_alertas(
        posicoes,
        profissionais,
        ausencias_abertas,
        db.query(PessoasCertificacao).all(),
        hoje,
        todos_feriados,
    )
    return {
        "competencia": chave,
        "parametro": parametro,
        "dias_uteis": len(dias),
        "feriados": sorted(feriados),
        "mes_execucao": mes_execucao,
        "fator": fator_pao(mes_execucao),
        "leitura": leitura,
        "leitura_nome": LEITURAS_PAO[leitura],
        "linhas": linhas,
        "itens": itens,
        "glosa_total": sum(item.glosa or 0 for item in itens) if any(v for v in valores.values()) else None,
        "rotatividade": rotatividade,
        "janela_rotatividade": (inicio_rot, fim_rot),
        "alertas": alertas,
    }
