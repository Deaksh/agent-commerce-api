from pydantic import BaseModel

class AuditRequest(BaseModel):
    url: str

class AuditResponse(BaseModel):
    url: str
    score: float
    recommendations: list[str]
    product_info: dict
