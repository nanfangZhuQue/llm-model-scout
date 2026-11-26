from ..domain import ErrorCategory


def error_message(data: object) -> str:
    if not isinstance(data, dict):
        return "Non-standard error response"
    error = data.get("error", data)
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code", error.get("type"))
        return ": ".join(v for v in (code, message) if isinstance(v, str)) or "API error"
    return "API error"


def classify_error(status: int, data: object, stage: str = "verify") -> ErrorCategory:
    exact = {
        401: ErrorCategory.UNAUTHORIZED,
        403: ErrorCategory.FORBIDDEN,
        402: ErrorCategory.PAYMENT_REQUIRED,
        429: ErrorCategory.RATE_LIMITED,
    }
    if status in exact:
        return exact[status]
    if 500 <= status <= 599:
        return ErrorCategory.SERVER_ERROR
    if status == 200:
        return ErrorCategory.BAD_RESPONSE
    error = data.get("error", {}) if isinstance(data, dict) else {}
    code = error.get("code", error.get("type", "")) if isinstance(error, dict) else ""
    message = error_message(data).lower()
    if code in ("insufficient_quota", "insufficient_balance", "credit_balance_too_low"):
        return ErrorCategory.PAYMENT_REQUIRED
    if "credit balance is too low" in message or "insufficient balance" in message:
        return ErrorCategory.PAYMENT_REQUIRED
    if stage == "verify" and (
        code == "model_not_found"
        or ("model" in message and ("does not exist" in message or "not found" in message))
    ):
        return ErrorCategory.MODEL_NOT_FOUND
    return ErrorCategory.UNKNOWN_ERROR
