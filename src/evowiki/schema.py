"""The product's names for the types it uses from the frozen research kernel (architecture audit Q8).

`content.models`, `content.graph` and `timeutil` are in the simulator's frozen closure (their hashes
are pinned by the closed experiments), so the product never imports them directly: it imports
these names from here (tests/test_product_schema.py enforces it). They are the kernel's own
objects, not copies, so values stored or passed between the product and the content loader stay
one type. When the product needs to change one of them, give that one type a product definition
here; the frozen files stay as they are.
"""

from evowiki.content.graph import ContentGraph
from evowiki.content.models import PUBLISHABLE, Lang, Segment
from evowiki.timeutil import days_between

__all__ = ["PUBLISHABLE", "ContentGraph", "Lang", "Segment", "days_between"]
