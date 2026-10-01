from flask import jsonify


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def handle_api_error(err):
    return jsonify(error=str(err)), err.status
