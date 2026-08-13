from __future__ import annotations

from pydantic import BaseModel, ConfigDict

class SchemaBase(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        use_enum_values=True,
        arbitrary_types_allowed=True,
    )
