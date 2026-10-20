"""Coleta do SLA do Zabbix para os indicadores IND01 e IND02 (TR Tabelas 23 e 24).

O cálculo de disponibilidade é o do próprio Zabbix (sla.getsli): manutenções programadas e indisponibilidades
de terceiros devem ser cadastradas como "excluded downtimes" no SLA do Zabbix (TR 8.28.3.7).
Indicador do mês = média aritmética das disponibilidades dos serviços (Tabelas 23/24).
"""
from __future__ import annotations

import json
import logging
import ssl
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt_password, encrypt_password, is_encrypted
from app.models import ZabbixSli
from app.pessoas import INICIO_CONTRATO
from app.system_config import get_system_config_value, set_system_config_value

logger = logging.getLogger(__name__)
settings = get_settings()

TZ = ZoneInfo("America/Sao_Paulo")
CFG_URL = "zabbix_url"
CFG_TOKEN = "zabbix_token"
CFG_VERIFY = "zabbix_verificar_certificado"
CFG_SLA = {"IND01": "zabbix_sla_ind01", "IND02": "zabbix_sla_ind02"}
CFG_ULTIMA = "zabbix_ultima_coleta"
CFG_ERRO = "zabbix_ultimo_erro"
METAS = {"IND01": 99.4, "IND02": 98.5}
MAX_PERIODOS = 90  # sla.getsli aceita até 100 períodos por chamada
RECOLETA_DIAS = 3  # dias recentes recalculados a cada coleta (o dia atual ainda está em andamento)


class ZabbixErro(RuntimeError):
    pass


# ---------------------------------------------------------------- configuração


def config(db: Session) -> dict:
    token = get_system_config_value(db, CFG_TOKEN) or ""
    return {
        "url": get_system_config_value(db, CFG_URL) or "",
        "tem_token": bool(token),
        "verificar": (get_system_config_value(db, CFG_VERIFY, "true") or "true") == "true",
        "sla": {ind: get_system_config_value(db, chave) or "" for ind, chave in CFG_SLA.items()},
        "ultima_coleta": get_system_config_value(db, CFG_ULTIMA) or "",
        "ultimo_erro": get_system_config_value(db, CFG_ERRO) or "",
    }


def salvar_config(db: Session, url: str, token: str | None, verificar: bool, sla_ind01: str, sla_ind02: str) -> None:
    set_system_config_value(db, CFG_URL, url.strip())
    if token:
        set_system_config_value(db, CFG_TOKEN, encrypt_password(token.strip(), settings.encryption_key))
    set_system_config_value(db, CFG_VERIFY, "true" if verificar else "false")
    set_system_config_value(db, CFG_SLA["IND01"], sla_ind01.strip())
    set_system_config_value(db, CFG_SLA["IND02"], sla_ind02.strip())


def _token(db: Session) -> str:
    valor = get_system_config_value(db, CFG_TOKEN) or ""
    if not valor:
        raise ZabbixErro("Token da API do Zabbix não configurado.")
    return decrypt_password(valor, settings.encryption_key) if is_encrypted(valor) else valor


# ---------------------------------------------------------------- cliente da API


class Cliente:
    def __init__(self, url: str, token: str, verificar: bool = True):
        base = url.strip().rstrip("/")
        self.endpoint = base if base.endswith("api_jsonrpc.php") else f"{base}/api_jsonrpc.php"
        self.token = token
        self.contexto = ssl.create_default_context()
        if not verificar:
            self.contexto.check_hostname = False
            self.contexto.verify_mode = ssl.CERT_NONE

    def chamar(self, metodo: str, params: dict, autenticar: bool = True):
        corpo = json.dumps({"jsonrpc": "2.0", "method": metodo, "params": params, "id": 1}).encode()
        cabecalhos = {"Content-Type": "application/json-rpc"}
        if autenticar:
            cabecalhos["Authorization"] = f"Bearer {self.token}"
        try:
            resposta = urllib.request.urlopen(
                urllib.request.Request(self.endpoint, data=corpo, headers=cabecalhos), context=self.contexto, timeout=120
            ).read()
        except Exception as exc:
            raise ZabbixErro(f"Falha ao acessar {self.endpoint}: {exc}") from exc
        dados = json.loads(resposta)
        if "error" in dados:
            erro = dados["error"]
            raise ZabbixErro(f"{metodo}: {erro.get('data') or erro.get('message')}")
        return dados["result"]


def cliente(db: Session) -> Cliente:
    cfg = config(db)
    if not cfg["url"]:
        raise ZabbixErro("URL do Zabbix não configurada.")
    return Cliente(cfg["url"], _token(db), cfg["verificar"])


def testar(db: Session) -> dict:
    c = cliente(db)
    versao = c.chamar("apiinfo.version", {}, autenticar=False)
    slas = c.chamar("sla.get", {"output": ["slaid", "name", "slo", "period", "status"], "selectServiceTags": "extend",
                                "selectExcludedDowntimes": "count"})
    hosts = c.chamar("host.get", {"countOutput": True, "monitored_hosts": True})
    return {"versao": versao, "slas": slas, "hosts": hosts}


# ---------------------------------------------------------------- coleta


def _data_local(timestamp) -> date:
    return datetime.fromtimestamp(int(timestamp), TZ).date()


def _inicio_coleta(db: Session, indicador: str) -> date:
    ultima = db.query(func.max(ZabbixSli.data)).filter(ZabbixSli.indicador == indicador).scalar()
    if not ultima:
        return INICIO_CONTRATO
    return max(INICIO_CONTRATO, ultima - timedelta(days=RECOLETA_DIAS))


def coletar_indicador(db: Session, c: Cliente, indicador: str, slaid: int, desde: date, ate: datetime) -> int:
    """Grava o SLI diário de cada serviço do SLA entre `desde` e `ate`. Retorna o número de linhas gravadas."""
    gravadas = 0
    inicio = datetime.combine(desde, datetime.min.time(), TZ)
    nomes: dict[int, str] = {}
    while inicio < ate:
        fim = min(ate, inicio + timedelta(days=MAX_PERIODOS))
        resultado = c.chamar("sla.getsli", {"slaid": str(slaid), "period_from": int(inicio.timestamp()),
                                             "period_to": int(fim.timestamp())})
        periodos = resultado.get("periods", [])
        servicos = [int(s) for s in resultado.get("serviceids", [])]
        faltam = [s for s in servicos if s not in nomes]
        if faltam:
            for s in c.chamar("service.get", {"output": ["serviceid", "name"], "serviceids": faltam}):
                nomes[int(s["serviceid"])] = s["name"]
        linhas = resultado.get("sli", [])
        for i, periodo in enumerate(periodos):
            dia = _data_local(periodo["period_from"])
            db.query(ZabbixSli).filter(ZabbixSli.indicador == indicador, ZabbixSli.data == dia).delete()
            for j, serviceid in enumerate(servicos):
                celula = linhas[i][j] if i < len(linhas) and j < len(linhas[i]) else {}
                uptime, downtime = int(celula.get("uptime", 0)), int(celula.get("downtime", 0))
                excluido = sum(int(x.get("period_to", 0)) - int(x.get("period_from", 0)) for x in celula.get("excluded_downtimes", []) or [])
                db.add(ZabbixSli(
                    data=dia, mes=f"{dia.year:04d}-{dia.month:02d}", indicador=indicador, slaid=slaid,
                    serviceid=serviceid, servico=(nomes.get(serviceid) or f"Serviço {serviceid}")[:255],
                    uptime=uptime, downtime=downtime, excluido=excluido,
                    sli=round(100.0 * uptime / (uptime + downtime), 4) if uptime + downtime else None,
                ))
                gravadas += 1
        db.commit()
        inicio = fim
    return gravadas


def coletar(db: Session, completo: bool = False) -> dict:
    """Coleta IND01 e IND02. Por padrão recalcula só os últimos dias; `completo` refaz desde o início do contrato."""
    cfg = config(db)
    c = cliente(db)
    agora = datetime.now(TZ)
    resumo = {}
    for indicador in ("IND01", "IND02"):
        slaid = cfg["sla"].get(indicador)
        if not slaid:
            resumo[indicador] = "SLA não configurado"
            continue
        desde = INICIO_CONTRATO if completo else _inicio_coleta(db, indicador)
        resumo[indicador] = coletar_indicador(db, c, indicador, int(slaid), desde, agora)
    set_system_config_value(db, CFG_ULTIMA, agora.strftime("%Y-%m-%d %H:%M"))
    set_system_config_value(db, CFG_ERRO, "")
    return resumo


def run_zabbix_job() -> None:
    """Tarefa agendada (antes da rodada de relatórios). Silenciosa se a integração não estiver configurada."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        cfg = config(db)
        if not cfg["url"] or not cfg["tem_token"]:
            return
        resumo = coletar(db)
        logger.info("Coleta Zabbix IND01/IND02: %s", resumo)
    except Exception as exc:
        db.rollback()
        logger.exception("Falha na coleta do Zabbix")
        try:
            set_system_config_value(db, CFG_ERRO, f"{datetime.now(TZ):%Y-%m-%d %H:%M} {str(exc)[:400]}")
        except Exception:
            db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------- resumo para a tela


def resumo_mes(db: Session, mes: str) -> list[dict]:
    """IND01/IND02 do mês = média aritmética da disponibilidade de cada serviço (soma de uptime ÷ tempo medido)."""
    saida = []
    for indicador, meta in METAS.items():
        linhas = (
            db.query(ZabbixSli.servico, func.sum(ZabbixSli.uptime), func.sum(ZabbixSli.downtime), func.count(ZabbixSli.id))
            .filter(ZabbixSli.indicador == indicador, ZabbixSli.mes == mes)
            .group_by(ZabbixSli.serviceid, ZabbixSli.servico)
            .all()
        )
        # SUM() no MariaDB devolve Decimal: converter antes de calcular
        por_servico = []
        for nome, up, down, dias in linhas:
            up, down = float(up or 0), float(down or 0)
            if up + down:
                por_servico.append((nome, 100.0 * up / (up + down), int(dias or 0)))
        valor = sum(v for _, v, _ in por_servico) / len(por_servico) if por_servico else None
        saida.append({
            "indicador": indicador,
            "meta": meta,
            "valor": valor,
            "servicos": len(por_servico),
            "dias": max((d for _, _, d in por_servico), default=0),
            "na_meta": valor is not None and valor >= meta,
            "piores": sorted(por_servico, key=lambda x: x[1])[:10],
        })
    return saida
