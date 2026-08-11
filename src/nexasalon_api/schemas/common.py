from pydantic import BaseModel


class ErrorBody(BaseModel):
    type: str
    message: str
    details: object | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
