"""Deployment-configured namespace depth; no organization-specific defaults."""
import os


def is_nested_product_namespace(namespace: str) -> bool:
    configured = {
        item.strip().casefold()
        for item in os.getenv("PRODUCT_NESTED_NAMESPACES", "").split(",")
        if item.strip()
    }
    return namespace.casefold() in configured
