from .registries import ALL


def build(enabled: dict) -> list:
    """Instantiate the sources switched on in config."""
    return [cls() for key, cls in ALL.items() if enabled.get(key)]
