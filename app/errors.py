class BridgeError(RuntimeError):
    """Base error returned by the bridge."""


class RemoteAPIError(BridgeError):
    def __init__(self, message: str, status_code: int | None = None, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class PolicyError(BridgeError):
    """Operation blocked by deterministic server-side policy."""
