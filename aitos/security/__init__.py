"""Production secret-resolution boundary."""

from .secrets import SecretProvider, build_secret_provider

__all__ = ["SecretProvider", "build_secret_provider"]
