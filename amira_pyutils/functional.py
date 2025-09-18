from collections.abc import Callable, Generator, Iterable
from functools import reduce
from typing import Any, Final, Generic, TypeVar

from toolz import curry

T = TypeVar("T")
U = TypeVar("U")
FunctionalA = TypeVar("FunctionalA")

Endo = Callable[[FunctionalA], FunctionalA]


@curry  # type: ignore[misc]
def fmap_opt(func: Callable[[T], U], value: T | None) -> U | None:
    """Apply function to optional value using functor semantics.

    Args:
        func: Function to apply to the wrapped value
        value: Optional value to transform

    Returns:
        Transformed optional value, or None if input was None
    """
    return None if value is None else func(value)


@curry  # type: ignore[misc]
def flatmap_opt(func: Callable[[T], U | None], value: T | None) -> U | None:
    """Apply function returning optional to optional value using monadic bind.

    Args:
        func: Function that takes a value and returns an optional
        value: Optional value to transform

    Returns:
        Result of applying func to value, or None if input was None
    """
    return None if value is None else func(value)


@curry  # type: ignore[misc]
def or_else(default: T, value: T | None) -> T:
    """Provide default value for None optional.

    Args:
        default: Value to return if optional is None
        value: Optional value to check

    Returns:
        The value if not None, otherwise the default
    """
    return default if value is None else value


@curry  # type: ignore[misc]
def fold_opt(if_none: Callable[[], U], if_val: Callable[[T], U], value: T | None) -> U:
    """Fold over optional value with handlers for both cases.

    Args:
        if_none: Function to call when value is None
        if_val: Function to call with value when not None
        value: Optional value to fold over

    Returns:
        Result of calling appropriate handler function
    """
    return if_none() if value is None else if_val(value)


def const(value: T) -> Callable[..., T]:
    """Create constant function that always returns the same value.

    Args:
        value: Value to always return

    Returns:
        Function that ignores arguments and returns value
    """

    def constant_func(*args: Any, **kwargs: Any) -> T:
        return value

    return constant_func


@curry  # type: ignore[misc]
def nested_dict_lens(path: list[str], dictionary: dict[str, Any]) -> Any | None:
    """Navigate nested dictionary structure using path of keys.

    Args:
        path: List of keys to traverse in order
        dictionary: Dictionary to navigate

    Returns:
        Value at the end of the path, or None if any key is missing
    """
    if not path:
        return dictionary

    head, *tail = path
    next_dict: dict[str, Any] | None = dictionary.get(head)
    return fmap_opt(func=lambda d: nested_dict_lens(path=tail, dictionary=d), value=next_dict)


@curry  # type: ignore[misc]
def fmap_dict(func: Callable[[Any], Any], dictionary: dict[str, Any]) -> dict[str, Any]:
    """Recursively apply function to all leaf values in nested dictionary.

    Args:
        func: Function to apply to leaf values
        dictionary: Dictionary structure to transform

    Returns:
        Dictionary with same structure but transformed leaf values
    """
    if isinstance(dictionary, dict):
        return {key: fmap_dict(func=func, dictionary=value) for key, value in dictionary.items()}
    return func(dictionary)


class SemiGroup(Generic[FunctionalA]):
    """Algebraic structure with associative binary operation.

    A semigroup provides a binary operation that combines two values of type A
    to produce another value of type A. The operation must be associative:
    (a + b) + c = a + (b + c) for all a, b, c.

    Examples:
        - Integer addition or multiplication
        - List concatenation
        - Function composition for endomorphisms
    """

    def __init__(self, *, operation: Callable[[FunctionalA, FunctionalA], FunctionalA]) -> None:
        """Initialize semigroup with binary operation.

        Args:
            operation: Associative binary function
        """
        self._operation = operation

    @property
    def plus(self) -> Callable[[FunctionalA, FunctionalA], FunctionalA]:
        """Get the semigroup's binary operation.

        Returns:
            The binary operation.
        """
        return self._operation


class Monoid(SemiGroup[FunctionalA]):
    """Semigroup with identity element.

    A monoid extends a semigroup with an identity element (zero) such that:
    - plus(a, zero) = a for all a
    - plus(zero, a) = a for all a

    This enables reduction operations over collections.
    """

    def __init__(
        self,
        *,
        operation: Callable[[FunctionalA, FunctionalA], FunctionalA],
        identity: FunctionalA,
    ) -> None:
        """Initialize monoid with operation and identity element.

        Args:
            operation: Associative binary function
            identity: Identity element for the operation
        """
        super().__init__(operation=operation)
        self._identity = identity

    @property
    def zero(self) -> FunctionalA:
        """Get the monoid's identity element.

        Returns:
            The identity element.
        """
        return self._identity

    def sum(self, *, values: Iterable[FunctionalA]) -> FunctionalA:
        """Reduce collection using monoid operation.

        Args:
            values: Collection of values to reduce

        Returns:
            Result of reducing values with monoid operation
        """
        return reduce(self.plus, values, self.zero)


BOOL_OR_MONOID: Final[Monoid[bool]] = Monoid(operation=lambda x, y: x or y, identity=False)
BOOL_AND_MONOID: Final[Monoid[bool]] = Monoid(operation=lambda x, y: x and y, identity=True)


@curry  # type: ignore[misc]
def fmap_generator(
    func: Callable[[T], U], generator: Generator[T, None, None]
) -> Generator[U, None, None]:
    """Apply function to each element of generator using functor semantics.

    Args:
        func: Function to apply to each generated value
        generator: Generator to transform

    Yields:
        Transformed values from the original generator
    """
    for item in generator:
        yield func(item)
