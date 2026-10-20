"""Reconhecimento EXPLÍCITO de uma posição aberta manualmente na mesma conta.

Registro deliberadamente pequeno e SEPARADO de `RealTrade`: o trade manager
gerencia `RealTrade` (coloca/move SL, fecha, cancela condicionais), e uma posição
manual reconhecida NÃO pode ser gerenciada pelo bot. Reconhecer aqui significa
apenas: "esta posição é do operador; o bot não a administra, não a conta no
próprio orçamento nominal e mantém o símbolo inteiro indisponível".

Invariantes:

- No máximo UM reconhecimento `ACTIVE` por conta/exchange/mercado/símbolo
  (índice parcial único no banco — defesa final contra corrida).
- Histórico preservado: invalidar/encerrar muda `state` e carimba o motivo; a
  linha anterior nunca é sobrescrita nem apagada.
- `fingerprint` é a identidade observável da posição no momento do
  reconhecimento (conta, mercado, símbolo/quote, lado, positionSide, qty,
  preço de entrada e versão temporal da exchange). Mark price e P&L NÃO entram:
  oscilação de preço não exige novo reconhecimento.
- `identity_note`/`reason` são texto administrativo NÃO secreto; nenhum token,
  credencial ou stack trace entra aqui.
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db import Base

#: Estados do reconhecimento. VALIDADE do reconhecimento e BLOQUEIO do símbolo
#: são coisas diferentes: "não está ACTIVE" NUNCA significa "símbolo livre".
#: Posição manual reconhecida e compatível. Símbolo bloqueado.
STATE_ACTIVE = "ACTIVE"
#: Identidade mudou/não corresponde mais. Símbolo CONTINUA bloqueado: é preciso
#: nova confirmação administrativa ou encerramento comprovado.
STATE_INVALIDATED = "INVALIDATED"
#: Posição comprovadamente flat, mas restam ordens (ou não foi possível provar
#: a ausência delas). Símbolo CONTINUA bloqueado.
STATE_WAITING_ORDERS = "WAITING_ORDERS"
#: Flat E ausência fresca de ordens comuns E condicionais, comprovadas antes da
#: transição. Só aqui o símbolo volta a ficar livre.
STATE_CLOSED = "CLOSED"
#: Registro antigo substituído por NOVA confirmação administrativa explícita,
#: atomicamente. É histórico — nunca transição automática por mudança de posição.
STATE_SUPERSEDED = "SUPERSEDED"
ACK_STATES = (STATE_ACTIVE, STATE_INVALIDATED, STATE_WAITING_ORDERS,
              STATE_CLOSED, STATE_SUPERSEDED)
#: Estados que BLOQUEIAM o símbolo. Mesmo predicado usado no transporte, na
#: admissão, no reconciliador e no índice de unicidade.
BLOCKING_STATES = (STATE_ACTIVE, STATE_INVALIDATED, STATE_WAITING_ORDERS)
#: Estados ENCERRADOS (histórico). Não bloqueiam nem contam para unicidade.
ENDED_STATES = (STATE_CLOSED, STATE_SUPERSEDED)

#: Versão do contrato de identidade. Mudar a fórmula do fingerprint exige
#: subir esta versão: reconhecimento de contrato antigo não vale para o novo.
ACK_CONTRACT_VERSION = "MANUAL_ACK_V1"


class ManualPositionAcknowledgement(Base):
    __tablename__ = "manual_position_acks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Escopo OPACO da conta (mesmo contrato do ledger R05C/R05D).
    account_scope: Mapped[str] = mapped_column(String(64), index=True)
    exchange: Mapped[str] = mapped_column(String(20))
    market: Mapped[str] = mapped_column(String(20))
    #: Símbolo canônico do mercado (`BTC/USDT:USDT`) e quote separada.
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    quote: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(8))
    position_side: Mapped[str] = mapped_column(String(10))
    #: Decimais canônicos — float perderia a identidade em qty pequena.
    qty: Mapped[object] = mapped_column(Numeric(38, 18))
    entry_price: Mapped[object] = mapped_column(Numeric(38, 18))
    #: Identificador temporal REAL fornecido pela exchange (`updateTime`).
    #: Ausente/inválido NÃO vira relógio local: o reconhecimento é recusado.
    exchange_update_time_ms: Mapped[int] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    contract_version: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16), index=True)
    #: Motivo administrativo e identidade NÃO secreta de quem reconheceu.
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    identity_note: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Chave do incidente UNTRACKED vinculado (mesma transação do ack).
    incident_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    #: Observações auditáveis do momento do reconhecimento (sem segredo).
    evidence: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: CAS: toda mudança de estado incrementa a revisão. Um scan atrasado não
    #: pode fechar um reconhecimento mais novo.
    revision: Mapped[int] = mapped_column(Integer, default=0)
    #: Revisão EXATA do reconhecimento que foi validada. Prova só autoriza
    #: quando `revision == validated_revision`: qualquer mudança de identidade,
    #: estado ou conta sobe a revisão e derruba a autorização no mesmo commit.
    validated_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Época da VALIDAÇÃO DE CONTA que governa esta prova (não é a geração
    #: FINANCEIRA). Falha de validação avança a época da conta e invalida todas
    #: as provas publicadas sob a época anterior.
    validated_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Prova de VALIDAÇÃO: instante (ms), escopo e conta da última observação
    #: fresca e completa que confirmou este registro. Nova exposição exige prova
    #: válida e atual — ausência/expiração NEGA.
    validated_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    validation_scope: Mapped[str | None] = mapped_column(String(16), nullable=True)
    validation_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Quando saiu de ACTIVE (invalidado ou fechado). ACTIVE ⇒ NULL.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)

    __table_args__ = (
        # Defesa NO BANCO: duas confirmações concorrentes jamais deixam dois
        # reconhecimentos ativos para a mesma posição.
        Index(
            "uq_manual_ack_open",
            "account_scope", "exchange", "market", "symbol",
            unique=True,
            postgresql_where=text(
                "state IN ('ACTIVE','INVALIDATED','WAITING_ORDERS')"),
            sqlite_where=text(
                "state IN ('ACTIVE','INVALIDATED','WAITING_ORDERS')"),
        ),
        Index("ix_manual_ack_state_symbol", "state", "symbol"),
    )

    def to_public(self) -> dict:
        """Projeção SEM segredo, para respostas administrativas e diagnóstico."""
        return {
            "id": self.id,
            "account_scope": self.account_scope,
            "exchange": self.exchange,
            "market": self.market,
            "symbol": self.symbol,
            "quote": self.quote,
            "side": self.side,
            "position_side": self.position_side,
            "qty": str(self.qty),
            "entry_price": str(self.entry_price),
            "exchange_update_time_ms": int(self.exchange_update_time_ms or 0),
            "fingerprint": self.fingerprint,
            "contract_version": self.contract_version,
            "state": self.state,
            "revision": int(self.revision or 0),
            "validated_revision": (int(self.validated_revision)
                                   if self.validated_revision is not None else None),
            "validated_generation": (int(self.validated_generation)
                                     if self.validated_generation is not None else None),
            "validated_at_ms": (int(self.validated_at_ms)
                                if self.validated_at_ms is not None else None),
            "validation_scope": self.validation_scope,
            "validation_account": self.validation_account,
            "reason": self.reason,
            "identity_note": self.identity_note,
            "incident_key": self.incident_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "ended_reason": self.ended_reason,
        }
