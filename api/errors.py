from flask import jsonify


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        # Extra JSON fields for the response, e.g. linked_count on a 409.
        self.extra = extra


def handle_api_error(err):
    return jsonify(error=str(err), **err.extra), err.status
