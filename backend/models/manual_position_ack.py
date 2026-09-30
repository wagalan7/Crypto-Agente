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

#: Estados do reconhecimento. Só `ACTIVE` isenta o símbolo do orçamento BOT e
#: permite encerrar o incidente UNTRACKED correspondente.
STATE_ACTIVE = "ACTIVE"
#: Identidade divergiu (qty/lado/versão temporal/conta) — autorização caiu.
STATE_INVALIDATED = "INVALIDATED"
#: Posição comprovadamente fechada — histórico preservado.
STATE_CLOSED = "CLOSED"
ACK_STATES = (STATE_ACTIVE, STATE_INVALIDATED, STATE_CLOSED)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Quando saiu de ACTIVE (invalidado ou fechado). ACTIVE ⇒ NULL.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)

    __table_args__ = (
        # Defesa NO BANCO: duas confirmações concorrentes jamais deixam dois
        # reconhecimentos ativos para a mesma posição.
        Index(
            "uq_manual_ack_active",
            "account_scope", "exchange", "market", "symbol",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
            sqlite_where=text("state = 'ACTIVE'"),
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
            "reason": self.reason,
            "identity_note": self.identity_note,
            "incident_key": self.incident_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "ended_reason": self.ended_reason,
        }
