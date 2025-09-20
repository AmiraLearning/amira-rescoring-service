from abc import ABC, abstractmethod
from typing import Any, Generic, Self, TypeVar, cast

# JSON-compatible types (primitives and recursive containers)
JsonSerializable = (
    int | float | bool | str | None | list["JsonSerializable"] | dict[str, "JsonSerializable"]
)

T = TypeVar("T")


class JsonSerializer(ABC, Generic[T]):
    @classmethod
    @abstractmethod
    def deserialize(cls, json: JsonSerializable) -> T: ...

    @classmethod
    @abstractmethod
    def serialize(cls, value: T) -> JsonSerializable: ...


class Jsonable(ABC):
    """
    Classes that are JSON serializable provide a class method to retrieve
    the appropriate serializer
    """

    @classmethod
    @abstractmethod
    def serialization(cls) -> JsonSerializer[Self]:
        pass


class IdentitySerializer(JsonSerializer[JsonSerializable]):
    """
    Serializer for types that are naturally JSON representable
    """

    @classmethod
    def deserialize(cls, json: JsonSerializable) -> JsonSerializable:
        """Deserialize JSON data into its original type.

        Args:
            json: JSON data to deserialize

        Returns:
            Deserialized JSON data
        """
        return json

    @classmethod
    def serialize(cls, value: JsonSerializable) -> JsonSerializable:
        """Serialize JSON data into its original type.

        Args:
            value: JSON data to serialize

        Returns:
            Serialized JSON data
        """
        return value


try:
    import pandas as pd

    class DataFrameSerializer(JsonSerializer["pd.DataFrame"]):
        """
        Serializer for Pandas DataFrames whose values are themselves naturally JSON serializable
        Note - indexes are NOT preserved
        """

        @classmethod
        def deserialize(cls, json: JsonSerializable) -> "pd.DataFrame":
            """Deserialize JSON data into a Pandas DataFrame.

            Args:
                json: JSON data to deserialize

            Returns:
                Deserialized Pandas DataFrame
            """
            if not isinstance(json, dict):
                raise ValueError(f"Expected dict for DataFrame deserialization, got {type(json)}")
            return pd.DataFrame.from_dict(cast(dict[Any, Any], json))

        @classmethod
        def serialize(cls, value: "pd.DataFrame") -> JsonSerializable:
            return cast(JsonSerializable, value.to_dict(orient="list"))
except ImportError:
    pass
