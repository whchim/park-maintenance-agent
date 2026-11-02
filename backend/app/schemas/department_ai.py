"""模型只能填写这些建议字段，不能返回业务状态或数据库操作。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class ExtractedFact(ModelOutput):
    value: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=1000)


class ServiceReview(ModelOutput):
    location: ExtractedFact | None
    issue_type: ExtractedFact | None
    follow_up_question: str = Field(max_length=500)


class InvestmentReview(ModelOutput):
    customer_name: ExtractedFact | None
    contact_name: ExtractedFact | None
    contact_phone: ExtractedFact | None
    intent_level: Literal["A", "B", "C"] | None
    intent_reason: str = Field(max_length=500)
    follow_up_subject: str = Field(min_length=1, max_length=200)


class EngineeringReview(ModelOutput):
    recommended_personnel: str | None = Field(max_length=100)
    reason: str = Field(min_length=1, max_length=600)
