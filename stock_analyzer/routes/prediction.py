from __future__ import annotations

from flask import Blueprint

from .common import int_arg, json_result, services


bp = Blueprint("prediction", __name__)


@bp.route("/api/stock-prediction/<code>")
def stock_prediction(code: str):
    return json_result(
        services().stock_prediction_payload(code)
    )


@bp.route("/api/stock-prediction/stance-validation")
def stock_prediction_stance_validation():
    days = int_arg("days", 120, minimum=1, maximum=500)
    return json_result(services().stock_prediction_stance_validation(days))


@bp.route("/api/stock-prediction/stance-validation/update", methods=["POST"])
def stock_prediction_stance_validation_update():
    days = int_arg("days", 120, minimum=1, maximum=500)
    return json_result(services().stock_prediction_stance_validation_update(days))

