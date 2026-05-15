from sqlalchemy.orm import Session
from models import Client, AgentMemory, ContentPiece, MetricsSnapshot
from typing import Optional


class MemoryService:
    def __init__(self, db: Session):
        self.db = db

    def build_client_context(self, client_id: int) -> str:
        client = self.db.query(Client).filter(Client.id == client_id).first()
        if not client:
            return ""

        parts = [
            f"NOME: {client.name}",
            f"NICHO: {client.niche or 'Não definido'}",
            f"PÚBLICO-ALVO: {client.target_audience or 'Não definido'}",
            f"TOM DE VOZ: {client.tone or 'Não definido'}",
            f"PERSONALIDADE: {client.personality or 'Não definido'}",
            f"POSICIONAMENTO: {client.positioning or 'Não definido'}",
            f"OBJETIVOS: {', '.join(client.goals or [])}",
            f"PLATAFORMAS: {', '.join(client.platforms or [])}",
            f"SCORE DE AUTORIDADE: {client.authority_score:.1f}/100",
        ]

        memories = self.db.query(AgentMemory).filter(
            AgentMemory.client_id == client_id,
            AgentMemory.is_active == True,
        ).all()
        if memories:
            parts.append("\nMEMÓRIA CONTEXTUAL:")
            for m in memories:
                parts.append(f"  [{m.agent_type}] {m.memory_key}: {m.memory_value}")

        return "\n".join(parts)

    def build_winning_patterns(self, client_id: int) -> str:
        recent_metrics = (
            self.db.query(MetricsSnapshot)
            .filter(MetricsSnapshot.client_id == client_id)
            .order_by(MetricsSnapshot.recorded_at.desc())
            .limit(20)
            .all()
        )
        if not recent_metrics:
            return ""

        top = sorted(
            recent_metrics,
            key=lambda m: (m.views or 0) + (m.shares or 0) * 5 + (m.saves or 0) * 3,
            reverse=True,
        )[:3]

        lines = []
        for m in top:
            content = self.db.query(ContentPiece).filter(ContentPiece.id == m.content_id).first()
            if content:
                lines.append(
                    f"- [{content.format}/{content.platform}] {content.title} — "
                    f"{m.views} views, {m.retention_rate:.0f}% retenção, {m.shares} shares"
                )
        return "\n".join(lines)

    def store(self, client_id: int, agent_type: str, key: str, value: str) -> None:
        existing = (
            self.db.query(AgentMemory)
            .filter(
                AgentMemory.client_id == client_id,
                AgentMemory.agent_type == agent_type,
                AgentMemory.memory_key == key,
            )
            .first()
        )
        if existing:
            existing.memory_value = value
        else:
            self.db.add(AgentMemory(client_id=client_id, agent_type=agent_type, memory_key=key, memory_value=value))
        self.db.commit()

    def get(self, client_id: int, agent_type: str, key: str) -> Optional[str]:
        m = (
            self.db.query(AgentMemory)
            .filter(
                AgentMemory.client_id == client_id,
                AgentMemory.agent_type == agent_type,
                AgentMemory.memory_key == key,
                AgentMemory.is_active == True,
            )
            .first()
        )
        return m.memory_value if m else None
