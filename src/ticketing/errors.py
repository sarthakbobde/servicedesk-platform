"""One error shape (A11.1) and the error catalogue (A11.2)."""

STATUS = {
    "not_authenticated": 401,
    "login_failed": 401,
    "password_change_required": 403,
    "forbidden": 403,
    "not_found": 404,
    "invalid_state": 409,
    "already_claimed": 409,
    "reopen_window_closed": 409,
    "idempotency_conflict": 409,
    "reopen_blocked": 409,
    "children_open": 409,
    "conflict": 409,
    "validation_failed": 422,
    "resolver_not_in_team": 422,
    "inactive_reference": 422,
    "no_change": 422,
    "rate_limited": 429,
}

DEFAULT_MESSAGES = {
    "not_authenticated": "Sign in first.",
    "login_failed": "Email or password is not correct, or the account is temporarily locked.",
    "password_change_required": "Change your password before continuing.",
    "forbidden": "You are not allowed to do this.",
    "not_found": "Not found.",
    "invalid_state": "This ticket was updated. Reload to see the latest.",
    "already_claimed": "Someone else claimed this ticket first.",
    "reopen_window_closed": "The reopen window has passed. Raise a new ticket.",
    "idempotency_conflict": "This Idempotency-Key was used with a different request.",
    "reopen_blocked": "This ticket cannot be reopened.",
    "children_open": "Finish its child tickets first.",
    "validation_failed": "Some fields are not valid.",
    "resolver_not_in_team": "The person is not an active resolver of the owning team.",
    "inactive_reference": "A referenced item is inactive or belongs to another project.",
    "no_change": "The new value equals the current one.",
    "rate_limited": "Try again in a few minutes.",
}


class AppError(Exception):
    def __init__(self, code: str, message: str | None = None, fields: dict | None = None, status: int | None = None):
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, code)
        self.fields = fields
        self.status = status or STATUS.get(code, 400)
        super().__init__(self.message)

    def to_dict(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.fields:
            err["fields"] = self.fields
        return {"error": err}


def not_found() -> AppError:
    return AppError("not_found")


def validation(fields: dict, message: str | None = None) -> AppError:
    return AppError("validation_failed", message, fields=fields)


def require_text(fields: dict, name: str, value, max_len: int = 2000, min_len: int = 1) -> str | None:
    """Text limits (A11.1): whitespace-only counts as empty."""
    if value is None or not str(value).strip():
        fields[name] = "required"
        return None
    value = str(value).strip()
    if len(value) < min_len or len(value) > max_len:
        fields[name] = f"length {min_len} to {max_len}"
        return None
    return value
