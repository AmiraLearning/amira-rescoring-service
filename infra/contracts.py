from pydantic import BaseModel


class UpdateActivityPayload(BaseModel):
    activityId: str


class UpdateActivityData(BaseModel):
    updateActivity: UpdateActivityPayload


class UpdateActivityResponse(BaseModel):
    data: UpdateActivityData
