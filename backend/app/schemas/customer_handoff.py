from pydantic import BaseModel, ConfigDict, Field


class HandoffDocument(BaseModel):
    """正式模板尚未提供：这些是演示联系单字段，不代表已校验真实合同。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    contract_no: str = Field(default="", max_length=100)
    contract_version: str = Field(default="", max_length=100)
    contact_sheet_ref: str = Field(default="", max_length=1000)
    contact_sheet_version: str = Field(default="", max_length=100)
    location: str = Field(default="", max_length=200)
    contact_name: str = Field(default="", max_length=100)
    contact_phone: str = Field(default="", max_length=100)
    service_requirements: str = Field(default="", max_length=3000)
    agreements: str = Field(default="", max_length=3000)


class HandoffSaveRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    document: HandoffDocument


class HandoffSubmitRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    expected_revision: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=100)
    contract_signed: bool
    idempotency_key: str = Field(min_length=1, max_length=100)


class HandoffDecisionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    expected_revision: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=100)
    accepted: bool
    comment: str = Field(min_length=1, max_length=3000)
    idempotency_key: str = Field(min_length=1, max_length=100)
