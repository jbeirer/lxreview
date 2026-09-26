from enum import StrEnum


class Category(StrEnum):
    UNAVAILABLE = "unavailable"
    AUTH = "authentication_required"
    BUSY = "busy"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol_error"
    ACCESS = "access_failed"
    UNSAFE = "unsafe_operation"
    CONFIG = "configuration_error"


class LXError(Exception):
    def __init__(self, category: Category, message: str):
        self.category = category
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"ok": False, "category": self.category.value, "message": str(self)}
