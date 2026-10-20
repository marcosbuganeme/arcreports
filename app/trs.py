"""Termos de Serviço (TR Tabela 26): catálogo, pontuação, detecção automática e apuração mensal.

A glosa segue o TR 8.29: ajuste % = Σ pontos ÷ 10 (0,5% a cada 5 pontos), teto de 30% (10.7);
durante o PAO os pontos são multiplicados pelo fator do mês (6.28.12 e 8.29.4).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    PessoasAusencia,
    PessoasCertificacao,
    PessoasParametroMes,
    PessoasProfissional,
    TrsOcorrencia,
    TrsTipo,
)
from app.pessoas import (
    INICIO_CONTRATO,
    fator_pao,
    montar_painel,
    parse_feriados,
    prazo_comunicacao,
    somar_dias_uteis,
)
from app.system_config import get_system_config_value


TETO_GLOSA = 30.0
PRAZO_CMDB_DIAS = 60  # TRS29: CMDB integrado ao ITSM em até 60 dias do início da prestação
PRAZO_RELATORIO_DIAS_UTEIS = 5  # TRS17: relatórios consolidados em até 5 dias úteis
CONFIG_CMDB = "trs_cmdb_implantado_em"
CONFIG_RELATORIO = "trs_relatorio_entregue_{competencia}"

STATUS = ["Em aberto", "Contestada", "Confirmada", "Cancelada"]
STATUS_QUE_CONTAM = {"Em aberto", "Contestada", "Confirmada"}
FONTES = ["Controle interno", "Fiscalização (DETRAN)", "Detecção automática"]

# unidade -> (como o TR descreve, rótulo do campo quantidade)
UNIDADES = {
    "ocorrencia": ("Por ocorrência", "Ocorrências"),
    "indicador": ("Por indicador manipulado", "Indicadores"),
    "dia_profissional": ("Por dia, para cada profissional", "Dias"),
    "dia_atraso": ("Por dia de atraso", "Dias de atraso"),
    "dia_interrupcao": ("Por dia de interrupção", "Dias de interrupção"),
    "dia_excedente": ("Por dia incompleto que exceder as 24 horas", "Dias excedentes"),
    "mes_atraso": ("Por mês incompleto de atraso", "Meses de atraso"),
    "mes_progressivo": ("Por mês de atraso, cumulativamente (20, 40, 60…)", "Nº do mês de atraso"),
}

# (código, unidade, pontos, regra, descrição) — TR Tabela 26 (não existe TRS34)
CATALOGO = [
    ("TRS1", "ocorrencia", 5, "reincidencia", "Permitir a presença de profissional sem crachá nos locais onde há prestação de serviço para a CONTRATANTE, após reincidência formalmente notificada."),
    ("TRS2", "ocorrencia", 10, None, "Suspender ou interromper, salvo por motivo de força maior ou caso fortuito, os serviços solicitados."),
    ("TRS3", "ocorrencia", 10, None, "Finalizar a requisição de serviço ou incidente sem que o mesmo tenha sido solucionado, ou deixar de realizar os testes para aferir a efetiva resolução."),
    ("TRS4", "indicador", 100, None, "Fraudar, manipular ou descaracterizar indicadores/metas de níveis de serviço e de desempenho por quaisquer subterfúgios."),
    ("TRS5", "dia_profissional", 5, None, "Manter profissionais sem formação ou sem a qualificação exigida para executar os serviços contratados."),
    ("TRS6", "ocorrencia", 10, None, "Alocar profissional sem capacidade técnica necessária ao pleno atendimento do objeto contratado ou sem atender às qualificações exigidas no contrato, ainda que em casos de substituição temporária."),
    ("TRS7", "ocorrencia", 50, None, "Causar qualquer indisponibilidade dos serviços da CONTRATANTE por motivo de imperícia na execução das atividades contratuais."),
    ("TRS8", "ocorrencia", 50, None, "Causar qualquer dano aos equipamentos da CONTRATANTE por motivo de imperícia na execução das atividades contratuais."),
    ("TRS9", "ocorrencia", 10, None, "Recusar-se a executar serviço relacionado ao objeto do contrato, determinado pela fiscalização, por serviço."),
    ("TRS10", "ocorrencia", 10, None, "Deixar de zelar pela organização, acomodação e correta identificação dos cabos nos racks de equipamentos e patch panels, ou não cuidar da correta montagem e conservação dos equipamentos das unidades de prestação de serviços."),
    ("TRS11", "ocorrencia", 10, None, "Utilizar indevidamente os recursos de TIC (acessos indevidos, utilização para fins particulares, etc.) ou utilizar equipamento particular, salvo em situação excepcional e devidamente autorizado pela CONTRATANTE."),
    ("TRS12", "ocorrencia", 30, None, "Incluir, excluir ou alterar regras dos dispositivos de segurança sem autorização do gestor de TIC, ou contrariando as políticas de segurança da CONTRATANTE."),
    ("TRS13", "ocorrencia", 10, None, "Deixar de comunicar a CONTRATANTE da substituição de profissionais responsáveis pela execução das atividades de suporte à infraestrutura."),
    ("TRS14", "ocorrencia", 10, None, "Deixar de cumprir ou de implantar as Políticas de Segurança e de Continuidade de Negócios de TIC."),
    ("TRS15", "ocorrencia", 20, None, "Permitir que violações de segurança afetem ou causem indisponibilidade dos sistemas da CONTRATANTE, sem aplicar as contramedidas necessárias."),
    ("TRS16", "ocorrencia", 10, None, "Deixar de planejar e instalar nos equipamentos e sistemas as atualizações e patches de segurança disponibilizados pelos fabricantes e distribuidores."),
    ("TRS17", "dia_atraso", 5, None, "Deixar de apresentar os relatórios consolidados para a fiscalização contratual, conforme exigências neste documento, dentro do prazo definido de 5 (cinco) dias úteis."),
    ("TRS18", "ocorrencia", 10, None, "Deixar de apresentar relatórios, levantamentos e inventários no prazo determinado em comum acordo."),
    ("TRS19", "ocorrencia", 10, None, "Deixar de documentar os ICs e de manter completa e atualizada a Base de Dados de Configuração, inclusive no que diz respeito aos diagramas e desenhos."),
    ("TRS20", "ocorrencia", 10, None, "Deixar de produzir ou de manter atualizadas as rotinas e scripts da Base de Dados de Conhecimentos."),
    ("TRS21", "ocorrencia", 10, None, "Deixar de notificar incidentes repetitivos, quer tenham sido conhecidos através do monitoramento ou por notificações de usuários, para a CONTRATANTE."),
    ("TRS22", "ocorrencia", 5, None, "Deixar de analisar a viabilidade e o impacto da instalação de novas soluções e correções."),
    ("TRS23", "ocorrencia", 5, None, "Deixar de aplicar as políticas de controle de acesso e de gestão da identidade de usuários de TIC."),
    ("TRS24", "ocorrencia", 20, None, "Deixar de operar e monitorar proativamente o ambiente de TIC, e de atuar tempestivamente no caso de incidentes graves."),
    ("TRS25", "ocorrencia", 5, None, "Deixar de realizar os testes e análises de vulnerabilidades e potenciais falhas de segurança, conforme política de segurança da informação."),
    ("TRS26", "ocorrencia", 10, None, "Deixar de realizar a avaliação de impacto, criação de cronograma, monitoramento e controle do processo de mudança, ou realizá-los de forma deficiente ou incompleta, quando se tratar de Requisições Planejadas."),
    ("TRS27", "ocorrencia", 10, None, "Deixar de comunicar a realização de Requisições Planejadas que poderão gerar indisponibilidade em sistemas ou serviços."),
    ("TRS28", "ocorrencia", 10, None, "Deixar de executar as Requisições Planejadas no Tempo Máximo para Solução definido pela CONTRATANTE na Demanda de Serviço ou outra metodologia definida pela CONTRATANTE."),
    ("TRS29", "mes_progressivo", 20, None, "Deixar de implementar o CMDB de todos os Itens de Configuração (ICs) descritos no documento, integrada à ferramenta ITSM no prazo máximo de 60 (sessenta) dias após o início da prestação do serviço. Glosa progressiva por mês adicional de atraso (20, 40, 60 pontos…)."),
    ("TRS30", "ocorrencia", 5, None, "Deixar de realizar a atualização no CMDB dos ICs modificados ou incluídos, bem como atualizar a relação entre os ICs, caso a Requisição Planejada venha a afetá-los."),
    ("TRS31", "ocorrencia", 5, None, "Deixar de participar de reunião solicitada e previamente agendada com a equipe de gestão de TIC da CONTRATANTE."),
    ("TRS32", "dia_excedente", 10, None, "Deixar de retirar profissional que se conduza de modo inconveniente, que não respeite as normas da CONTRATANTE ou que não atenda às necessidades, em no máximo 24 (vinte e quatro) horas após a notificação formal."),
    ("TRS33", "ocorrencia", 5, None, "Deixar de zelar pelas máquinas, equipamentos e instalações da CONTRATANTE utilizados pela CONTRATADA."),
    ("TRS35", "mes_atraso", 100, None, "Deixar de adaptar durante o PAO a Ferramenta de Requisição e Gerenciamento de Serviços de TIC (ITSM), e a Ferramenta de Suporte Remoto com todas as características e requisitos necessários."),
    ("TRS36", "mes_atraso", 10, None, "Deixar de apresentar no prazo definido neste documento as comprovações das capacidades técnicas dos colaboradores da CONTRATADA."),
    ("TRS37", "ocorrencia", 10, None, "Deixar de documentar todas as ocorrências (incidentes, requisições, mudanças, problemas, indisponibilidades) na Ferramenta de Requisição e Gerenciamento de Serviços de TIC (ITSM)."),
    ("TRS38", "ocorrencia", 5, None, "Deixar de apresentar à CONTRATANTE o impacto e o cronograma da solução do Problema no Tempo Máximo para Solução do Incidente do respectivo Incidente que deu origem ao Problema."),
    ("TRS39", "ocorrencia", 5, None, "Deixar de executar a solução de um Problema conforme cronograma definido pela CONTRATANTE."),
    ("TRS40", "dia_interrupcao", 100, None, "Interromper unilateralmente a prestação de serviços sem que haja evento de força maior que o justifique."),
    ("TRS41", "ocorrencia", 200, None, "Perder dados ou informações corporativas por erros na operação devidamente comprovados."),
    ("TRS42", "ocorrencia", 50, None, "Deixar de zelar pela segurança orgânica das instalações da CONTRATANTE, fornecendo crachá ou credenciais de acesso a pessoas não autorizadas, ou ainda não verificando o correto fechamento das portas das áreas restritas."),
    ("TRS43", "ocorrencia", 5, None, "Realizar cancelamento de chamado na Ferramenta de Requisição e Gerenciamento de Serviços de TIC (ITSM) sem justificativa aceitável pela CONTRATANTE."),
    ("TRS44", "ocorrencia", 5, None, "Deixar de designar profissionais técnicos para cada equipe especializada, que serão os pontos focais para alinhamento técnico sobre demandas operacionais e projetos com a CONTRATANTE."),
    ("TRS45", "ocorrencia", 5, None, "Deixar de atender no triplo do tempo os chamados que não foram solucionados no TMSI, no TMSR e no TMSP, de acordo com a tabela de Tempo de Início e Tempo Máximo por Prioridade de Atendimento."),
    ("TRS46", "ocorrencia", 50, None, "Meta do INS11 ficar abaixo de 80% por 3 meses consecutivos."),
    ("TRS47", "ocorrencia", 5, None, "Deixar de apresentar o perfil profissional adequado no prazo estabelecido para o atendimento de projetos e demandas de implementação e melhorias dos serviços de TIC."),
    ("TRS48", "ocorrencia", 10, None, "Deixar de apresentar ou substituir perfil profissional conforme prazos estabelecidos neste TR."),
    ("TRS49", "ocorrencia", 10, "reincidencia", "Deixar de cumprir qualquer outra obrigação estabelecida no edital e não prevista nesta tabela, de forma reincidente, após formalmente notificada pela CONTRATANTE."),
]

# TRS que o módulo consegue detectar sozinho (aba Sugestões)
AUTOMATICOS = {"TRS5", "TRS13", "TRS17", "TRS29", "TRS45", "TRS46"}


def ensure_trs_tipos(db: Session) -> None:
    """Cria o catálogo da Tabela 26 na primeira execução (pontos podem ser revistos depois, TR 8.28.2.6)."""
    existentes = {codigo for (codigo,) in db.query(TrsTipo.codigo).all()}
    novos = False
    for ordem, (codigo, unidade, pontos, regra, descricao) in enumerate(CATALOGO, start=1):
        if codigo not in existentes:
            db.add(TrsTipo(codigo=codigo, ordem=ordem, descricao=descricao, unidade=unidade, pontos=pontos, regra=regra))
            novos = True
    if novos:
        db.commit()


# ---------------------------------------------------------------- pontuação


def competencia_de(dia: date) -> str:
    return f"{dia.year:04d}-{dia.month:02d}"


def limites_competencia(competencia: str) -> tuple[date, date]:
    ano, mes = (int(p) for p in competencia.split("-"))
    return date(ano, mes, 1), date(ano, mes, calendar.monthrange(ano, mes)[1])


def mes_execucao(db: Session, competencia: str) -> int:
    parametro = db.get(PessoasParametroMes, competencia)
    if parametro:
        return parametro.mes_execucao
    ano, mes = (int(p) for p in competencia.split("-"))
    return max(1, (ano - INICIO_CONTRATO.year) * 12 + mes - INICIO_CONTRATO.month + 1)


def pontos_brutos(ocorrencia: TrsOcorrencia, tipo: TrsTipo) -> int:
    if ocorrencia.natureza == "notificacao" or ocorrencia.status not in STATUS_QUE_CONTAM:
        return 0
    return tipo.pontos * max(1, ocorrencia.quantidade)


def notificacao_previa(db: Session, codigo: str, ate: date, ignorar_id: int | None = None) -> TrsOcorrencia | None:
    query = db.query(TrsOcorrencia).filter(
        TrsOcorrencia.codigo == codigo,
        TrsOcorrencia.natureza == "notificacao",
        TrsOcorrencia.status != "Cancelada",
        TrsOcorrencia.data_ocorrencia <= ate,
    )
    if ignorar_id:
        query = query.filter(TrsOcorrencia.id != ignorar_id)
    return query.order_by(TrsOcorrencia.data_ocorrencia.desc()).first()


# ---------------------------------------------------------------- detecção automática


def _feriados(db: Session) -> set[date]:
    feriados: set[date] = set()
    for parametro in db.query(PessoasParametroMes).all():
        feriados |= parse_feriados(parametro.feriados)
    return feriados


def _sugestao(codigo: str, chave: str, dia: date, quantidade: int, evidencia: str, profissional_id: int | None = None) -> dict:
    return {
        "codigo": codigo,
        "chave": chave,
        "data": dia,
        "quantidade": quantidade,
        "evidencia": evidencia[:500],
        "profissional_id": profissional_id,
    }


def detectar_trs5(db: Session, competencia: str, hoje: date) -> list[dict]:
    """Certificação vencida de profissional ativo: 5 pontos por dia (corrido) e por profissional."""
    inicio, fim = limites_competencia(competencia)
    fim = min(fim, hoje)
    sugestoes = []
    for cert in db.query(PessoasCertificacao).all():
        prof = cert.profissional
        if not cert.data_validade or not prof:
            continue
        de = max(inicio, cert.data_validade + timedelta(days=1), prof.data_entrada, INICIO_CONTRATO)
        ate = min(fim, prof.data_saida) if prof.data_saida else fim
        dias = (ate - de).days + 1
        if dias > 0:
            sugestoes.append(_sugestao(
                "TRS5", f"TRS5:cert{cert.id}:{competencia}", de, dias,
                f"{prof.nome}: certificação {cert.nome} vencida em {cert.data_validade:%d/%m/%Y} "
                f"({dias} dia(s) em {competencia}).",
                prof.id,
            ))
    return sugestoes


def detectar_trs13(db: Session, competencia: str, hoje: date) -> list[dict]:
    """Substituição não comunicada (ou comunicada fora do prazo) — TR 6.32.3."""
    inicio, fim = limites_competencia(competencia)
    feriados = _feriados(db)
    sugestoes = []
    ausencias = db.query(PessoasAusencia).filter(PessoasAusencia.data_inicio >= inicio, PessoasAusencia.data_inicio <= fim)
    for ausencia in ausencias.all():
        prazo = prazo_comunicacao(ausencia, feriados)
        quem = ausencia.profissional.nome if ausencia.profissional else f"posição {ausencia.posicao.numero:02d} (vaga)"
        if ausencia.comunicado_em is None and hoje > prazo:
            texto = f"{quem}: {ausencia.tipo.lower()} a partir de {ausencia.data_inicio:%d/%m/%Y} sem comunicação ao DETRAN (prazo {prazo:%d/%m/%Y})."
        elif ausencia.comunicado_em and ausencia.comunicado_em > prazo:
            texto = f"{quem}: {ausencia.tipo.lower()} a partir de {ausencia.data_inicio:%d/%m/%Y} comunicada em {ausencia.comunicado_em:%d/%m/%Y} (prazo {prazo:%d/%m/%Y})."
        else:
            continue
        sugestoes.append(_sugestao("TRS13", f"TRS13:aus{ausencia.id}", min(max(prazo, inicio), fim), 1, texto, ausencia.profissional_id))
    return sugestoes


def ins11_geral(db: Session, competencia: str) -> float | None:
    """INS11 do contrato (equipes 1–3 e 5–12) na competência; None se não houver medição."""
    painel = montar_painel(db, competencia)
    itens = [item for item in painel["itens"] if item.n and item.d]
    if not itens or not db.query(PessoasProfissional.id).first():
        return None
    total = sum(item.n * item.d for item in itens)
    soma_di = sum(item.soma_di for item in itens)
    return 100.0 * (1 - soma_di / total) if total else None


def _competencia_anterior(competencia: str, meses: int = 1) -> str:
    ano, mes = (int(p) for p in competencia.split("-"))
    total = ano * 12 + (mes - 1) - meses
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def detectar_trs46(db: Session, competencia: str, hoje: date) -> list[dict]:
    """INS11 abaixo de 80% por 3 meses consecutivos (meses já encerrados)."""
    _, fim = limites_competencia(competencia)
    if hoje <= fim:
        return []
    meses = [_competencia_anterior(competencia, k) for k in (2, 1, 0)]
    valores = []
    for mes in meses:
        if limites_competencia(mes)[1] < INICIO_CONTRATO:
            return []
        valor = ins11_geral(db, mes)
        if valor is None or valor >= 80:
            return []
        valores.append(valor)
    texto = "INS11 abaixo de 80% em " + ", ".join(f"{m} ({v:.2f}%)".replace(".", ",") for m, v in zip(meses, valores)) + "."
    return [_sugestao("TRS46", f"TRS46:{competencia}", fim, 1, texto)]


def detectar_trs29(db: Session, competencia: str, hoje: date) -> list[dict]:
    """CMDB não implantado em até 60 dias do início: 20 pontos × nº do mês de atraso."""
    prazo = INICIO_CONTRATO + timedelta(days=PRAZO_CMDB_DIAS)
    implantado = get_system_config_value(db, CONFIG_CMDB)
    implantado_em = date.fromisoformat(implantado) if implantado else None
    inicio, fim = limites_competencia(competencia)
    limite = min(fim, hoje, implantado_em - timedelta(days=1)) if implantado_em else min(fim, hoje)
    if limite <= prazo or limite < inicio:
        return []
    mes_atraso = (limite - prazo - timedelta(days=1)).days // 30 + 1
    situacao = f"implantado em {implantado_em:%d/%m/%Y}" if implantado_em else "ainda não informado como implantado"
    return [_sugestao(
        "TRS29", f"TRS29:{competencia}", max(inicio, prazo + timedelta(days=1)), mes_atraso,
        f"CMDB integrado ao ITSM: prazo {prazo:%d/%m/%Y}, {situacao}. {mes_atraso}º mês de atraso em {competencia}.",
    )]


def prazo_relatorio(competencia_relatorio: str, feriados: set[date]) -> date:
    """5º dia útil do mês seguinte à competência do relatório."""
    _, fim = limites_competencia(competencia_relatorio)
    return somar_dias_uteis(fim, PRAZO_RELATORIO_DIAS_UTEIS, feriados)


def detectar_trs17(db: Session, competencia: str, hoje: date) -> list[dict]:
    """Relatório consolidado do mês anterior entregue (ou ainda não) após o 5º dia útil desta competência."""
    relatorio = _competencia_anterior(competencia)
    if limites_competencia(relatorio)[1] < INICIO_CONTRATO:
        return []
    prazo = prazo_relatorio(relatorio, _feriados(db))
    entregue = get_system_config_value(db, CONFIG_RELATORIO.format(competencia=relatorio))
    entregue_em = date.fromisoformat(entregue) if entregue else None
    referencia = entregue_em or min(hoje, limites_competencia(competencia)[1])
    atraso = (referencia - prazo).days
    if atraso <= 0:
        return []
    situacao = f"entregue em {entregue_em:%d/%m/%Y}" if entregue_em else f"não entregue até {referencia:%d/%m/%Y} (contagem parcial)"
    return [_sugestao(
        "TRS17", f"TRS17:{relatorio}", prazo + timedelta(days=1), atraso,
        f"Relatório Geral de Faturamento de {relatorio}: prazo {prazo:%d/%m/%Y}, {situacao}; {atraso} dia(s) de atraso.",
    )]


SQL_TRS45 = text(
    """
    SELECT x.id, x.titulo, x.abertura, x.solucao, x.limite3
    FROM (
      SELECT t.id, t.name AS titulo, t.date AS abertura, t.solvedate AS solucao,
        DATE_ADD(t.date, INTERVAL 3 * TIMESTAMPDIFF(MINUTE, t.date, t.time_to_resolve) MINUTE) AS limite3
      FROM glpi.glpi_tickets t
      WHERE (t.is_deleted IS NULL OR t.is_deleted + 0 = 0)
        AND t.time_to_resolve IS NOT NULL AND t.time_to_resolve <> ''
        AND t.date >= :inicio_contrato
    ) x
    WHERE x.limite3 >= :inicio AND x.limite3 < :fim AND x.limite3 <= :agora
      AND (x.solucao IS NULL OR x.solucao = '' OR x.solucao > x.limite3)
    ORDER BY x.limite3
    LIMIT 3000
    """
)


def consultar_glpi_trs45(db: Session, inicio: date, fim: date, agora: datetime) -> list[dict]:
    rows = db.execute(SQL_TRS45, {
        "inicio_contrato": INICIO_CONTRATO.isoformat(),
        "inicio": inicio.isoformat(),
        "fim": (fim + timedelta(days=1)).isoformat(),
        "agora": agora.strftime("%Y-%m-%d %H:%M:%S"),
    }).mappings().all()
    return [dict(row) for row in rows]


def _para_data(valor) -> date:
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor)[:10])


def detectar_trs45(db: Session, competencia: str, hoje: date, agora: datetime | None = None) -> tuple[list[dict], str | None]:
    """Chamados não solucionados no triplo do prazo de solução do GLPI (TMSI/TMSR)."""
    inicio, fim = limites_competencia(competencia)
    try:
        rows = consultar_glpi_trs45(db, inicio, fim, agora or datetime.now())
    except Exception as exc:  # réplica do GLPI indisponível ou sem permissão
        db.rollback()
        return [], f"Não foi possível consultar o GLPI: {str(exc)[:200]}"
    sugestoes = []
    for row in rows:
        situacao = f"solucionado em {str(row['solucao'])[:16]}" if row.get("solucao") else "ainda não solucionado"
        sugestoes.append(_sugestao(
            "TRS45", f"TRS45:t{row['id']}", _para_data(row["limite3"]), 1,
            f"Chamado {row['id']} ({str(row.get('titulo') or '')[:80]}): aberto em {str(row['abertura'])[:16]}, "
            f"triplo do prazo em {str(row['limite3'])[:16]}, {situacao}.",
        ))
    return sugestoes, None


def sugestoes_competencia(db: Session, competencia: str, hoje: date | None = None) -> tuple[list[dict], list[str]]:
    """Ocorrências detectadas automaticamente que ainda não foram registradas."""
    hoje = hoje or date.today()
    avisos: list[str] = []
    todas = []
    for detector in (detectar_trs5, detectar_trs13, detectar_trs17, detectar_trs29, detectar_trs46):
        todas.extend(detector(db, competencia, hoje))
    trs45, erro = detectar_trs45(db, competencia, hoje)
    todas.extend(trs45)
    if erro:
        avisos.append(erro)
    registradas = {chave for (chave,) in db.query(TrsOcorrencia.chave).filter(TrsOcorrencia.chave.isnot(None)).all()}
    return [s for s in todas if s["chave"] not in registradas], avisos


# ---------------------------------------------------------------- apuração


def pontos_ins(db: Session, competencia: str) -> tuple[list[dict], str | None]:
    """Pontos dos indicadores (INS) já apurados pelo relatório 'NMS TR - Apuração mensal por indicador'."""
    try:
        rows = db.execute(
            text(
                "SELECT indicador, valor, meta, sentido, pontos, fator_pao, pontos_ponderados, situacao "
                "FROM nms_tr_apuracao WHERE mes = :mes ORDER BY grupo, indicador"
            ),
            {"mes": competencia},
        ).mappings().all()
    except Exception:
        db.rollback()
        return [], "A apuração dos indicadores (relatório nms_tr_apuracao) não está disponível."
    return [dict(row) for row in rows], None


def _num(valor) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def montar_apuracao(db: Session, competencia: str) -> dict:
    fator = fator_pao(mes_execucao(db, competencia))
    tipos = {t.codigo: t for t in db.query(TrsTipo).all()}
    ocorrencias = (
        db.query(TrsOcorrencia)
        .filter(TrsOcorrencia.competencia == competencia)
        .order_by(TrsOcorrencia.data_ocorrencia, TrsOcorrencia.id)
        .all()
    )
    por_codigo: dict[str, dict] = {}
    for oc in ocorrencias:
        tipo = tipos[oc.codigo]
        linha = por_codigo.setdefault(oc.codigo, {
            "codigo": oc.codigo, "tipo": tipo, "registros": 0, "quantidade": 0, "pontos": 0,
            "abertos": 0, "contestados": 0, "confirmados": 0, "cancelados": 0, "notificacoes": 0,
        })
        linha["registros"] += 1
        if oc.natureza == "notificacao":
            linha["notificacoes"] += 1
            continue
        chave = {"Em aberto": "abertos", "Contestada": "contestados", "Confirmada": "confirmados", "Cancelada": "cancelados"}[oc.status]
        linha[chave] += 1
        pts = pontos_brutos(oc, tipo)
        if pts:
            linha["quantidade"] += oc.quantidade
            linha["pontos"] += pts
    linhas_trs = sorted(por_codigo.values(), key=lambda l: l["tipo"].ordem)
    for linha in linhas_trs:
        linha["ponderados"] = linha["pontos"] * fator
    trs_brutos = sum(l["pontos"] for l in linhas_trs)
    trs_ponderados = trs_brutos * fator
    trs_contestados = sum(
        pontos_brutos(oc, tipos[oc.codigo]) for oc in ocorrencias if oc.status == "Contestada"
    ) * fator

    linhas_ins, aviso_ins = pontos_ins(db, competencia)
    ins_ponderados = sum(_num(l["pontos_ponderados"]) for l in linhas_ins)

    total = ins_ponderados + trs_ponderados
    ajuste = total / 10
    glosa_pct = min(TETO_GLOSA, ajuste)
    parametro = db.get(PessoasParametroMes, competencia)
    valor_mensal = None
    if parametro and (parametro.valor_item1 is not None or parametro.valor_item2 is not None):
        valor_mensal = float(parametro.valor_item1 or 0) + float(parametro.valor_item2 or 0)
    return {
        "competencia": competencia,
        "mes_execucao": mes_execucao(db, competencia),
        "fator": fator,
        "linhas_trs": linhas_trs,
        "trs_brutos": trs_brutos,
        "trs_ponderados": trs_ponderados,
        "trs_contestados": trs_contestados,
        "linhas_ins": linhas_ins,
        "aviso_ins": aviso_ins,
        "ins_ponderados": ins_ponderados,
        "total_ponderado": total,
        "ajuste_pct": ajuste,
        "glosa_pct": glosa_pct,
        "no_teto": ajuste > TETO_GLOSA,
        "valor_mensal": valor_mensal,
        "glosa_valor": valor_mensal * glosa_pct / 100 if valor_mensal else None,
        "trs_sem_teto_valor": valor_mensal * min(TETO_GLOSA, trs_ponderados / 10) / 100 if valor_mensal else None,
    }
