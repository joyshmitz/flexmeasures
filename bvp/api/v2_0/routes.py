import copy

from flask_security import auth_token_required

from bvp.api.common.utils.api_utils import list_access, append_doc_of
from bvp.api.common.utils.decorators import as_response_type
from bvp.api.common.utils.validators import usef_roles_accepted
from bvp.api.v1 import implementations as v1_implementations
from bvp.api.v1_1 import implementations as v1_1_implementations
from bvp.api.v1_3 import implementations as v1_3_implementations
from bvp.api.v1_3 import routes as v1_3_routes

from bvp.api.v2_0 import bvp_api as bvp_api_v2_0
from bvp.api.v2_0.implementations import assets


# The service listing for this API version (import from previous version or update if needed)
v2_0_service_listing = copy.deepcopy(v1_3_routes.v1_3_service_listing)
v2_0_service_listing["version"] = "2.0"

# Note: For the time being, no (USEF) role access is added to asset endpoints
# TODO: Add role access when multi-tenancy is added
v2_0_service_listing["services"].append(
    {
        "name": "GET /assets",
        "access": [],
        "description": "List owned assets.",
    },
)
v2_0_service_listing["services"].append(
    {
        "name": "POST /assets",
        "access": [],
        "description": "Create an asset.",
    },
)
v2_0_service_listing["services"].append(
    {
        "name": "GET /asset/<id>",
        "description": "Get an asset.",
    },
)
v2_0_service_listing["services"].append(
    {
        "name": "PATCH /assets/<id>",
        "access": [],
        "description": "Edit an asset.",
    },
)
v2_0_service_listing["services"].append(
    {
        "name": "DELETE /assets/<id>",
        "access": [],
        "description": "Delete an asset and its data.",
    },
)


@bvp_api_v2_0.route("/assets", methods=["GET"])
@auth_token_required
def get_assets():
    """API endpoint to get assets.

    .. :quickref: Asset; Download asset list

    This endpoint returns all accessible assets for a given owner.
    The `owner_id` query parameter can be used to set an owner.
    If no owner is set, all accessible assets are returned.
    A non-admin user can only access its own assets.

    **Example response**

    An example of one asset being returned:

    .. sourcecode:: json

        [
            {
                "asset_type": "battery",
                "capacity_in_mw": 2.0,
                "display_name": "Test battery",
                "event_resolution": 10,
                "id": 1,
                "latitude": 10,
                "longitude": 100,
                "market": 1,
                "max_soc_in_mwh": 5,
                "min_soc_in_mwh": 0,
                "name": "Test battery",
                "owner": 2,
                "soc_datetime": "2015-01-01T00:00:00+00:00",
                "soc_in_mwh": 2.5,
                "soc_udi_event_id": 203,
                "unit": "MW"
            }
        ]

    Note that event_resolution is returned as the number of minutes and
    soc_datetime is returned as ISO8601 datetime string.

    :reqheader Authorization: The authentication token
    :reqheader Content-Type: application/json
    :resheader Content-Type: application/json
    :status 200: PROCESSED
    :status 400: INVALID_REQUEST
    :status 401: UNAUTHORIZED
    :status 403: INVALID_SENDER
    """
    return assets.get()


@bvp_api_v2_0.route("/assets", methods=["POST"])
@auth_token_required
# @usef_roles_accepted(*list_access(v2_0_service_listing, "POST /assets"))
def post_assets():
    """API endpoint to post a new asset.

    .. :quickref: Asset; Post a new asset

    This endpoint creates a new asset.
    Only users with the admin role are allowed to create assets.

    **Example request**

    The following example contains the required fields only, plus the two state of charge (soc) fields
    which a battery asset needs to specify:

    .. sourcecode:: json

        {
            "name": "Test battery",
            "asset_type": "battery",
            "unit": "kW",
            "owner": 2,
            "market": 1,
            "event_resolution": 5,
            "capacity_in_mw": 4.2,
            "latitude": 40,
            "longitude": 170.3,
            "max_soc_in_mwh": 5,
            "min_soc_in_mwh": 0
        }

    Note that event_resolution is expected as the number of minutes and
    soc_datetime is expected as ISO8601 datetime string.

    **Example response**

    The newly posted asset, including all fields, is returned in the response:

    .. sourcecode:: json

        {
            "id": 1,
            "asset_type": "battery",
            "unit": "kW"
            "capacity_in_mw": 4.2,
            "display_name": "Test battery",
            "event_resolution": 5,
            "latitude": 40,
            "longitude": 170.3,
            "max_soc_in_mwh": 5,
            "min_soc_in_mwh": 0,
            "name": "Test battery",
            "owner": 2,
            "market": 1,
            "soc_datetime": null,
            "soc_in_mwh": null,
            "soc_udi_event_id": null
        }

    :reqheader Authorization: The authentication token
    :reqheader Content-Type: application/json
    :resheader Content-Type: application/json
    :status 201: CREATED
    :status 400: INVALID_REQUEST
    :status 401: UNAUTHORIZED
    :status 403: INVALID_SENDER
    """
    return assets.post()


@bvp_api_v2_0.route("/asset/<id>", methods=["GET"])
@auth_token_required
# @usef_roles_accepted(*check_access(v2_0_service_listing, "GET /asset/<id>"))
def get_asset(id: int):
    """API endpoint to get an asset.

    .. :quickref: Asset; Get an asset

    This endpoint gets an asset.
    Only users who own the asset can use this endpoint.

    **Example response**

    .. sourcecode:: json

        {
            "asset_type": "battery",
            "capacity_in_mw": 2.0,
            "display_name": "Test battery",
            "event_resolution": 5,
            "id": 1,
            "latitude": 10,
            "longitude": 100,
            "market": 1,
            "max_soc_in_mwh": 5,
            "min_soc_in_mwh": 0,
            "name": "Test battery",
            "owner": 2,
            "soc_datetime": "2015-01-01T00:00:00+00:00",
            "soc_in_mwh": 2.5,
            "soc_udi_event_id": 203,
            "unit": "kW"
        }

    :reqheader Authorization: The authentication token
    :reqheader Content-Type: application/json
    :resheader Content-Type: application/json
    :status 200: PROCESSED
    :status 400: INVALID_REQUEST, REQUIRED_INFO_MISSING, UNEXPECTED_PARAMS
    :status 401: UNAUTHORIZED
    :status 403: INVALID_SENDER
    """
    return assets.fetch_one(id)


@bvp_api_v2_0.route("/asset/<id>", methods=["PATCH"])
@auth_token_required
# @usef_roles_accepted(*list_access(v2_0_service_listing, "PATCH /assets"))
def patch_asset(id: int):
    """API endpoint to patch asset data.

    .. :quickref: Asset; Patch data for an existing asset

    This endpoint sets data for an existing asset.
    Any subset of asset fields can be sent.
    Only users who own the asset are allowed to update its data.

    Several fields are not allowed to be updated, e.g. id. They are ignored.

    **Example request**

    .. sourcecode:: json

        {
            "latitude": 11.1,
            "longitude": 99.9,
        }

    Note that event_resolution is expected as the number of minutes and
    soc_datetime is expected as ISO8601 datetime string.

    **Example response**

    The whole asset is returned in the response:

    .. sourcecode:: json

        {
            "asset_type": "battery",
            "capacity_in_mw": 2.0,
            "display_name": "Test battery",
            "event_resolution": 5,
            "id": 1,
            "latitude": 11.1,
            "longitude": 99.9,
            "market": 1,
            "max_soc_in_mwh": 5,
            "min_soc_in_mwh": 0,
            "name": "Test battery",
            "owner": 2,
            "soc_datetime": "2015-01-01T00:00:00+00:00",
            "soc_in_mwh": 2.5,
            "soc_udi_event_id": 203,
            "unit": "kW"
        }

    :reqheader Authorization: The authentication token
    :reqheader Content-Type: application/json
    :resheader Content-Type: application/json
    :status 200: UPDATED
    :status 400: INVALID_REQUEST, REQUIRED_INFO_MISSING, UNEXPECTED_PARAMS
    :status 401: UNAUTHORIZED
    :status 403: INVALID_SENDER
    """
    return assets.patch(id)


@bvp_api_v2_0.route("/asset/<id>", methods=["DELETE"])
@auth_token_required
# @usef_roles_accepted(*list_access(v2_0_service_listing, "DELETE /assets"))
def delete_asset(id: int):
    """API endpoint to delete an asset, and its sensed data.

    .. :quickref: Asset; Delete an asset, together with its existing data.

    This endpoint deletes an existing asset, as well as all measurements recorded for it.
    Only users who own the asset are allowed to delete the asset.

    :reqheader Authorization: The authentication token
    :reqheader Content-Type: application/json
    :resheader Content-Type: application/json
    :status 204: DELETED
    :status 400: INVALID_REQUEST, REQUIRED_INFO_MISSING, UNEXPECTED_PARAMS
    :status 401: UNAUTHORIZED
    :status 403: INVALID_SENDER
    """
    return assets.delete(id)


# endpoints from earlier versions


@bvp_api_v2_0.route("/getConnection", methods=["GET"])
@as_response_type("GetConnectionResponse")
@auth_token_required
@usef_roles_accepted(*list_access(v2_0_service_listing, "getConnection"))
@append_doc_of(v1_3_routes.get_connection)
def get_connection():
    return v1_1_implementations.get_connection_response()


@bvp_api_v2_0.route("/postPriceData", methods=["POST"])
@as_response_type("PostPriceDataResponse")
@auth_token_required
@usef_roles_accepted(*list_access(v2_0_service_listing, "postPriceData"))
@append_doc_of(v1_3_routes.post_price_data)
def post_price_data():
    return v1_1_implementations.post_price_data_response()


@bvp_api_v2_0.route("/postWeatherData", methods=["POST"])
@as_response_type("PostWeatherDataResponse")
@auth_token_required
@usef_roles_accepted(*list_access(v2_0_service_listing, "postWeatherData"))
@append_doc_of(v1_3_routes.post_weather_data)
def post_weather_data():
    return v1_1_implementations.post_weather_data_response()


@bvp_api_v2_0.route("/getPrognosis", methods=["GET"])
@as_response_type("GetPrognosisResponse")
@auth_token_required
@usef_roles_accepted(*list_access(v2_0_service_listing, "getPrognosis"))
@append_doc_of(v1_3_routes.get_prognosis)
def get_prognosis():
    return v1_1_implementations.get_prognosis_response()


@bvp_api_v2_0.route("/getMeterData", methods=["GET"])
@as_response_type("GetMeterDataResponse")
@auth_token_required
@usef_roles_accepted(*list_access(v2_0_service_listing, "getMeterData"))
@append_doc_of(v1_3_routes.get_meter_data)
def get_meter_data():
    return v1_implementations.get_meter_data_response()


@bvp_api_v2_0.route("/postMeterData", methods=["POST"])
@as_response_type("PostMeterDataResponse")
@auth_token_required
@usef_roles_accepted(*list_access(v2_0_service_listing, "postMeterData"))
@append_doc_of(v1_3_routes.post_meter_data)
def post_meter_data():
    return v1_implementations.post_meter_data_response()


@bvp_api_v2_0.route("/getService", methods=["GET"])
@as_response_type("GetServiceResponse")
@append_doc_of(v1_3_routes.get_service)
def get_service(service_listing=v2_0_service_listing):
    return v1_implementations.get_service_response(service_listing)


@bvp_api_v2_0.route("/getDeviceMessage", methods=["GET"])
@as_response_type("GetDeviceMessageResponse")
@auth_token_required
@usef_roles_accepted(*list_access(v2_0_service_listing, "getDeviceMessage"))
@append_doc_of(v1_3_routes.get_device_message)
def get_device_message():
    return v1_3_implementations.get_device_message_response()


@bvp_api_v2_0.route("/postUdiEvent", methods=["POST"])
@as_response_type("PostUdiEventResponse")
@auth_token_required
@usef_roles_accepted(*list_access(v2_0_service_listing, "postUdiEvent"))
@append_doc_of(v1_3_routes.post_udi_event)
def post_udi_event():
    return v1_3_implementations.post_udi_event_response()
