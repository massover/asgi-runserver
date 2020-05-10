import errno
import logging
import os
import sys
from datetime import datetime

from daphne.cli import ASGI3Middleware
from daphne.endpoints import build_endpoint_description_strings
from daphne.server import Server
from django.apps import apps
from django.conf import settings
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler
from django.contrib.staticfiles.management.commands import runserver
from django.core.asgi import get_asgi_application
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError
from django.core.servers.basehttp import run
from django.utils import autoreload
from django.utils.module_loading import import_string

logger = logging.getLogger("django.server")


def get_internal_asgi_application():
    """
    Load and return the ASGI application as configured by the user in
    ``settings.ASGI_APPLICATION``. With the default ``startproject`` layout,
    this will be the ``application`` object in ``projectname/asgi.py``.

    This function, and the ``ASGI_APPLICATION`` setting itself, are only useful
    for Django's internal server (runserver); external ASGI servers should just
    be configured to point to the correct application object directly.

    If settings.ASGI_APPLICATION is not set (is ``None``), return
    whatever ``django.core.asgi.get_asgi_application`` returns.
    """
    from django.conf import settings

    app_path = getattr(settings, "ASGI_APPLICATION")
    if app_path is None:
        return get_asgi_application()

    try:
        return import_string(app_path)
    except ImportError as err:
        raise ImproperlyConfigured(
            "ASGI application '%s' could not be loaded; "
            "Error importing module." % app_path
        ) from err


class Command(runserver.Command):
    protocol = "http"
    http_timeout = None

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--asgi",
            action="store_true",
            dest="asgi",
            default=False,
            help="Run an ASGI-based runserver rather than the WSGI-based one",
        )
        parser.add_argument(
            "--http_timeout",
            action="store",
            dest="http_timeout",
            type=int,
            default=None,
            help="Specify the daphne http_timeout interval in seconds (default: no timeout)",
        )

    def handle(self, *args, **options):
        self.http_timeout = options.get("http_timeout", self.http_timeout)

        if options["asgi"] and not hasattr(settings, "ASGI_APPLICATION"):
            raise CommandError(
                "You have not set ASGI_APPLICATION, which is needed to run the server."
            )
        super().handle(*args, **options)

    def inner_run(self, *args, **options):
        # If an exception was silenced in ManagementUtility.execute in order
        # to be raised in the child process, raise it now.
        autoreload.raise_last_exception()
        quit_command = "CTRL-BREAK" if sys.platform == "win32" else "CONTROL-C"
        self.stdout.write("Performing system checks...\n\n")
        self.check(display_num_errors=True)
        # Need to check migrations here, so can't use the
        # requires_migrations_check attribute.
        self.check_migrations()
        now = datetime.now().strftime("%B %d, %Y - %X")
        self.stdout.write(now)
        if options["asgi"]:
            server_type = "ASGI"
            run_fn = self.run_asgi
        else:
            server_type = "WSGI"
            run_fn = self.run_wsgi
        self.stdout.write(
            (
                "Django version %(version)s, using settings %(settings)r\n"
                "Starting %(server_type)s development server at %(protocol)s://%(addr)s:%(port)s/\n"
                "Quit the server with %(quit_command)s."
            )
            % {
                "version": self.get_version(),
                "settings": settings.SETTINGS_MODULE,
                "server_type": server_type,
                "protocol": self.protocol,
                "addr": "[%s]" % self.addr if self._raw_ipv6 else self.addr,
                "port": self.port,
                "quit_command": quit_command,
            }
        )
        run_fn(*args, **options)

    def run_wsgi(self, *args, **options):
        threading = options["use_threading"]
        # 'shutdown_message' is a stealth option.
        shutdown_message = options.get("shutdown_message", "")
        try:
            handler = self.get_handler(*args, **options)
            run(
                self.addr,
                int(self.port),
                handler,
                ipv6=self.use_ipv6,
                threading=threading,
                server_cls=self.server_cls,
            )
        except OSError as e:
            # Use helpful error messages instead of ugly tracebacks.
            ERRORS = {
                errno.EACCES: "You don't have permission to access that port.",
                errno.EADDRINUSE: "That port is already in use.",
                errno.EADDRNOTAVAIL: "That IP address can't be assigned to.",
            }
            try:
                error_text = ERRORS[e.errno]
            except KeyError:
                error_text = e
            self.stderr.write("Error: %s" % error_text)
            # Need to use an OS exit because sys.exit doesn't work in a thread
            os._exit(1)
        except KeyboardInterrupt:
            if shutdown_message:
                self.stdout.write(shutdown_message)
            sys.exit(0)

    def run_asgi(self, *args, **options):
        # Launch server in 'main' thread. Signals are disabled as it's still
        # actually a subthread under the autoreloader.
        logger.debug("Daphne running, listening on %s:%s", self.addr, self.port)

        # build the endpoint description string from host/port options
        endpoints = build_endpoint_description_strings(host=self.addr, port=self.port)
        try:
            Server(
                application=self.get_handler(**options),
                endpoints=endpoints,
                signal_handlers=not options["use_reloader"],
                action_logger=self.log_action,
                http_timeout=self.http_timeout,
                root_path=getattr(settings, "FORCE_SCRIPT_NAME", "") or "",
            ).run()
            logger.debug("Daphne exited")
        except KeyboardInterrupt:
            shutdown_message = options.get("shutdown_message", "")
            if shutdown_message:
                self.stdout.write(shutdown_message)
            return

    def get_handler(self, *args, **options):
        """
        Returns the static files serving application wrapping the default application,
        if static files should be served. Otherwise just returns the default
        handler. Also wraps the application in an ASGI3Middleware for daphne compatibility.
        """
        if not options["asgi"]:
            return super().get_handler(*args, **options)

        staticfiles_installed = apps.is_installed("django.contrib.staticfiles")
        use_static_handler = options.get("use_static_handler", staticfiles_installed)
        insecure_serving = options.get("insecure_serving", False)
        if use_static_handler and (settings.DEBUG or insecure_serving):
            application = ASGIStaticFilesHandler(get_internal_asgi_application())
        else:
            application = get_internal_asgi_application()
        return ASGI3Middleware(application)

    def log_action(self, protocol, action, details):
        """
        Logs various different kinds of requests to the console.
        """
        # HTTP requests
        if protocol == "http" and action == "complete":
            msg = "HTTP %(method)s %(path)s %(status)s [%(time_taken).2f, %(client)s]"

            # Utilize terminal colors, if available
            if 200 <= details["status"] < 300:
                # Put 2XX first, since it should be the common case
                logger.info(self.style.HTTP_SUCCESS(msg), details)
            elif 100 <= details["status"] < 200:
                logger.info(self.style.HTTP_INFO(msg), details)
            elif details["status"] == 304:
                logger.info(self.style.HTTP_NOT_MODIFIED(msg), details)
            elif 300 <= details["status"] < 400:
                logger.info(self.style.HTTP_REDIRECT(msg), details)
            elif details["status"] == 404:
                logger.warning(self.style.HTTP_NOT_FOUND(msg), details)
            elif 400 <= details["status"] < 500:
                logger.warning(self.style.HTTP_BAD_REQUEST(msg), details)
            else:
                # Any 5XX, or any other response
                logger.error(self.style.HTTP_SERVER_ERROR(msg), details)
