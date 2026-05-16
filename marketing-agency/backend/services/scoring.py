from sqlalchemy.orm import Session
from models import Client, MetricsSnapshot, ContentPiece, Persona, Product, KnowledgeItem, Insight, AuthorityScoreSnapshot
from datetime import datetime, timedelta


class AuthorityScorer:
    """Scores authority on 0-100 from two pillars:
    - Performance signals (metrics, frequency) — 70 pts
    - Strategic maturity signals (persona, produto, base, insights resolvidos) — 30 pts
    """

    def __init__(self, db: Session):
        self.db = db

    def compute(self, client_id: int) -> float:
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        metrics = (
            self.db.query(MetricsSnapshot)
            .filter(
                MetricsSnapshot.client_id == client_id,
                MetricsSnapshot.recorded_at >= thirty_days_ago,
            )
            .all()
        )

        # ---- Performance (max 70) ----
        if metrics:
            total_views = sum(m.views or 0 for m in metrics)
            total_shares = sum(m.shares or 0 for m in metrics)
            total_saves = sum(m.saves or 0 for m in metrics)
            total_comments = sum(m.comments or 0 for m in metrics)
            avg_retention = sum(m.retention_rate or 0 for m in metrics) / len(metrics)
            avg_ctr = sum(m.ctr or 0 for m in metrics) / len(metrics)
            reach_score = min(total_views / 10000 * 15, 15)
            engagement_score = min((total_shares * 5 + total_saves * 3 + total_comments) / 500 * 20, 20)
            retention_score = avg_retention / 100 * 20
            conversion_score = min(avg_ctr / 10 * 5, 5)
            perf = reach_score + engagement_score + retention_score + conversion_score
        else:
            perf = 0.0

        published_count = (
            self.db.query(ContentPiece)
            .filter(
                ContentPiece.client_id == client_id,
                ContentPiece.status == "published",
                ContentPiece.published_at >= thirty_days_ago,
            )
            .count()
        )
        consistency_score = min(published_count / 20 * 10, 10)  # max 10
        perf += consistency_score

        # ---- Strategic maturity (max 30) ----
        maturity = 0.0
        if self.db.query(Persona).filter(Persona.client_id == client_id).first():
            maturity += 7
        if self.db.query(Product).filter(Product.client_id == client_id, Product.is_active == True).first():
            maturity += 5
        if self.db.query(Product).filter(Product.client_id == client_id, Product.is_primary == True, Product.is_active == True).first():
            maturity += 3  # bonus for primary set
        kb_count = self.db.query(KnowledgeItem).filter(KnowledgeItem.client_id == client_id).count()
        maturity += min(kb_count, 5)  # 1 pt per KB item up to 5
        # Insights resolvidos (dismissed) — sinal de que o usuário está agindo sobre feedback
        resolved = self.db.query(Insight).filter(Insight.client_id == client_id, Insight.is_dismissed == True).count()
        maturity += min(resolved / 4, 5)  # up to 5 pts (20 resolvidos = 5 pts)
        # Conteúdos com reasoning preenchido — indica uso do auto-criador estratégico
        reasoned = self.db.query(ContentPiece).filter(
            ContentPiece.client_id == client_id,
            ContentPiece.objective_reasoning.isnot(None),
        ).count()
        maturity += min(reasoned / 4, 5)

        score = min(perf + maturity, 100)
        return round(score, 1)

    def update(self, client_id: int) -> float:
        score = self.compute(client_id)
        client = self.db.query(Client).filter(Client.id == client_id).first()
        if client:
            client.authority_score = score
            # Daily snapshot: only one row per UTC day to keep the timeline clean
            today = datetime.utcnow().date()
            existing = (
                self.db.query(AuthorityScoreSnapshot)
                .filter(AuthorityScoreSnapshot.client_id == client_id)
                .order_by(AuthorityScoreSnapshot.recorded_at.desc())
                .first()
            )
            if existing and existing.recorded_at.date() == today:
                existing.score = score
                existing.recorded_at = datetime.utcnow()
            else:
                self.db.add(AuthorityScoreSnapshot(client_id=client_id, score=score))
            self.db.commit()
        return score
