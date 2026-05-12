from pydantic import BaseModel
from typing import Optional


class ProductInput(BaseModel):
    produto: str
    preco: str
    publico: str
    objetivo: str
    plataforma: str
    tom_de_voz: str
    pagina_vendas: Optional[str] = None
    orcamento: Optional[str] = None


class AgencyOutput(BaseModel):
    estrategia: Optional[str] = None
    copy: Optional[str] = None
    conteudo: Optional[str] = None
    criativos: Optional[str] = None
    ads: Optional[str] = None
    automacao: Optional[str] = None
    publicacao: Optional[str] = None
