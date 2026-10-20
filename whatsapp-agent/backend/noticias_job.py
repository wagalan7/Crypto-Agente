"""Boletim de notícias (E08B) — cache por segmento, job diário e leitura pelo painel.

## Como funciona

- **Um boletim por segmento**, compartilhado por todos os consultórios do
  segmento. Abrir o painel só **lê o cache** (`ler_boletim`): nenhuma coleta,
  nenhuma chamada de IA por consultório ou por acesso.
- **Job dedicado** (thread própria, checagem a cada `TICK_S` segundos), separado
  do scheduler de 30 min dos pacientes, cuja cadência não muda. A edição diária
  é devida a partir das **09:00 em America/Sao_Paulo**, todos os dias, inclusive
  fim de semana. O horário vem de `zoneinfo`, nunca do fuso do servidor.
- **Execução única por chave** `(segmento, chave)`: `diaria:AAAA-MM-DD`,
  `inicial` (primeiro preenchimento, uma vez) e `manual:AAAA-MM-DDTHH` (pedido
  pelo painel, no máximo uma por hora por segmento). A posse é um *lease* no
  banco, tomado num único UPSERT: dois processos ou um restart não executam a
  mesma chave duas vezes. Lease vencido (crash no meio) é retomado como nova
  tentativa.
- **Sem transação aberta durante HTTP:** toma o lease (transação curta),
  baixa as fontes (sem transação) e grava o resultado (outra transação curta),
  conferindo antes que o lease ainda é seu.
- **Falha** de todas as fontes: a edição anterior fica intacta; nova tentativa
  com backoff (10, 30, 90 min), até `TENTATIVAS_MAX`; depois disso a chave fica
  `esgotada`, uma pendência operacional visível em `/admin/noticias/estado` e
  no aviso do quadro. Falha de **uma** fonte não apaga os itens bons de outra.
- **Atraso:** se o app estava fora às 09:00, a edição sai quando ele voltar, e
  o atraso real fica registrado (`atraso_s`). O quadro nunca diz que a
  atualização foi pontual.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time as _time
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx

import database as db
import noticias as nt

logger = logging.getLogger(__name__)

UTC = nt.UTC
TZ = nt.TZ_BRASILIA
HORA_DIARIA = time(9, 0)
TICK_S = int(os.getenv("NOTICIAS_TICK_S", "60"))
LEASE = timedelta(minutes=10)
TENTATIVAS_MAX = 4
BACKOFF = (timedelta(minutes=10), timedelta(minutes=30), timedelta(minutes=90))
COOLDOWN_MANUAL = timedelta(minutes=60)
RETENCAO_ITENS = timedelta(days=60)
RETENCAO_EXECUCOES = timedelta(days=90)


def agora_utc() -> datetime:
    return datetime.now(UTC)


def _iso(d: datetime | None) -> str | None:
    return d.astimezone(UTC).isoformat(timespec="seconds") if d else None


def _de_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


# ── Esquema ───────────────────────────────────────────────────────────────────

def criar_tabelas(conn) -> None:
    """Chamado por database.init_db(). Só cria (IF NOT EXISTS): nada é alterado
    nas tabelas existentes. Reverter = parar de usar; as tabelas podem ficar."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS noticias_itens (
            chave             TEXT PRIMARY KEY,      -- URL sem rastreio (dedup)
            fonte_id          TEXT NOT NULL,
            url               TEXT NOT NULL,
            titulo            TEXT NOT NULL,
            resumo            TEXT NOT NULL DEFAULT '',
            publicado_em      TEXT,                  -- UTC ISO; NULL = sem data
            atualizado_fonte  TEXT,                  -- <updated> do feed (só registro)
            primeira_coleta   TEXT NOT NULL,
            ultima_coleta     TEXT NOT NULL,
            ficticio          INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_noticias_itens_fonte ON noticias_itens(fonte_id, publicado_em);
        CREATE TABLE IF NOT EXISTS noticias_fontes_estado (
            fonte_id          TEXT PRIMARY KEY,
            ultima_tentativa  TEXT,
            ultimo_sucesso    TEXT,
            ultimo_erro       TEXT,
            itens_no_feed     INTEGER
        );
        CREATE TABLE IF NOT EXISTS noticias_edicoes (
            segmento               TEXT PRIMARY KEY,
            itens_json             TEXT NOT NULL,
            assinatura             TEXT NOT NULL,
            conteudo_atualizado_em TEXT NOT NULL,  -- quando a seleção mudou
            verificado_em          TEXT NOT NULL,  -- última verificação com ao menos 1 fonte ok
            chave_execucao         TEXT NOT NULL,
            ficticio               INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS noticias_execucoes (
            segmento            TEXT NOT NULL,
            chave               TEXT NOT NULL,     -- diaria:AAAA-MM-DD | inicial | manual:AAAA-MM-DDTHH
            agendada_para       TEXT NOT NULL,     -- UTC ISO (09:00 local para a diária)
            status              TEXT NOT NULL,     -- executando | sucesso | parcial | falha | esgotada
            tentativas          INTEGER NOT NULL DEFAULT 0,
            dono_lease          TEXT,
            lease_ate           TEXT,
            iniciada_em         TEXT,
            concluida_em        TEXT,
            proxima_tentativa   TEXT,
            atraso_s            INTEGER,           -- concluída − agendada (recuperação)
            erro                TEXT,
            resultado_json      TEXT,
            PRIMARY KEY (segmento, chave)
        );
    """)


# ── Fontes efetivas (reais ou fictícias de prévia) ────────────────────────────

_FIXTURE_HOST = "noticias-ficticias.invalid"


def _dir_fixtures() -> Path | None:
    """Modo prévia: NOTICIAS_FIXTURES=<pasta> troca as fontes reais por fontes
    fictícias servidas do disco. Recusado se o banco parecer o de produção."""
    pasta = (os.getenv("NOTICIAS_FIXTURES") or "").strip()
    if not pasta:
        return None
    if os.path.abspath(os.path.dirname(db.DB_PATH)).rstrip("/") in ("/data", "/app"):
        logger.error("[noticias] NOTICIAS_FIXTURES ignorado: banco parece ser de produção")
        return None
    return Path(pasta)


def fontes_do_segmento(segmento: str) -> tuple:
    seg = nt.SEGMENTOS[segmento]
    if _dir_fixtures() is None:
        return seg.fontes
    return tuple(
        nt.Fonte(id=f"ficticia-{f.id}", nome=f"{f.nome} (FICTÍCIO)",
                 url=f"https://{_FIXTURE_HOST}/{f.id}.xml", formato=f.formato,
                 hosts=frozenset({_FIXTURE_HOST}), exige_tema=f.exige_tema, ficticia=True)
        for f in seg.fontes
    )


def _transporte_fixtures() -> httpx.BaseTransport | None:
    pasta = _dir_fixtures()
    if pasta is None:
        return None

    def servir(req: httpx.Request) -> httpx.Response:
        arq = pasta / Path(req.url.path).name
        if req.url.host != _FIXTURE_HOST or not arq.is_file():
            return httpx.Response(404)
        corpo = _datas_relativas(arq.read_text(encoding="utf-8"))
        tipo = "application/atom+xml" if "<feed" in corpo[:400] else "application/rss+xml"
        return httpx.Response(200, headers={"content-type": tipo}, content=corpo.encode())

    return httpx.MockTransport(servir)


def _datas_relativas(texto: str) -> str:
    """Fixtures usam {{RFC822:-2d}} / {{ISO:-30h}} para ficarem 'recentes' na prévia."""
    import re
    agora = agora_utc()

    def troca(m):
        n, unidade = int(m.group(2)), m.group(3)
        d = agora + (timedelta(days=n) if unidade == "d" else timedelta(hours=n))
        return d.strftime("%a, %d %b %Y %H:%M:%S +0000") if m.group(1) == "RFC822" else _iso(d)

    return re.sub(r"\{\{(RFC822|ISO):([+-]\d+)([dh])\}\}", troca, texto)


# ── Execuções (lease) ─────────────────────────────────────────────────────────

def agendada_para(dia_local: date) -> datetime:
    return datetime.combine(dia_local, HORA_DIARIA, tzinfo=TZ).astimezone(UTC)


def _reivindicar(segmento: str, chave: str, agendada: datetime, agora: datetime, dono: str) -> int | None:
    """Toma a execução (segmento, chave) num único UPSERT. Devolve o número da
    tentativa ou None se outro processo tem o lease, se já concluiu ou se ainda
    não é hora da próxima tentativa."""
    a = _iso(agora)
    with db.get_conn() as conn:
        # Lease vencido com tentativas esgotadas: encerra em vez de retomar.
        conn.execute(
            """UPDATE noticias_execucoes SET status='esgotada', dono_lease=NULL, lease_ate=NULL,
                   concluida_em=?, erro=COALESCE(erro, 'interrompida sem concluir (lease vencido)')
               WHERE segmento=? AND chave=? AND status='executando' AND lease_ate < ? AND tentativas >= ?""",
            (a, segmento, chave, a, TENTATIVAS_MAX))
        conn.execute(
            """INSERT INTO noticias_execucoes
                   (segmento, chave, agendada_para, status, tentativas, dono_lease, lease_ate, iniciada_em)
               VALUES (?, ?, ?, 'executando', 1, ?, ?, ?)
               ON CONFLICT(segmento, chave) DO UPDATE SET
                   status='executando', tentativas=noticias_execucoes.tentativas + 1,
                   dono_lease=excluded.dono_lease, lease_ate=excluded.lease_ate,
                   iniciada_em=excluded.iniciada_em, proxima_tentativa=NULL
               WHERE noticias_execucoes.tentativas < ? AND (
                   (noticias_execucoes.status='falha' AND noticias_execucoes.proxima_tentativa <= ?)
                   OR (noticias_execucoes.status='executando' AND noticias_execucoes.lease_ate < ?))""",
            (segmento, chave, _iso(agendada), dono, _iso(agora + LEASE), a, TENTATIVAS_MAX, a, a))
        row = conn.execute(
            "SELECT status, dono_lease, tentativas FROM noticias_execucoes WHERE segmento=? AND chave=?",
            (segmento, chave)).fetchone()
    if row and row["status"] == "executando" and row["dono_lease"] == dono:
        return row["tentativas"]
    return None


def _execucao(segmento: str, chave: str):
    with db.get_conn() as conn:
        return conn.execute("SELECT * FROM noticias_execucoes WHERE segmento=? AND chave=?",
                            (segmento, chave)).fetchone()


def _concluida(segmento: str, chave: str) -> bool:
    row = _execucao(segmento, chave)
    return bool(row and row["status"] in ("sucesso", "parcial", "esgotada"))


def _tem_edicao(segmento: str) -> bool:
    with db.get_conn() as conn:
        return conn.execute("SELECT 1 FROM noticias_edicoes WHERE segmento=?", (segmento,)).fetchone() is not None


def ultimo_horario(agora: datetime) -> datetime:
    """O horário das 09:00 (America/Sao_Paulo) mais recente que já passou."""
    local = agora.astimezone(TZ)
    dia = local.date() if local.time() >= HORA_DIARIA else local.date() - timedelta(days=1)
    return agendada_para(dia)


def chaves_devidas(segmento: str, agora: datetime) -> list[tuple[str, datetime]]:
    """Execuções que deveriam rodar agora. Só lê o banco.

    A diária devida é a do último horário das 09:00 que já passou: se o app
    estava fora às 09:00 (ou o dia inteiro), a edição sai quando ele voltar,
    com o atraso registrado. Exceção: o primeiro preenchimento concluído depois
    desse horário já o cobre (evita duas coletas seguidas no primeiro deploy)."""
    devidas = []
    inicial = _execucao(segmento, "inicial")
    if not _tem_edicao(segmento) and not (inicial and inicial["status"] in ("sucesso", "parcial", "esgotada")):
        # O primeiro preenchimento cobre o último horário: só ele é devido.
        return [("inicial", agora)]
    horario = ultimo_horario(agora)
    chave = f"diaria:{horario.astimezone(TZ).date().isoformat()}"
    coberta_pela_inicial = (inicial and inicial["status"] in ("sucesso", "parcial")
                            and inicial["concluida_em"] and _de_iso(inicial["concluida_em"]) >= horario)
    if not coberta_pela_inicial and not _concluida(segmento, chave):
        devidas.append((chave, horario))
    return devidas


def tick(dono: str, transporte: httpx.BaseTransport | None = None, relogio=None) -> list[dict]:
    """Uma volta do job: executa o que estiver devido e for reivindicável.
    `relogio` (callable → datetime aware) existe para os testes fixarem a hora."""
    relogio = relogio or agora_utc
    resultados = []
    if not nt.modulo_habilitado():
        return resultados
    for segmento in nt.SEGMENTOS:
        agora = relogio()
        for chave, agendada in chaves_devidas(segmento, agora):
            tentativa = _reivindicar(segmento, chave, agendada, agora, dono)
            if tentativa is not None:
                resultados.append(executar(segmento, chave, tentativa, dono,
                                           transporte=transporte, relogio=relogio))
    return resultados


def executar(segmento: str, chave: str, tentativa: int, dono: str,
             transporte: httpx.BaseTransport | None = None, relogio=None) -> dict:
    """Corre a execução cujo lease este `dono` acabou de tomar."""
    fontes = fontes_do_segmento(segmento)
    coletas, resultado = [], {}
    transporte = transporte or _transporte_fixtures()
    with nt.novo_cliente(transporte) as cliente:          # ← nenhuma transação aberta aqui
        for f in fontes:
            try:
                itens, descartes = nt.interpretar(f, nt.baixar(f, cliente))
                coletas.append((f, itens))
                resultado[f.id] = {"ok": True, "itens": len(itens), "descartes": descartes}
            except nt.ErroColeta as e:
                resultado[f.id] = {"ok": False, "erro": str(e)}
            except Exception as e:  # erro inesperado de uma fonte não derruba as outras
                logger.exception(f"[noticias] fonte {f.id}: erro inesperado")
                resultado[f.id] = {"ok": False, "erro": f"erro inesperado: {type(e).__name__}"}

    agora = (relogio or agora_utc)()
    a = _iso(agora)
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, dono_lease, agendada_para FROM noticias_execucoes WHERE segmento=? AND chave=?",
                           (segmento, chave)).fetchone()
        if not row or row["status"] != "executando" or row["dono_lease"] != dono:
            logger.warning(f"[noticias] {segmento}/{chave}: lease perdido; resultado descartado")
            return {"segmento": segmento, "chave": chave, "status": "lease_perdido"}

        for f, itens in coletas:
            for it in itens:
                conn.execute(
                    """INSERT INTO noticias_itens (chave, fonte_id, url, titulo, resumo, publicado_em,
                           atualizado_fonte, primeira_coleta, ultima_coleta, ficticio)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(chave) DO UPDATE SET url=excluded.url, titulo=excluded.titulo,
                           resumo=excluded.resumo, publicado_em=excluded.publicado_em,
                           atualizado_fonte=excluded.atualizado_fonte, ultima_coleta=excluded.ultima_coleta""",
                    (it.chave, f.id, it.url, it.titulo, it.resumo, _iso(it.publicado_em),
                     _iso(it.atualizado_em), a, a, int(it.ficticio)))
        for f in fontes:
            r = resultado[f.id]
            conn.execute(
                """INSERT INTO noticias_fontes_estado (fonte_id, ultima_tentativa, ultimo_sucesso, ultimo_erro, itens_no_feed)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(fonte_id) DO UPDATE SET ultima_tentativa=excluded.ultima_tentativa,
                       ultimo_sucesso=COALESCE(excluded.ultimo_sucesso, noticias_fontes_estado.ultimo_sucesso),
                       ultimo_erro=excluded.ultimo_erro,
                       itens_no_feed=COALESCE(excluded.itens_no_feed, noticias_fontes_estado.itens_no_feed)""",
                (f.id, a, a if r["ok"] else None, None if r["ok"] else r["erro"], r.get("itens")))

        atraso = None
        if chave.startswith("diaria:"):
            atraso = int((agora - _de_iso(row["agendada_para"])).total_seconds())
        ok = [f for f in fontes if resultado[f.id]["ok"]]
        if ok:
            itens_seg = _itens_do_banco(conn, fontes)
            escolhidos, descartes = nt.selecionar(itens_seg, {f.id: f for f in fontes}, agora)
            mudou = _gravar_edicao(conn, segmento, escolhidos, fontes, agora, chave)
            status = "sucesso" if len(ok) == len(fontes) else "parcial"
            resultado["_selecao"] = {"destaques": len(escolhidos), "descartes": descartes, "conteudo_mudou": mudou}
            conn.execute(
                """UPDATE noticias_execucoes SET status=?, concluida_em=?, atraso_s=?, erro=NULL,
                       resultado_json=?, dono_lease=NULL, lease_ate=NULL WHERE segmento=? AND chave=?""",
                (status, a, atraso, json.dumps(resultado, ensure_ascii=False), segmento, chave))
            conn.execute("DELETE FROM noticias_itens WHERE publicado_em IS NOT NULL AND publicado_em < ?",
                         (_iso(agora - RETENCAO_ITENS),))
            conn.execute("DELETE FROM noticias_execucoes WHERE iniciada_em < ? AND status != 'executando'",
                         (_iso(agora - RETENCAO_EXECUCOES),))
        else:
            row2 = conn.execute("SELECT tentativas FROM noticias_execucoes WHERE segmento=? AND chave=?",
                                (segmento, chave)).fetchone()
            esgotou = row2["tentativas"] >= TENTATIVAS_MAX
            status = "esgotada" if esgotou else "falha"
            espera = BACKOFF[min(row2["tentativas"], len(BACKOFF)) - 1]
            conn.execute(
                """UPDATE noticias_execucoes SET status=?, concluida_em=?, atraso_s=?, erro=?,
                       proxima_tentativa=?, resultado_json=?, dono_lease=NULL, lease_ate=NULL
                   WHERE segmento=? AND chave=?""",
                (status, a, atraso, "nenhuma fonte respondeu",
                 None if esgotou else _iso(agora + espera),
                 json.dumps(resultado, ensure_ascii=False), segmento, chave))
            nivel = logger.error if esgotou else logger.warning
            nivel(f"[noticias] {segmento}/{chave}: {status} na tentativa {tentativa} — {resultado}")
    logger.info(f"[noticias] {segmento}/{chave}: {status} (tentativa {tentativa}, atraso {atraso}s)")
    return {"segmento": segmento, "chave": chave, "status": status, "tentativa": tentativa,
            "atraso_s": atraso, "resultado": resultado}


def _itens_do_banco(conn, fontes) -> list:
    ids = [f.id for f in fontes]
    rows = conn.execute(
        f"SELECT * FROM noticias_itens WHERE fonte_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return [nt.Item(fonte_id=r["fonte_id"], url=r["url"], chave=r["chave"], titulo=r["titulo"],
                    resumo=r["resumo"], publicado_em=_de_iso(r["publicado_em"]),
                    atualizado_em=_de_iso(r["atualizado_fonte"]), ficticio=bool(r["ficticio"]))
            for r in rows]


def _gravar_edicao(conn, segmento, escolhidos, fontes, agora, chave) -> bool:
    nomes = {f.id: f.nome for f in fontes}
    assin = nt.assinatura(escolhidos)
    atual = conn.execute("SELECT assinatura, conteudo_atualizado_em FROM noticias_edicoes WHERE segmento=?",
                         (segmento,)).fetchone()
    mudou = atual is None or atual["assinatura"] != assin
    itens_json = json.dumps([{
        "titulo": i.titulo, "resumo": i.resumo, "url": i.url, "fonte": nomes.get(i.fonte_id, i.fonte_id),
        "publicado_em": _iso(i.publicado_em),
    } for i in escolhidos], ensure_ascii=False)
    conn.execute(
        """INSERT INTO noticias_edicoes (segmento, itens_json, assinatura, conteudo_atualizado_em,
               verificado_em, chave_execucao, ficticio)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(segmento) DO UPDATE SET itens_json=excluded.itens_json,
               assinatura=excluded.assinatura, verificado_em=excluded.verificado_em,
               chave_execucao=excluded.chave_execucao, ficticio=excluded.ficticio,
               conteudo_atualizado_em=CASE WHEN noticias_edicoes.assinatura = excluded.assinatura
                   THEN noticias_edicoes.conteudo_atualizado_em ELSE excluded.conteudo_atualizado_em END""",
        (segmento, itens_json, assin, _iso(agora), _iso(agora), chave,
         int(any(f.ficticia for f in fontes))))
    return mudou


# ── Leitura pelo painel (só cache) ────────────────────────────────────────────

def ler_boletim(tenant: dict, agora: datetime | None = None) -> dict:
    """O que o quadro mostra. Nunca acessa a internet."""
    segmento = nt.segmento_do_tenant(tenant)
    if not segmento:
        return {"habilitado": False}
    agora = agora or agora_utc()
    seg = nt.SEGMENTOS[segmento]
    fontes = fontes_do_segmento(segmento)
    with db.get_conn() as conn:
        ed = conn.execute("SELECT * FROM noticias_edicoes WHERE segmento=?", (segmento,)).fetchone()
        ultima = conn.execute(
            """SELECT chave, status, iniciada_em, concluida_em, atraso_s, proxima_tentativa
               FROM noticias_execucoes WHERE segmento=? ORDER BY iniciada_em DESC LIMIT 1""",
            (segmento,)).fetchone()
        estados = {r["fonte_id"]: r for r in conn.execute(
            f"SELECT * FROM noticias_fontes_estado WHERE fonte_id IN ({','.join('?' * len(fontes))})",
            [f.id for f in fontes]).fetchall()}

    itens, aviso = [], None
    if ed:
        inicio, limite = agora - nt.JANELA, agora + nt.TOLERANCIA_FUTURO
        for it in json.loads(ed["itens_json"]):
            pub = _de_iso(it.get("publicado_em"))
            # Cache envelhece: item que saiu da janela de 7 dias deixa de ser
            # "recente", mesmo que a última coleta tenha sido hoje.
            if pub and inicio <= pub <= limite:
                itens.append(it)

    if ultima and ultima["status"] in ("falha", "esgotada"):
        quando = ultima["concluida_em"] or ultima["iniciada_em"]
        aviso = {"tipo": "falha", "em": quando,
                 "texto": "Não foi possível consultar as fontes na última tentativa."
                          + (" Exibindo o conteúdo verificado anteriormente." if ed else "")}
    elif ultima and ultima["status"] == "parcial":
        fora = [f.nome for f in fontes if estados.get(f.id) and estados[f.id]["ultimo_erro"]]
        aviso = {"tipo": "parcial", "em": ultima["concluida_em"],
                 "texto": "Fonte indisponível na última verificação: " + ", ".join(fora) + "." if fora else None}
    if ed and not aviso:
        verificado = _de_iso(ed["verificado_em"])
        if agora - verificado > timedelta(hours=36):
            aviso = {"tipo": "desatualizado", "em": ed["verificado_em"],
                     "texto": "As fontes não são verificadas há mais de um dia."}

    return {
        "habilitado": True,
        "segmento": segmento,
        "titulo": seg.titulo,
        "itens": itens[: nt.MAX_DESTAQUES],
        "visiveis": nt.DESTAQUES_VISIVEIS,
        "situacao": "sem_edicao" if not ed else ("sem_recentes" if not itens else "ok"),
        "conteudo_atualizado_em": ed["conteudo_atualizado_em"] if ed else None,
        "verificado_em": ed["verificado_em"] if ed else None,
        "aviso": aviso if aviso and aviso.get("texto") else None,
        "ficticio": bool(ed["ficticio"]) if ed else any(f.ficticia for f in fontes),
        "fontes": [{"nome": f.nome, "ok": not (estados.get(f.id) and estados[f.id]["ultimo_erro"])}
                   for f in fontes],
        "proxima_diaria": _iso(_proxima_diaria(agora)),
    }


def _proxima_diaria(agora: datetime) -> datetime:
    local = agora.astimezone(TZ)
    dia = local.date() if local.time() < HORA_DIARIA else local.date() + timedelta(days=1)
    return agendada_para(dia)


# ── Revisão manual (painel) ───────────────────────────────────────────────────

def pedir_revisao(tenant: dict, relogio=None, dono: str | None = None,
                  em_segundo_plano: bool = True, transporte=None) -> dict:
    """O botão "Atualizar" do quadro. Distingue os dois casos:
    - `leitura_cache`: as fontes foram verificadas há menos de COOLDOWN_MANUAL
      (ou há revisão em andamento) — nada é buscado, o painel só relê o cache;
    - `revisao_iniciada`: uma revisão real das fontes começou (lease próprio).
    Compartilhado pelo segmento: vários consultórios clicando geram no máximo
    uma revisão por hora."""
    segmento = nt.segmento_do_tenant(tenant)
    if not segmento:
        return {"acao": "indisponivel"}
    relogio = relogio or agora_utc
    agora = relogio()
    with db.get_conn() as conn:
        ed = conn.execute("SELECT verificado_em FROM noticias_edicoes WHERE segmento=?", (segmento,)).fetchone()
        ult = conn.execute(
            """SELECT MAX(iniciada_em) AS m FROM noticias_execucoes
               WHERE segmento=? AND chave LIKE 'manual:%'""", (segmento,)).fetchone()
    referencias = [d for d in (_de_iso(ed["verificado_em"]) if ed else None,
                               _de_iso(ult["m"]) if ult else None) if d]
    if referencias:
        recente = max(referencias)
        if agora - recente < COOLDOWN_MANUAL:
            return {"acao": "leitura_cache", "verificado_em": ed["verificado_em"] if ed else None,
                    "proxima_revisao_em": _iso(recente + COOLDOWN_MANUAL)}
    chave = f"manual:{agora.astimezone(TZ):%Y-%m-%dT%H}"
    dono = dono or _dono()
    tentativa = _reivindicar(segmento, chave, agora, agora, dono)
    if tentativa is None:
        return {"acao": "leitura_cache", "motivo": "revisão já em andamento ou concluída nesta hora"}
    if em_segundo_plano:
        threading.Thread(target=executar, args=(segmento, chave, tentativa, dono),
                         kwargs={"transporte": transporte, "relogio": relogio},
                         daemon=True, name="noticias-manual").start()
        return {"acao": "revisao_iniciada"}
    r = executar(segmento, chave, tentativa, dono, transporte=transporte, relogio=relogio)
    return {"acao": "revisao_concluida", "status": r["status"]}


def forcar_revisao(segmento: str, relogio=None, transporte=None) -> dict:
    """Operação (MASTER_KEY): ignora o intervalo mínimo, mas não o lease."""
    if segmento not in nt.SEGMENTOS:
        return {"acao": "segmento_desconhecido"}
    relogio = relogio or agora_utc
    agora = relogio()
    chave = f"manual:{agora.astimezone(TZ):%Y-%m-%dT%H%M}"
    dono = _dono()
    tentativa = _reivindicar(segmento, chave, agora, agora, dono)
    if tentativa is None:
        return {"acao": "em_andamento"}
    return executar(segmento, chave, tentativa, dono, transporte=transporte, relogio=relogio)


def estado_operacional(limite: int = 20) -> dict:
    with db.get_conn() as conn:
        execs = [dict(r) for r in conn.execute(
            """SELECT segmento, chave, status, tentativas, agendada_para, iniciada_em, concluida_em,
                      atraso_s, proxima_tentativa, erro FROM noticias_execucoes
               ORDER BY iniciada_em DESC LIMIT ?""", (limite,)).fetchall()]
        fontes = [dict(r) for r in conn.execute("SELECT * FROM noticias_fontes_estado").fetchall()]
        eds = [{"segmento": r["segmento"], "destaques": len(json.loads(r["itens_json"])),
                "conteudo_atualizado_em": r["conteudo_atualizado_em"], "verificado_em": r["verificado_em"],
                "ficticio": bool(r["ficticio"])}
               for r in conn.execute("SELECT * FROM noticias_edicoes").fetchall()]
    pendencias = [e for e in execs if e["status"] in ("falha", "esgotada")]
    return {"modulo_habilitado": nt.modulo_habilitado(), "edicoes": eds, "fontes": fontes,
            "execucoes": execs, "pendencias": pendencias}


# ── Thread do job ─────────────────────────────────────────────────────────────

def _dono() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _laco(dono: str) -> None:
    while True:
        try:
            tick(dono)
        except Exception:
            logger.exception("[noticias] erro no job (segue no próximo ciclo)")
        _time.sleep(TICK_S)


def iniciar() -> None:
    if (os.getenv("NOTICIAS_JOB_ENABLED") or "1").strip() == "0" or not nt.modulo_habilitado():
        logger.info("[noticias] job desligado")
        return
    dono = _dono()
    threading.Thread(target=_laco, args=(dono,), daemon=True, name="noticias-boletim").start()
    logger.info(f"[noticias] job iniciado: edição diária a partir das 09:00 America/Sao_Paulo, "
                f"checagem a cada {TICK_S}s ({dono})")
