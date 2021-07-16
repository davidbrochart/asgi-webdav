from typing import Optional
import sys
import pathlib
import logging.config
from logging import getLogger


from asgi_middleware_static_file import ASGIMiddlewareStaticFile


from asgi_webdav import __version__
from asgi_webdav.constants import DAVMethod, AppArgs
from asgi_webdav.exception import NotASGIRequestException, ProviderInitException
from asgi_webdav.config import (
    Config,
    update_config_from_obj,
    update_config_from_file,
    get_config,
)
from asgi_webdav.request import DAVRequest
from asgi_webdav.auth import DAVAuth
from asgi_webdav.web_dav import WebDAV
from asgi_webdav.web_page import WebPage
from asgi_webdav.response import DAVResponse
from asgi_webdav.logging import LOGGING_CONFIG

logger = getLogger(__name__)


class Server:
    def __init__(self, config: Config):
        logger.info("ASGI WebDAV Server(v{}) starting...".format(__version__))
        self.dav_auth = DAVAuth(config)
        try:

            self.web_dav = WebDAV(config)
        except ProviderInitException as e:
            logger.error(e)
            logger.info("ASGI WebDAV Server has stopped working!")
            sys.exit()

        self.web_page = WebPage()

    async def __call__(self, scope, receive, send) -> None:
        try:
            request = DAVRequest(scope, receive, send)

        except NotASGIRequestException as e:
            message = bytes(e.message, encoding="utf-8")
            request = DAVRequest({"method": "GET"}, receive, send)
            await DAVResponse(400, content=message).send_in_one_call(request)
            return

        response = await self.handle(request)
        logger.info(
            '{} - "{} {}" {} {} - {}'.format(
                request.client_ip_address,
                request.method,
                request.path,
                response.status,
                request.authorization_method,  # Basic/Digest
                request.client_user_agent,
            )
        )
        logger.debug(request.headers)
        await response.send_in_one_call(request)

    async def handle(self, request: DAVRequest) -> DAVResponse:
        # check user auth
        request.user, message = self.dav_auth.pick_out_user(request)
        if request.user is None:
            logger.debug(request)
            return self.dav_auth.create_response_401(request, message)

        # process Admin request
        if (
            request.method == DAVMethod.GET
            and request.src_path.count >= 1
            and request.src_path.parts[0] == "_"
        ):
            # route /_
            status, data = await self.web_page.enter(request)
            return DAVResponse(
                status=status,
                content=data.encode("utf-8"),
            )

        # process WebDAV request
        response = await self.web_dav.distribute(request)
        logger.debug(response)

        return response


def get_app(
    app_args: AppArgs,
    config_obj: Optional[dict] = None,
    config_file: Optional[str] = None,
):
    logging.config.dictConfig(LOGGING_CONFIG)

    # init config
    if config_obj is not None:
        update_config_from_obj(config_obj)
    if config_file is not None:
        update_config_from_file(config_file)

    config = get_config()
    config.update_from_app_args_and_env_and_default_value(app_args=app_args)

    LOGGING_CONFIG["loggers"]["asgi_webdav"]["level"] = config.logging_level.value
    if app_args.in_docker_container:
        LOGGING_CONFIG["handlers"]["webdav"]["formatter"] = "webdav_docker"
        LOGGING_CONFIG["handlers"]["uvicorn"]["formatter"] = "uvicorn_docker"

    logging.config.dictConfig(LOGGING_CONFIG)

    # create ASGI app
    app = Server(config)

    # route /_/static
    app = ASGIMiddlewareStaticFile(
        app=app,
        static_url="_/static",
        static_root_paths=[pathlib.Path(__file__).parent.joinpath("static")],
    )

    # config sentry
    if config.sentry_dsn:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

            sentry_sdk.init(dsn=config.sentry_dsn)
            app = SentryAsgiMiddleware(app)

        except ImportError as e:
            logger.warning(e)

    logger.info(
        "ASGI WebDAV Server running on http://{}:{} (Press CTRL+C to quit)".format(
            app_args.bind_host if app_args.bind_host is not None else "?",
            app_args.bind_port if app_args.bind_port is not None else "?",
        )
    )
    return app