"""Money movement routes: internal transfers, P2P payments, loan repayments."""
from flask import Blueprint, g, jsonify, request

from . import money
from .auth import login_required
from .db import get_db
from .errors import api_error

bp = Blueprint("transfers", __name__, url_prefix="/api")


def _account_id(data, field):
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        return None, api_error(f"{field} is required", f"invalid_{field}", 400)
    return value, None


def _description(data, *, required=True):
    text = str(data.get("description") or "").strip()
    if required and not text:
        return None, api_error("description is required", "invalid_description", 400)
    if len(text) > 200:
        return None, api_error("description must be 200 characters or fewer", "invalid_description", 400)
    return text, None


def _category(data):
    text = str(data.get("category") or "").strip() or None
    if text and len(text) > 40:
        return None, api_error("category must be 40 characters or fewer", "invalid_category", 400)
    return text, None


@bp.post("/transfers")
@login_required
def create_transfer():
    """Move money between your own same-currency checking/savings accounts."""
    data = request.get_json(silent=True) or {}
    source_id, err = _account_id(data, "source_account_id")
    if err:
        return err
    dest_id, err = _account_id(data, "dest_account_id")
    if err:
        return err
    description, err = _description(data)
    if err:
        return err
    category, err = _category(data)
    if err:
        return err

    try:
        result = money.transfer_internal(
            get_db(),
            g.user_id,
            source_id,
            dest_id,
            data.get("amount_cents"),
            description,
            category,
        )
    except money.MoneyError as exc:
        return api_error(exc.message, exc.code, exc.status)
    return jsonify(result), 201


@bp.post("/p2p")
@login_required
def create_p2p():
    """Pay another member by email or account number."""
    data = request.get_json(silent=True) or {}
    source_id, err = _account_id(data, "source_account_id")
    if err:
        return err
    description, err = _description(data)
    if err:
        return err
    category, err = _category(data)
    if err:
        return err
    if category is None:
        category = "p2p"

    recipient_account_id = data.get("recipient_account_id")
    if recipient_account_id is not None and (
        not isinstance(recipient_account_id, int) or isinstance(recipient_account_id, bool)
    ):
        return api_error("recipient_account_id must be an integer", "invalid_recipient_account", 400)

    try:
        result = money.transfer_p2p(
            get_db(),
            g.user_id,
            source_id,
            data.get("recipient"),
            data.get("amount_cents"),
            description,
            category,
            recipient_account_id,
        )
    except money.MoneyError as exc:
        return api_error(exc.message, exc.code, exc.status)
    return jsonify(result), 201


@bp.post("/repayments")
@login_required
def create_repayment():
    """Shrink a loan: debits the source account and the loan together."""
    data = request.get_json(silent=True) or {}
    source_id, err = _account_id(data, "source_account_id")
    if err:
        return err
    loan_id, err = _account_id(data, "loan_account_id")
    if err:
        return err
    description, err = _description(data, required=False)
    if err:
        return err

    try:
        result = money.repay_loan(
            get_db(),
            g.user_id,
            source_id,
            loan_id,
            data.get("amount_cents"),
            description,
        )
    except money.MoneyError as exc:
        return api_error(exc.message, exc.code, exc.status)
    return jsonify(result), 201
