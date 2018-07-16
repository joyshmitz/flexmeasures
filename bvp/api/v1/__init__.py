from flask import Flask, Blueprint


# The api blueprint. It is registered with the Flask app (see register_at)
bvp_api = Blueprint("bvp_api_v1", __name__)


def register_at(app: Flask):
    """This can be used to register this blueprint together with other api-related things"""

    import bvp.api.v1.routes  # noqa: F401 this is necessary to load the endpoints

    app.register_blueprint(bvp_api, url_prefix="/api/v1")