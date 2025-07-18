from flask import jsonify
from flexmeasures.data.schemas.utils import FMValidationError
from webargs.multidictproxy import MultiDictProxy
from webargs import ValidationError
from webargs.flaskparser import parser

"""
Utils for argument parsing (we use webargs),
including error handling.
"""


@parser.error_handler
def handle_error(error, req, schema, *, error_status_code, error_headers):
    """Replacing webargs's error parser, so we can throw custom Exceptions."""
    if error.__class__ == ValidationError:
        # re-package all marshmallow's validation errors as our own kind (see below)
        raise FMValidationError(message=error.messages)
    raise error


def validation_error_handler(error: FMValidationError):
    """Handles errors during parsing.
    Aborts the current HTTP request and responds with a 422 error.
    FMValidationError attributes "result" and "status" are packaged in the response.
    """
    status_code = 422
    response_data = dict(message=error.messages)
    if hasattr(error, "result"):
        response_data["result"] = error.result
    if hasattr(error, "status"):
        response_data["status"] = error.status
    response = jsonify(response_data)
    response.status_code = status_code
    return response


@parser.location_loader("args_and_json")
def load_data(request, schema):
    """
    We allow parameters to come from URL path, GET args and POST JSON,
    as validators can be attached to any of them.
    """

    # GET args (i.e. query parameters, such as https://flexmeasures.io/?id=5)
    newdata = request.args.copy()

    # View args (i.e. path parameters, such as the `/assets/<id>` endpoint)
    path_params = request.view_args
    # Avoid clashes such as visiting https://flexmeasures.io/assets/4/?id=5 on the /assets/<id> endpoint
    for key in path_params:
        if key in newdata:
            raise FMValidationError(message=f"{key} already set in the URL path")
    newdata.update(path_params)

    if request.mimetype == "application/json" and request.method == "POST":
        json_params = request.get_json()
        # Avoid clashes
        for key in json_params:
            if key in newdata:
                raise FMValidationError(
                    message=f"{key} already set in the URL path or query parameters"
                )
        newdata.update(json_params)
    return MultiDictProxy(newdata, schema)
