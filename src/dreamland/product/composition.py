from typing import NoReturn


class ProductCompositionUnavailable(RuntimeError):
    """Raised when this build cannot safely compose the product runtime."""


def compose_product_runtime() -> NoReturn:
    """Fail closed until a complete product runtime can be composed."""
    raise ProductCompositionUnavailable(
        "Product API support is not available in this build; "
        "disable [product_api] enabled. No product listener was started."
    )
