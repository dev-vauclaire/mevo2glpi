"""HTTP-wide concerns for the web application.

The MVP does not need an application middleware yet. Cross-cutting HTTP
policies belong in this package when they cannot be expressed safely by the
edge proxy or by a route dependency; business workflows remain in ``services``.
"""

__all__: list[str] = []
