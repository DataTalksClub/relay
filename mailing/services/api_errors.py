class ApiValidationError(Exception):
    def __init__(self, errors, *, status_code=400):
        self.errors = errors
        self.status_code = status_code
        super().__init__("api_validation_error")


def isoformat(value):
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")
