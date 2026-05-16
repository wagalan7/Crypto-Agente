"""BrandBrain — single source of strategic truth per client.

Aggregates: client briefing, persona, primary product, recent insights,
recent winning content, knowledge base, weekly focus.

Every strategic agent (auto creator, weekly brain, sales strategist, etc)
calls BrandBrain.build(client_id) instead of stitching context manually.
This is what makes outputs feel intelligent — every decision sees the whole.
"""
from sqlalchemy.orm import Session
from models import (
    Client, Persona, Product, KnowledgeItem, Insight, WeeklyBrain,
    ContentPiece, MetricsSnapshot,
)


class BrandBrain:
    def __init__(self, db: Session):
        self.db = db

    def build(self, client_id: int, max_knowledge_chars: int = 2000) -> dict:
        """Return rich strategic context as a dict (structured) and as text (for LLM)."""
        client = self.db.query(Client).filter(Client.id == client_id).first()
        if not client:
            return {"text": "", "data": {}}

        sections = []

        # --- Briefing
        sections.append(self._client_block(client))

        # --- Persona
        persona = self.db.query(Persona).filter(Persona.client_id == client_id).first()
        if persona:
            sections.append(self._persona_block(persona))

        # --- Primary product
        product = self.db.query(Product).filter(
            Product.client_id == client_id,
            Product.is_primary == True,
            Product.is_active == True,
        ).first()
        if not product:
            product = self.db.query(Product).filter(
                Product.client_id == client_id,
                Product.is_active == True,
            ).first()
        if product:
            sections.append(self._product_block(product))

        # --- Recent insights (active)
        insights = self.db.query(Insight).filter(
            Insight.client_id == client_id,
            Insight.is_dismissed == False,
        ).order_by(Insight.created_at.desc()).limit(6).all()
        if insights:
            sections.append("INSIGHTS RECENTES:\n" + "\n".join(
                f"  - [{i.severity}] {i.title}: {i.message}" for i in insights
            ))

        # --- Weekly brain
        wb = self.db.query(WeeklyBrain).filter(WeeklyBrain.client_id == client_id).order_by(WeeklyBrain.generated_at.desc()).first()
        if wb:
            sections.append(self._weekly_block(wb))

        # --- Knowledge (top items, truncated)
        items = self.db.query(KnowledgeItem).filter(KnowledgeItem.client_id == client_id).order_by(KnowledgeItem.created_at.desc()).limit(5).all()
        if items:
            kb = []
            budget = max_knowledge_chars
            for it in items:
                snippet = (it.content or "")[:600]
                if len(snippet) > budget:
                    snippet = snippet[:budget]
                kb.append(f"  [{it.source_type}] {it.title}\n  {snippet}")
                budget -= len(snippet)
                if budget <= 0:
                    break
            sections.append("BASE DE CONHECIMENTO DO CRIADOR:\n" + "\n".join(kb))

        # --- Winning content patterns
        winners = self._winning_patterns(client_id)
        if winners:
            sections.append("PADRÕES VENCEDORES (últimos posts performáticos):\n" + winners)

        text = "\n\n".join(sections)
        return {
            "text": text,
            "data": {
                "client": client,
                "persona": persona,
                "primary_product": product,
                "insights": insights,
                "weekly_brain": wb,
                "knowledge_count": len(items),
            },
        }

    def _client_block(self, c: Client) -> str:
        return "\n".join([
            "BRIEFING DO CRIADOR:",
            f"  Nome: {c.name}",
            f"  Nicho: {c.niche or '-'}",
            f"  Público-alvo: {c.target_audience or '-'}",
            f"  Tom: {c.tone or '-'}",
            f"  Personalidade: {c.personality or '-'}",
            f"  Posicionamento: {c.positioning or '-'}",
            f"  Objetivos: {', '.join(c.goals or [])}",
            f"  Score de autoridade: {c.authority_score:.1f}/100",
        ])

    def _persona_block(self, p: Persona) -> str:
        return "\n".join([
            "PERSONA DA AUDIÊNCIA:",
            f"  Dores: {', '.join(p.pains or [])}",
            f"  Desejos: {', '.join(p.desires or [])}",
            f"  Emoções dominantes: {', '.join(p.emotions or [])}",
            f"  Inseguranças: {', '.join(p.insecurities or [])}",
            f"  Objetivos da audiência: {', '.join(p.audience_goals or [])}",
            f"  Padrões de linguagem: {(p.language_patterns or '')[:300]}",
            f"  Padrões psicológicos: {(p.psychological_patterns or '')[:300]}",
        ])

    def _product_block(self, prod: Product) -> str:
        return "\n".join([
            f"PRODUTO PRINCIPAL: {prod.name} ({prod.type})",
            f"  Preço: {prod.price or '-'}",
            f"  Transformação prometida: {prod.transformation or '-'}",
            f"  Dores que resolve: {', '.join(prod.pains_solved or [])}",
            f"  Desejos que ativa: {', '.join(prod.desires or [])}",
            f"  Objeções comuns: {', '.join(prod.objections or [])}",
            f"  Estágio de consciência alvo: {prod.awareness_stage or '-'}",
            f"  Estágio de funil: {prod.funnel_stage or '-'}",
        ])

    def _weekly_block(self, wb: WeeklyBrain) -> str:
        return "\n".join([
            "FOCO DA SEMANA:",
            f"  {wb.focus or '-'}",
            f"  Prioridades: {', '.join(wb.priorities or [])}",
            f"  Oportunidades: {', '.join(wb.opportunities or [])}",
            f"  Alertas: {', '.join(wb.alerts or [])}",
        ])

    def _winning_patterns(self, client_id: int) -> str:
        snaps = self.db.query(MetricsSnapshot).filter(
            MetricsSnapshot.client_id == client_id
        ).order_by(MetricsSnapshot.recorded_at.desc()).limit(20).all()
        if not snaps:
            return ""
        snaps.sort(key=lambda m: (m.shares + m.saves + m.comments), reverse=True)
        lines = []
        for m in snaps[:3]:
            c = self.db.query(ContentPiece).filter(ContentPiece.id == m.content_id).first()
            if c:
                lines.append(f"  - '{c.title}' ({c.format}/{c.platform}) — emoção: {c.emotion_used or '?'} — shares:{m.shares} saves:{m.saves} comments:{m.comments}")
        return "\n".join(lines)
