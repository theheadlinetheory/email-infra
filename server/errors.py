"""Structured error types and HTTP mapping."""


class APIError(Exception):
    """Base error for all external API failures."""
    def __init__(self, code, message, status=502):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


class SmartLeadError(APIError):
    def __init__(self, message, status=502):
        super().__init__("SMARTLEAD_ERROR", message, status)


class ZapmailError(APIError):
    def __init__(self, message, status=502):
        super().__init__("ZAPMAIL_ERROR", message, status)


class RegistrarError(APIError):
    def __init__(self, message, status=502):
        super().__init__("REGISTRAR_ERROR", message, status)


class SheetsError(APIError):
    def __init__(self, message, status=502):
        super().__init__("SHEETS_ERROR", message, status)


class ValidationError(APIError):
    def __init__(self, message):
        super().__init__("VALIDATION_ERROR", message, status=400)


def error_dict(code, message):
    """Standard error dict for JSON responses."""
    return {"code": code, "message": message}
