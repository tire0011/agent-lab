"""Canonical version for the Nexus application."""

VERSION = "0.2.0"


def version_tuple(version: str = VERSION) -> tuple[int, ...]:
    """Return a dotted numeric version as a comparable tuple of ints."""
    parts = version.strip().split(".")
    if not all(part.isdigit() for part in parts):
        raise ValueError(f"invalid version: {version!r}")
    return tuple(int(part) for part in parts)
