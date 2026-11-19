"""The shapes that leave — what a client actually receives."""

from collections.abc import Mapping, Sequence
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from papilio.types.enums import FaStrEnum


class BaseOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_obj(cls, model: Any) -> Self:
        return cls.model_validate(model)

    @classmethod
    def from_objs(cls, models: Sequence[Any]) -> list[Self]:
        return [cls.model_validate(model) for model in models]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls.model_validate(data)

    @classmethod
    def from_dicts(cls, data: Sequence[Any]) -> list[Self]:
        return [cls.model_validate(item) for item in data]


class PublicIDOutput(BaseOutput):
    """Expose stored public_id as id through native Pydantic aliasing."""

    id: int = Field(validation_alias="public_id")


class EnumOut(BaseOutput):
    """One enum member as a client should receive it: what to send back, and
    what to show a person."""

    value: str
    label: str

    @classmethod
    def of(cls, enum: type[FaStrEnum]) -> list["EnumOut"]:
        return [cls(value=member.value, label=member.fa) for member in enum]


class EnumGroupOut(BaseOutput):
    """Several enums at once, each under its own name.

    Serve the vocabularies a client needs to render forms from one endpoint,
    and a new member reaches every client the moment it is added — no second
    copy of the list to keep in step::

        return APIResponse.from_data(
            EnumGroupOut.of([OrderStatus, PaymentMethod])
        )
    """

    name: str
    members: list[EnumOut]

    @classmethod
    def of(cls, enums: Sequence[type[FaStrEnum]]) -> list["EnumGroupOut"]:
        return [
            cls(name=enum.__name__, members=EnumOut.of(enum)) for enum in enums
        ]
