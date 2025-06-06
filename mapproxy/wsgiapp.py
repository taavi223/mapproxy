# This file is part of the MapProxy project.
# Copyright (C) 2010 Omniscale <http://omniscale.de>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
The WSGI application.
"""
from __future__ import print_function
import logging
import os
import re
import threading
import time

try:
    # time.strptime is thread-safe, but not the first call.
    # Import _strptime as a workaround. See: http://bugs.python.org/issue7980
    import _strptime  # noqa
except ImportError:
    pass

from mapproxy.request import Request
from mapproxy.response import Response
from mapproxy.config import local_base_config
from mapproxy.config.loader import load_configuration, ConfigurationError
from mapproxy.util.escape import escape_html

log = logging.getLogger('mapproxy.config')
log_wsgiapp = logging.getLogger('mapproxy.wsgiapp')


def make_wsgi_app(services_conf=None, debug=False, ignore_config_warnings=True, reloader=False):
    """
    Create a MapProxyApp with the given services conf.

    :param services_conf: the file name of the mapproxy.yaml configuration
    :param reloader: reload mapproxy.yaml when it changed
    """

    if reloader:
        def make_app():
            return make_wsgi_app(services_conf=services_conf, debug=debug, reloader=False)
        return ReloaderApp(services_conf, make_app)

    try:
        conf = load_configuration(mapproxy_conf=services_conf, ignore_warnings=ignore_config_warnings)
        services = conf.configured_services()
    except ConfigurationError as e:
        log.fatal(e)
        raise

    config_files = conf.config_files()

    app = MapProxyApp(services, conf.base_config)
    if debug:
        app = wrap_wsgi_debug(app, conf)

    app.config_files = config_files
    return app


class ReloaderApp(object):
    def __init__(self, timestamp_file, make_app_func):
        self.timestamp_file = timestamp_file
        self.make_app_func = make_app_func
        self.app = make_app_func()
        self._app_init_lock = threading.Lock()

    def _needs_reload(self):
        for conf_file, timestamp in self.app.config_files.items():
            m_time = os.path.getmtime(conf_file)
            if m_time > timestamp:
                return True
        return False

    def __call__(self, environ, start_response):
        if self._needs_reload():
            with self._app_init_lock:
                if self._needs_reload():
                    try:
                        self.app = self.make_app_func()
                    except ConfigurationError:
                        pass
                    self.last_reload = time.time()

        return self.app(environ, start_response)


def wrap_wsgi_debug(app, conf):
    conf.base_config.debug_mode = True
    try:
        from werkzeug.debug import DebuggedApplication
        app = DebuggedApplication(app, evalex=True)
    except ImportError:
        try:
            from paste.evalexception.middleware import EvalException
            app = EvalException(app)
        except ImportError:
            print('Error: Install Werkzeug or Paste for browser-based debugging.')

    return app


request_interceptors = set()


def register_request_interceptor(interceptor):
    request_interceptors.add(interceptor)


class MapProxyApp(object):
    """
    The MapProxy WSGI application.
    """
    handler_path_re = re.compile(r'^/(\w+)')

    def __init__(self, services, base_config):
        self.handlers = {}
        self.base_config = base_config
        self.cors_origin = base_config.http.access_control_allow_origin
        for service in services:
            for name in service.names:
                self.handlers[name] = service

    def __call__(self, environ, start_response):
        resp = None
        req = Request(environ)

        for interceptor in request_interceptors:
            req = interceptor(req)

        if self.cors_origin:
            orig_start_response = start_response

            def start_response(status, headers, exc_info=None):
                headers.append(('Access-control-allow-origin', self.cors_origin))
                return orig_start_response(status, headers, exc_info)

        with local_base_config(self.base_config):
            match = self.handler_path_re.match(req.path)
            if match:
                handler_name = match.group(1)
                if handler_name in self.handlers:
                    try:
                        resp = self.handlers[handler_name].handle(req)
                    except Exception:
                        if self.base_config.debug_mode:
                            raise
                        else:
                            log_wsgiapp.fatal('fatal error in %s for %s?%s',
                                handler_name, environ.get('PATH_INFO'), environ.get('QUERY_STRING'), exc_info=True)
                            import traceback
                            traceback.print_exc(file=environ['wsgi.errors'])
                            resp = Response('internal error', status=500)
            if resp is None:
                if req.path in ('', '/'):
                    resp = self.welcome_response(escape_html(req.script_url))
                else:
                    resp = Response('not found', mimetype='text/plain', status=404)
            return resp(environ, start_response)

    def welcome_response(self, script_url):
        import mapproxy.version
        html = "<html><body><h1>Welcome to MapProxy %s</h1>" % mapproxy.version.version
        if 'demo' in self.handlers:
            html += '<p>See all configured layers and services at: <a href="%s/demo/">demo</a>' % (script_url, )
        return Response(html, mimetype='text/html')
