_STATUS_VALUES = {
    "ready": "ready",
    "in_progress": "in_progress",
    "done": "done",
    "failed": "failed",
}


def normalize_status(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("status must be a string")
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        raise ValueError("status must not be empty")
    try:
        return _STATUS_VALUES[normalized]
    except KeyError as error:
        raise ValueError(f"unsupported status: {value}") from error
