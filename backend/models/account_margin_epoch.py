"""Geração (época) da interpretação de margem por conta/exchange/mercado.

Metadado de CONCORRÊNCIA — não é um segundo orçamento nem um motor de risco.

O problema que ele resolve: a soma de margem reservada só conta intenções ainda
pendentes. Quando uma intenção vira posição (`real_trade_id` preenchido), essa
parcela SAI da soma; uma carteira lida ANTES dessa transição ainda parece ter
saldo livre e autorizaria uma segunda proposta pelo mesmo dinheiro. Reduzir TTL
não resolve, porque o problema não é tempo — é a mudança local de interpretação.

Contrato: todo escritor que altere a margem reservada/consumida representada no
banco incrementa `generation` NA MESMA TRANSAÇÃO da mudança. Uma carteira
observada carrega a geração vigente; na admissão, geração diferente significa
observação SUPERADA e a proposta é negada sem POST.

Limite externo assumido e documentado: mudanças feitas pelo operador direto na
corretora NÃO incrementam esta geração. Por isso a leitura fresca continua
obrigatória e a janela externa residual permanece — não há atomicidade com a
exchange.
"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class AccountMarginEpoch(Base):
    __tablename__ = "account_margin_epochs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_scope: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str] = mapped_column(String(20))
    market: Mapped[str] = mapped_column(String(20))
    #: Contador MONOTÔNICO da interpretação FINANCEIRA. Nunca volta; nunca é
    #: relógio.
    generation: Mapped[int] = mapped_column(BigInteger, default=0)
    #: Contador MONOTÔNICO da VALIDAÇÃO MANUAL daquela conta. Finalidade
    #: independente da financeira: renovar prova manual não mexe no contador
    #: financeiro, e carteira financeira recente não mascara falha manual.
    manual_validation_generation: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False)
    #: Estado DURÁVEL de validação (não é feature flag): linha nova nasce
    #: BLOQUEADA e só uma leitura COMPLETA de conta, commitada, libera.
    manual_validation_blocked: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_margin_epoch_identity", "account_scope", "exchange", "market",
              unique=True),
    )
