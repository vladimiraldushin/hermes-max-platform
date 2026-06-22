try:
    from .adapter import register
except ImportError:  # Allows direct pytest/import from the plugin root.
    from adapter import register

__all__ = ["register"]
