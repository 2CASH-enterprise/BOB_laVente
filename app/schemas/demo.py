from pydantic import BaseModel, Field


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
    # Lot 33 : messages fixes envoyés après la réponse (ex. confirmation de rendez-vous), comme sur WhatsApp.
    extra_messages: list[str] = []


class DemoPromoteRequest(BaseModel):
    email: str
    password: str
    full_name: str = Field(min_length=1, max_length=255)  # lot 33 : comme la vraie inscription


class DemoPromoteResponse(BaseModel):
    access_token: str
