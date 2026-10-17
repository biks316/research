from typing import ClassVar

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer
from pydantic.alias_generators import to_camel


class RequestModel(BaseModel):
    """Request bodies: camelCase on the wire, unknown fields rejected (additionalProperties: false)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ResponseModel(BaseModel):
    """Response bodies: camelCase on the wire.

    Fields listed in `omit_when_none` are optional-but-not-nullable in the spec, so they
    are left out of the JSON instead of being sent as null.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    omit_when_none: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _omit_none(self, handler: SerializerFunctionWrapHandler):  # no return annotation: keeps the JSON schema
        data = handler(self)
        if not self.omit_when_none:
            return data
        omit_keys: set[str] = set()
        for name in self.omit_when_none:
            field = type(self).model_fields[name]
            omit_keys.update({name, field.alias or name})
        return {k: v for k, v in data.items() if not (k in omit_keys and v is None)}
