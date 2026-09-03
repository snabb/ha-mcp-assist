"""OpenAPI schema conversion compatibility."""

try:
    from probatio import to_openapi
except ImportError:  # pragma: no cover - older Home Assistant versions
    from voluptuous_openapi import convert as to_openapi

__all__ = ["to_openapi"]
