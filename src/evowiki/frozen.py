from types import MappingProxyType
from typing import Annotated, Any

from pydantic import AfterValidator, WrapSerializer


def ro_dict(t: Any) -> Any:
    """A dict type validated as usual, then frozen into a read-only MappingProxyType.

    Serializes back to a plain dict so model_dump()/model_dump_json() round-trip.
    """
    return Annotated[t, AfterValidator(MappingProxyType), WrapSerializer(lambda v, h: h(dict(v)))]
