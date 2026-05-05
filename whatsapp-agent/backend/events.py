"""
Sistema de eventos em tempo real via Server-Sent Events (SSE).
Permite que o painel web receba notificações sem precisar recarregar.
"""
from __future__ import annotations
import asyncio
import json
from collections import defaultdict
from datetime import datetime

# { tenant_id: [Queue, Queue, ...] }  — uma fila por cliente conectado
_subscribers: dict[int, list[asyncio.Queue]] = defaultdict(list)


def _now() -> str:
    return datetime.now().strftime("%H:%M")


async def publish(tenant_id: int, event_type: str, data: dict):
    """Publica um evento para todos os clientes conectados do tenant."""
    payload = json.dumps({"type": event_type, "data": data, "time": _now()})
    dead = []
    for q in _subscribers[tenant_id]:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers[tenant_id].remove(q)


async def subscribe(tenant_id: int):
    """Generator SSE — yield eventos enquanto o cliente estiver conectado."""
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers[tenant_id].append(q)
    try:
        # Evento inicial de conexão
        yield f"data: {json.dumps({'type': 'connected', 'time': _now()})}\n\n"
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=25)
                yield f"data: {payload}\n\n"
            except asyncio.TimeoutError:
                # Keep-alive ping a cada 25s
                yield ": ping\n\n"
    finally:
        try:
            _subscribers[tenant_id].remove(q)
        except ValueError:
            pass
