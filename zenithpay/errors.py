"""One shared JSON error shape for the whole API."""
from flask import jsonify


def api_error(message: str, code: str, status: int):
    """Flat error envelope: {"error": ..., "code": ...} with an HTTP status."""
    return jsonify({"error": message, "code": code}), status
