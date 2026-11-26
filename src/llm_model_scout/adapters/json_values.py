from decimal import Decimal, DecimalException
from json import loads


def _unique_properties(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON property")
    return result


def _reject_constant(_: str):
    raise ValueError("Non-JSON numeric constant")


def load_probe_json(text: str | bytes | bytearray) -> object:
    try:
        return loads(
            text,
            parse_int=Decimal,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_properties,
        )
    except DecimalException:
        raise ValueError("Invalid JSON number") from None


def is_json_integer(value: object) -> bool:
    return type(value) is int or (
        isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value()
    )
