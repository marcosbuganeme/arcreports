from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    AdminActionLog,
    PessoasAusencia,
    PessoasCertificacao,
    PessoasParametroMes,
    PessoasPosicao,
    PessoasProfissional,
)
from app.pessoas import (
    LEITURAS_PAO,
    MOTIVOS_SAIDA,
    TIPOS_AUSENCIA,
    dias_uteis_mes,
    ensure_pessoas_posicoes,
    montar_painel,
    parse_competencia,
    parse_feriados,
)
from app.routes.common import form_data, render
from app.security import require_admin, verify_csrf


router = APIRouter()


def pessoas_redirect(path: str, message: str, error: bool = False) -> RedirectResponse:
    key = "error" if error else "message"
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{separator}{key}={quote(message)}", status_code=status.HTTP_303_SEE_OTHER)


def log_pessoas_action(db: Session, user, action: str, table_name: str, message: str) -> None:
    db.add(
        AdminActionLog(
            user_id=user.id,
            username=user.username,
            action=action,
            table_name=table_name,
            status="success",
            message=message,
            ip_address=None,
        )
    )
    db.commit()


def parse_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_money(value: str | None) -> float | None:
    value = (value or "").strip().replace("R$", "").replace(" ", "")
    if not value:
        return None
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    try:
        return round(float(value), 2)
    except ValueError:
        return None


def page_context(request: Request, active_tab: str, **extra) -> dict:
    return {
        "active": "pessoas",
        "active_tab": active_tab,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
        **extra,
    }


def posicao_ocupada(db: Session, posicao_id: int, entrada: date, saida: date | None, ignorar_id: int | None = None):
    """Outro profissional na mesma posição com período sobreposto."""
    query = db.query(PessoasProfissional).filter(PessoasProfissional.posicao_id == posicao_id)
    if ignorar_id:
        query = query.filter(PessoasProfissional.id != ignorar_id)
    for outro in query.all():
        outro_fim = outro.data_saida or date.max
        fim = saida or date.max
        if outro.data_entrada <= fim and entrada <= outro_fim:
            return outro
    return None


# ---------------------------------------------------------------- painel


@router.get("/pessoas", response_class=HTMLResponse)
def pessoas_painel(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ensure_pessoas_posicoes(db)
    painel = montar_painel(db, request.query_params.get("competencia"))
    return render(request, "pessoas_painel.html", page_context(request, "painel", **painel), db)


# ---------------------------------------------------------------- posições


@router.get("/pessoas/posicoes", response_class=HTMLResponse)
def pessoas_posicoes(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ensure_pessoas_posicoes(db)
    posicoes = db.query(PessoasPosicao).order_by(PessoasPosicao.numero).all()
    return render(request, "pessoas_posicoes.html", page_context(request, "posicoes", posicoes=posicoes), db)


@router.post("/pessoas/posicoes/{posicao_id}", dependencies=[Depends(verify_csrf)])
async def pessoas_posicao_update(posicao_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    posicao = db.get(PessoasPosicao, posicao_id)
    if not posicao:
        return pessoas_redirect("/pessoas/posicoes", "Posição não encontrada.", True)
    data = await form_data(request)
    try:
        horas = float((data.get("horas_dia") or "").replace(",", "."))
    except ValueError:
        return pessoas_redirect("/pessoas/posicoes", "Horas por dia inválidas.", True)
    if not 0 < horas <= 24:
        return pessoas_redirect("/pessoas/posicoes", "Horas por dia devem ficar entre 0 e 24.", True)
    posicao.modalidade = (data.get("modalidade") or "").strip()[:80] or None
    posicao.horas_dia = horas
    posicao.ativo = data.get("ativo") == "on"
    db.commit()
    log_pessoas_action(db, user, "pessoas_posicao_update", "pessoas_posicoes", f"Posição {posicao.numero} atualizada.")
    return pessoas_redirect("/pessoas/posicoes", f"Posição {posicao.numero:02d} atualizada.")


# ---------------------------------------------------------------- profissionais


@router.get("/pessoas/profissionais", response_class=HTMLResponse)
def pessoas_profissionais(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ensure_pessoas_posicoes(db)
    mostrar = request.query_params.get("mostrar", "ativos")
    query = db.query(PessoasProfissional).join(PessoasProfissional.posicao)
    if mostrar != "todos":
        query = query.filter(
            (PessoasProfissional.data_saida.is_(None)) | (PessoasProfissional.data_saida >= date.today())
        )
    profissionais = query.order_by(PessoasPosicao.numero, PessoasProfissional.data_entrada).all()
    posicoes = db.query(PessoasPosicao).order_by(PessoasPosicao.numero).all()
    editar_id = request.query_params.get("editar")
    return render(
        request,
        "pessoas_profissionais.html",
        page_context(
            request,
            "profissionais",
            profissionais=profissionais,
            posicoes=posicoes,
            motivos=MOTIVOS_SAIDA,
            mostrar=mostrar,
            editar_id=int(editar_id) if editar_id and editar_id.isdigit() else None,
            hoje=date.today(),
        ),
        db,
    )


def validar_profissional(db: Session, data: dict, ignorar_id: int | None = None):
    nome = (data.get("nome") or "").strip()[:160]
    posicao = db.get(PessoasPosicao, int(data["posicao_id"])) if (data.get("posicao_id") or "").isdigit() else None
    entrada = parse_date(data.get("data_entrada"))
    saida = parse_date(data.get("data_saida"))
    motivo = (data.get("motivo_saida") or "").strip() or None
    if not nome or not posicao or not entrada:
        return None, "Informe nome, posição e data de entrada."
    if saida and saida < entrada:
        return None, "A data de saída não pode ser anterior à entrada."
    if saida and motivo not in MOTIVOS_SAIDA:
        return None, "Informe o motivo da saída."
    outro = posicao_ocupada(db, posicao.id, entrada, saida, ignorar_id)
    if outro:
        return None, f"A posição {posicao.numero:02d} já está ocupada por {outro.nome} nesse período. Registre a saída dele antes."
    return {
        "nome": nome,
        "posicao_id": posicao.id,
        "data_entrada": entrada,
        "data_saida": saida,
        "motivo_saida": motivo if saida else None,
    }, None


@router.post("/pessoas/profissionais", dependencies=[Depends(verify_csrf)])
async def pessoas_profissional_create(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    valores, erro = validar_profissional(db, await form_data(request))
    if erro:
        return pessoas_redirect("/pessoas/profissionais", erro, True)
    profissional = PessoasProfissional(**valores)
    db.add(profissional)
    db.commit()
    log_pessoas_action(db, user, "pessoas_profissional_create", "pessoas_profissionais", f"Profissional {profissional.id} cadastrado.")
    return pessoas_redirect("/pessoas/profissionais", "Profissional cadastrado.")


@router.post("/pessoas/profissionais/{profissional_id}", dependencies=[Depends(verify_csrf)])
async def pessoas_profissional_update(profissional_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    profissional = db.get(PessoasProfissional, profissional_id)
    if not profissional:
        return pessoas_redirect("/pessoas/profissionais", "Profissional não encontrado.", True)
    valores, erro = validar_profissional(db, await form_data(request), profissional.id)
    if erro:
        return pessoas_redirect(f"/pessoas/profissionais?editar={profissional.id}", erro, True)
    for campo, valor in valores.items():
        setattr(profissional, campo, valor)
    db.commit()
    log_pessoas_action(db, user, "pessoas_profissional_update", "pessoas_profissionais", f"Profissional {profissional.id} atualizado.")
    return pessoas_redirect("/pessoas/profissionais", "Profissional atualizado.")


@router.post("/pessoas/profissionais/{profissional_id}/delete", dependencies=[Depends(verify_csrf)])
def pessoas_profissional_delete(profissional_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    profissional = db.get(PessoasProfissional, profissional_id)
    if not profissional:
        return pessoas_redirect("/pessoas/profissionais", "Profissional não encontrado.", True)
    db.delete(profissional)
    db.commit()
    log_pessoas_action(
        db, user, "pessoas_profissional_delete", "pessoas_profissionais",
        f"Profissional {profissional_id} excluído com ausências e certificações.",
    )
    return pessoas_redirect("/pessoas/profissionais?mostrar=todos", "Profissional excluído.")


@router.post("/pessoas/profissionais/{profissional_id}/certificacoes", dependencies=[Depends(verify_csrf)])
async def pessoas_certificacao_create(profissional_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    profissional = db.get(PessoasProfissional, profissional_id)
    if not profissional:
        return pessoas_redirect("/pessoas/profissionais", "Profissional não encontrado.", True)
    data = await form_data(request)
    nome = (data.get("nome") or "").strip()[:160]
    if not nome:
        return pessoas_redirect(f"/pessoas/profissionais?editar={profissional.id}", "Informe a certificação.", True)
    db.add(PessoasCertificacao(profissional_id=profissional.id, nome=nome, data_validade=parse_date(data.get("data_validade"))))
    db.commit()
    log_pessoas_action(db, user, "pessoas_certificacao_create", "pessoas_certificacoes", f"Certificação do profissional {profissional.id} cadastrada.")
    return pessoas_redirect(f"/pessoas/profissionais?editar={profissional.id}", "Certificação cadastrada.")


@router.post("/pessoas/certificacoes/{certificacao_id}/delete", dependencies=[Depends(verify_csrf)])
def pessoas_certificacao_delete(certificacao_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    certificacao = db.get(PessoasCertificacao, certificacao_id)
    if not certificacao:
        return pessoas_redirect("/pessoas/profissionais", "Certificação não encontrada.", True)
    profissional_id = certificacao.profissional_id
    db.delete(certificacao)
    db.commit()
    log_pessoas_action(db, user, "pessoas_certificacao_delete", "pessoas_certificacoes", f"Certificação {certificacao_id} excluída.")
    return pessoas_redirect(f"/pessoas/profissionais?editar={profissional_id}", "Certificação excluída.")


# ---------------------------------------------------------------- ausências


@router.get("/pessoas/ausencias", response_class=HTMLResponse)
def pessoas_ausencias(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ensure_pessoas_posicoes(db)
    ano, mes = parse_competencia(request.query_params.get("competencia"))
    primeiro = date(ano, mes, 1)
    ultimo = date(ano + (mes == 12), mes % 12 + 1, 1)
    ausencias = (
        db.query(PessoasAusencia)
        .join(PessoasAusencia.posicao)
        .filter(PessoasAusencia.data_inicio < ultimo, PessoasAusencia.data_fim >= primeiro)
        .order_by(PessoasAusencia.data_inicio, PessoasPosicao.numero)
        .all()
    )
    posicoes = db.query(PessoasPosicao).order_by(PessoasPosicao.numero).all()
    profissionais = (
        db.query(PessoasProfissional)
        .filter((PessoasProfissional.data_saida.is_(None)) | (PessoasProfissional.data_saida >= primeiro))
        .order_by(PessoasProfissional.nome)
        .all()
    )
    return render(
        request,
        "pessoas_ausencias.html",
        page_context(
            request,
            "ausencias",
            competencia=f"{ano:04d}-{mes:02d}",
            ausencias=ausencias,
            posicoes=posicoes,
            profissionais=profissionais,
            tipos=TIPOS_AUSENCIA,
        ),
        db,
    )


@router.post("/pessoas/ausencias", dependencies=[Depends(verify_csrf)])
async def pessoas_ausencia_create(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    inicio = parse_date(data.get("data_inicio"))
    fim = parse_date(data.get("data_fim")) or inicio
    destino = f"/pessoas/ausencias?competencia={inicio:%Y-%m}" if inicio else "/pessoas/ausencias"
    profissional = (
        db.get(PessoasProfissional, int(data["profissional_id"]))
        if (data.get("profissional_id") or "").isdigit()
        else None
    )
    posicao = (
        profissional.posicao
        if profissional
        else db.get(PessoasPosicao, int(data["posicao_id"])) if (data.get("posicao_id") or "").isdigit() else None
    )
    tipo = (data.get("tipo") or "").strip()
    if not posicao or not inicio or tipo not in TIPOS_AUSENCIA:
        return pessoas_redirect(destino, "Informe o profissional (ou a posição vaga), o tipo e o período.", True)
    if fim < inicio:
        return pessoas_redirect(destino, "A data final não pode ser anterior à inicial.", True)
    substituto = (data.get("substituto_nome") or "").strip()[:160] or None
    ausencia = PessoasAusencia(
        posicao_id=posicao.id,
        profissional_id=profissional.id if profissional else None,
        tipo=tipo,
        data_inicio=inicio,
        data_fim=fim,
        substituto_nome=substituto,
        substituto_mesmo_perfil=bool(substituto) and data.get("substituto_mesmo_perfil") == "on",
        substituto_qualificado=bool(substituto) and data.get("substituto_qualificado") == "on",
        comunicado_em=parse_date(data.get("comunicado_em")),
        observacao=(data.get("observacao") or "").strip()[:255] or None,
    )
    db.add(ausencia)
    db.commit()
    log_pessoas_action(db, user, "pessoas_ausencia_create", "pessoas_ausencias", f"Ausência {ausencia.id} na posição {posicao.numero} cadastrada.")
    return pessoas_redirect(destino, "Ausência registrada.")


@router.post("/pessoas/ausencias/{ausencia_id}/delete", dependencies=[Depends(verify_csrf)])
def pessoas_ausencia_delete(ausencia_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ausencia = db.get(PessoasAusencia, ausencia_id)
    if not ausencia:
        return pessoas_redirect("/pessoas/ausencias", "Ausência não encontrada.", True)
    destino = f"/pessoas/ausencias?competencia={ausencia.data_inicio:%Y-%m}"
    db.delete(ausencia)
    db.commit()
    log_pessoas_action(db, user, "pessoas_ausencia_delete", "pessoas_ausencias", f"Ausência {ausencia_id} excluída.")
    return pessoas_redirect(destino, "Ausência excluída.")


# ---------------------------------------------------------------- parâmetros do mês


@router.get("/pessoas/parametros", response_class=HTMLResponse)
def pessoas_parametros(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ano, mes = parse_competencia(request.query_params.get("competencia"))
    competencia = f"{ano:04d}-{mes:02d}"
    parametro = db.get(PessoasParametroMes, competencia)
    feriados = parse_feriados(parametro.feriados if parametro else "")
    return render(
        request,
        "pessoas_parametros.html",
        page_context(
            request,
            "parametros",
            competencia=competencia,
            parametro=parametro,
            dias_uteis=len(dias_uteis_mes(ano, mes, feriados)),
            leituras=LEITURAS_PAO,
            historico=db.query(PessoasParametroMes).order_by(PessoasParametroMes.competencia.desc()).all(),
        ),
        db,
    )


@router.post("/pessoas/parametros", dependencies=[Depends(verify_csrf)])
async def pessoas_parametros_save(request: Request, db: Session = Depends(get_db)):
    user = require_admin(request, db)
    if isinstance(user, RedirectResponse):
        return user
    data = await form_data(request)
    ano, mes = parse_competencia(data.get("competencia"))
    competencia = f"{ano:04d}-{mes:02d}"
    destino = f"/pessoas/parametros?competencia={competencia}"
    try:
        mes_execucao = int(data.get("mes_execucao") or 1)
    except ValueError:
        return pessoas_redirect(destino, "Mês de execução inválido.", True)
    if not 1 <= mes_execucao <= 60:
        return pessoas_redirect(destino, "Mês de execução deve ficar entre 1 e 60.", True)
    leitura = data.get("leitura_pao") if data.get("leitura_pao") in LEITURAS_PAO else "C"
    feriados = sorted(d for d in parse_feriados(data.get("feriados")) if d.year == ano and d.month == mes)
    parametro = db.get(PessoasParametroMes, competencia) or PessoasParametroMes(competencia=competencia)
    parametro.feriados = ",".join(d.isoformat() for d in feriados)
    parametro.mes_execucao = mes_execucao
    parametro.leitura_pao = leitura
    parametro.valor_item1 = parse_money(data.get("valor_item1"))
    parametro.valor_item2 = parse_money(data.get("valor_item2"))
    db.add(parametro)
    db.commit()
    log_pessoas_action(db, user, "pessoas_parametros_save", "pessoas_parametros_mes", f"Parâmetros de {competencia} salvos.")
    return pessoas_redirect(destino, "Parâmetros salvos.")
