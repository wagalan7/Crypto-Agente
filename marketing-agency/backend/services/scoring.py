from sqlalchemy.orm import Session
from models import Client, MetricsSnapshot, ContentPiece
from datetime import datetime, timedelta


class AuthorityScorer:
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
        if not metrics:
            return 0.0

        total_views = sum(m.views or 0 for m in metrics)
        total_shares = sum(m.shares or 0 for m in metrics)
        total_saves = sum(m.saves or 0 for m in metrics)
        total_comments = sum(m.comments or 0 for m in metrics)
        avg_retention = sum(m.retention_rate or 0 for m in metrics) / len(metrics)
        avg_ctr = sum(m.ctr or 0 for m in metrics) / len(metrics)

        published_count = (
            self.db.query(ContentPiece)
            .filter(
                ContentPiece.client_id == client_id,
                ContentPiece.status == "published",
                ContentPiece.published_at >= thirty_days_ago,
            )
            .count()
        )

        # Weighted score components (0-100)
        reach_score = min(total_views / 10000 * 20, 20)
        engagement_score = min((total_shares * 5 + total_saves * 3 + total_comments) / 500 * 25, 25)
        retention_score = avg_retention / 100 * 25
        consistency_score = min(published_count / 20 * 20, 20)
        conversion_score = min(avg_ctr / 10 * 10, 10)

        score = reach_score + engagement_score + retention_score + consistency_score + conversion_score
        return round(min(score, 100), 1)

    def update(self, client_id: int) -> float:
        score = self.compute(client_id)
        client = self.db.query(Client).filter(Client.id == client_id).first()
        if client:
            client.authority_score = score
            self.db.commit()
        return score
