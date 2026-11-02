from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, StringConstraints


AttachmentId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]


class AttachmentResponse(BaseModel):
    id: str
    name: str
    media_type: str
    size_bytes: int
    width: int
    height: int
    purpose: str
    source_channel: str
    url: str
    workflow_id: str | None
    ticket_id: int | None
    repair_record_id: int | None
    created_at: datetime
