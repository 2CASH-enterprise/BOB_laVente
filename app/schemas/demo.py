from pydantic import BaseModel


class DemoCreateResponse(BaseModel):
    tenant_id: str
    demo_token: str
    imported: int
    updated: int
    failed: int
    available: int
    unavailable: int


class DemoChatRequest(BaseModel):
    message: str


class DemoChatResponse(BaseModel):
    reply: str


class DemoPromoteRequest(BaseModel):
    email: str
    password: str


class DemoPromoteResponse(BaseModel):
    access_token: str
