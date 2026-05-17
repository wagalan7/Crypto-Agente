"""BrandBrain — single source of strategic truth per client.

Aggregates: client briefing, persona, primary product, recent insights,
recent winning content, knowledge base, weekly focus.

Every strategic agent (auto creator, weekly brain, sales strategist, etc)
calls BrandBrain.build(client_id) instead of stitching context manually.
This is what makes outputs feel intelligent — every decision sees the whole.
"""
from sqlalchemy.orm import Session
from collections import Counter
from models import (
    Client, Persona, Product, KnowledgeItem, Insight, WeeklyBrain,
    ContentPiece, MetricsSnapshot, AgentMemory,
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

        # --- Knowledge (AI digests preferred over raw text — denser per token)
        items = self.db.query(KnowledgeItem).filter(KnowledgeItem.client_id == client_id).order_by(KnowledgeItem.created_at.desc()).limit(8).all()
        if items:
            kb = []
            budget = max_knowledge_chars
            voice_words: list[str] = []
            for it in items:
                # Mark as used (cheap — single field bump)
                from datetime import datetime as _dt
                it.use_count = (it.use_count or 0) + 1
                it.last_used_at = _dt.utcnow()
                # Prefer summary + insights over raw content
                if it.summary or it.key_insights:
                    block = f"  [{it.source_type}] {it.title}"
                    if it.summary:
                        block += f"\n  RESUMO: {it.summary[:300]}"
                    if it.key_insights:
                        insights_txt = " · ".join(it.key_insights[:5])[:400]
                        block += f"\n  IDEIAS-CHAVE: {insights_txt}"
                    kb.append(block)
                    budget -= len(block)
                else:
                    snippet = (it.content or "")[:500]
                    if len(snippet) > budget:
                        snippet = snippet[:budget]
                    if snippet:
                        kb.append(f"  [{it.source_type}] {it.title}\n  {snippet}")
                        budget -= len(snippet)
                # Aggregate voice signals across items
                for vs in (it.voice_signals or []):
                    if vs and vs not in voice_words:
                        voice_words.append(vs)
                if budget <= 0:
                    break
            try:
                self.db.commit()  # persist use_count bumps; safe — read-only context build
            except Exception:
                self.db.rollback()
            sections.append("BASE DE CONHECIMENTO DO CRIADOR (já digerida pela IA):\n" + "\n".join(kb))
            if voice_words:
                sections.append("VOZ DO CRIADOR (palavras/expressões a reusar):\n  " + " · ".join(voice_words[:20]))

        # --- Persona refinements (user edits — high signal)
        if persona and (persona.user_refinements or []):
            recent = (persona.user_refinements or [])[-5:]
            lines = ["AJUSTES MANUAIS DO USUÁRIO NA PERSONA (preserve esta direção):"]
            for r in recent:
                lines.append(f"  - [{r.get('field')}] {r.get('note') or ''}")
            sections.append("\n".join(lines))

        # --- Winning content patterns
        winners = self._winning_patterns(client_id)
        if winners:
            sections.append("PADRÕES VENCEDORES (últimos posts performáticos):\n" + winners)

        # --- Winning hook styles (A/B feedback loop)
        hook_block = self._winning_hook_styles(client_id)
        if hook_block:
            sections.append(hook_block)

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

    def _winning_hook_styles(self, client_id: int) -> str:
        """Closed-loop learning: when the user picks a hook from A/B variations,
        we log the chosen style. Here we count picks per style across the last
        30 selections and tell the IA which angles the brand consistently prefers.
        """
        memos = self.db.query(AgentMemory).filter(
            AgentMemory.client_id == client_id,
            AgentMemory.agent_type == "hook_style_winner",
            AgentMemory.is_active == True,
        ).order_by(AgentMemory.created_at.desc()).limit(30).all()
        if len(memos) < 2:
            return ""
        style_counts = Counter(m.memory_key for m in memos if m.memory_key)
        if not style_counts:
            return ""
        top = style_counts.most_common(3)
        # Sample winning hooks for the top style (gives the IA concrete examples)
        top_style = top[0][0]
        examples = [m.memory_value for m in memos if m.memory_key == top_style and m.memory_value][:2]
        lines = ["PADRÕES DE HOOK QUE A MARCA ESCOLHEU (feedback A/B):"]
        for style, n in top:
            lines.append(f"  - '{style}' venceu {n}x")
        if examples:
            lines.append("  EXEMPLOS do estilo dominante:")
            for ex in examples:
                lines.append(f"    • {ex[:140]}")
        lines.append("  → Ao gerar hooks novos, priorize esses ângulos.")
        return "\n".join(lines)

    def _winning_patterns(self, client_id: int) -> str:
        """Surface BOTH winners and losers so IA can replicate + avoid.

        We score each snapshot by (shares*3 + saves*2 + comments) — the share+save
        signal that an asset actually moved someone, not just got eyeballs.
        """
        snaps = self.db.query(MetricsSnapshot).filter(
            MetricsSnapshot.client_id == client_id
        ).order_by(MetricsSnapshot.recorded_at.desc()).limit(30).all()
        if not snaps:
            return ""

        def score(m):
            return (m.shares or 0) * 3 + (m.saves or 0) * 2 + (m.comments or 0)

        snaps_scored = [(s, score(s)) for s in snaps if s.content_id]
        if not snaps_scored:
            return ""
        snaps_scored.sort(key=lambda t: t[1], reverse=True)

        top = snaps_scored[:3]
        bottom = snaps_scored[-3:] if len(snaps_scored) >= 6 else []

        # Aggregate winning emotion/format patterns
        from collections import Counter
        top_ids = [m.content_id for m, _ in top]
        top_contents = self.db.query(ContentPiece).filter(ContentPiece.id.in_(top_ids)).all() if top_ids else []
        emo_counter = Counter([c.emotion_used for c in top_contents if c.emotion_used])
        fmt_counter = Counter([c.format for c in top_contents if c.format])
        funnel_counter = Counter([c.funnel_stage for c in top_contents if c.funnel_stage])

        lines = []
        if top_contents:
            lines.append("  ✓ FUNCIONOU MELHOR (replicar padrões):")
            for m, sc in top:
                c = next((x for x in top_contents if x.id == m.content_id), None)
                if c:
                    lines.append(f"    - '{(c.title or '')[:60]}' ({c.format}/{c.platform}) emoção={c.emotion_used or '?'} funil={c.funnel_stage or '?'} | shares={m.shares} saves={m.saves} comments={m.comments}")

        if bottom:
            bot_ids = [m.content_id for m, _ in bottom]
            bot_contents = self.db.query(ContentPiece).filter(ContentPiece.id.in_(bot_ids)).all()
            if bot_contents:
                lines.append("  ✗ FUNCIONOU PIOR (evitar replicar exatamente):")
                for m, sc in bottom:
                    c = next((x for x in bot_contents if x.id == m.content_id), None)
                    if c:
                        lines.append(f"    - '{(c.title or '')[:60]}' ({c.format}/{c.platform}) emoção={c.emotion_used or '?'} | shares={m.shares} saves={m.saves}")

        # Aggregated heuristics
        agg = []
        if emo_counter:
            top_emo = emo_counter.most_common(1)[0]
            agg.append(f"emoção '{top_emo[0]}' aparece em {top_emo[1]}/{len(top_contents)} vencedores")
        if fmt_counter:
            top_fmt = fmt_counter.most_common(1)[0]
            agg.append(f"formato '{top_fmt[0]}' domina topo")
        if funnel_counter:
            top_fn = funnel_counter.most_common(1)[0]
            agg.append(f"estágio '{top_fn[0]}' performa")
        if agg:
            lines.append(f"  → SINAL: {' · '.join(agg)}. Considere replicar.")

        return "\n".join(lines)
