from pydantic import BaseModel, ConfigDict, Field


class FollowupSettingsResponse(BaseModel):
    enabled: bool
    first_followup_hours: int
    first_message: str
    second_followup_hours: int
    second_message: str

    model_config = ConfigDict(from_attributes=True)


class FollowupSettingsUpdate(BaseModel):
    enabled: bool | None = None
    first_followup_hours: int | None = Field(default=None, ge=1)
    first_message: str | None = None
    second_followup_hours: int | None = Field(default=None, ge=1)
    second_message: str | None = None
