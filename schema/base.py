from __future__ import annotations

from pydantic import BaseModel, ConfigDict

class SchemaBase(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        # Schema methods compare enum members and access `.value`; preserving
        # members is therefore required at runtime.
        use_enum_values=False,
        arbitrary_types_allowed=True,
    )
