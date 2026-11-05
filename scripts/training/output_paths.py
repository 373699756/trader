"""Keep diagnostic outputs separate from every selected product checkout."""

from pathlib import Path


def RequireIndependentOutput(output: Path, product_checkout: Path) -> None:
    destination = output.expanduser().resolve()
    source = product_checkout.expanduser().resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("training_output_overlaps_product_checkout")
