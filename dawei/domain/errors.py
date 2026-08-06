"""Typed errors shared by V2 modules."""


class DaweiError(RuntimeError):
    """Base error for expected V2 failures."""


class ScrapeError(DaweiError):
    """A site did not produce one trustworthy result."""


class ConfigurationError(ScrapeError):
    """Site configuration is missing, malformed, or ambiguous."""


class ValidationError(DaweiError):
    """A domain value violates a strict data contract."""


class CacheError(DaweiError):
    """Base error for recent-period cache operations."""


class CacheFormatError(CacheError):
    """The cache file does not satisfy its schema."""


class CacheRollbackError(CacheError):
    """A write would move the cache window backwards."""


class CacheConflictError(CacheError):
    """The same site and issue have conflicting trusted data."""


class CacheLockTimeoutError(CacheError):
    """Another process held the cache lock for too long."""
