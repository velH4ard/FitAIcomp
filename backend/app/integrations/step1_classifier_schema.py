from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Step1ClassifierItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    match_type: Literal["exact", "fuzzy", "unknown"]
    confidence: float = Field(..., ge=0, le=1)

    class NutritionPer100gSchema(BaseModel):
        model_config = ConfigDict(extra="forbid")

        calories_kcal: float = Field(..., ge=0)
        protein_g: float = Field(..., ge=0)
        fat_g: float = Field(..., ge=0)
        carbs_g: float = Field(..., ge=0)

    nutrition_per_100g: NutritionPer100gSchema
    default_weight_g: Optional[float] = Field(default=None, gt=0)
    warnings: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("warnings")
    @classmethod
    def _validate_item_warnings(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text or len(text) > 240:
                raise ValueError("warning must be non-empty and <= 240 chars")
            cleaned.append(text)
        return cleaned


class Step1ClassifierResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recognized: bool
    overall_confidence: float = Field(..., ge=0, le=1)
    items: list[Step1ClassifierItemSchema] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("warnings")
    @classmethod
    def _validate_top_warnings(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text or len(text) > 240:
                raise ValueError("warning must be non-empty and <= 240 chars")
            cleaned.append(text)
        return cleaned
