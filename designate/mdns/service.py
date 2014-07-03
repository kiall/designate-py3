# Copyright 2014 Hewlett-Packard Development Company, L.P.
#
# Author: Kiall Mac Innes <kiall@hp.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import socket

from oslo.config import cfg

from designate import service
from designate.mdns import handler
from designate.mdns import notify
from designate.openstack.common import log as logging
from designate.openstack.common.gettextutils import _LI
from designate.openstack.common.gettextutils import _LW

LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class Service(service.Service):
    def __init__(self, *args, **kwargs):
        notify_endpoint = notify.NotifyEndpoint()
        kwargs['endpoints'] = [notify_endpoint]
        super(Service, self).__init__(*args, **kwargs)

        # Create an instance of the RequestHandler class
        self.handler = handler.RequestHandler()

        # Bind to the TCP port
        LOG.info(_LI('Opening TCP Listening Socket'))
        self._sock_tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock_tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock_tcp.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock_tcp.bind((CONF['service:mdns'].host,
                             CONF['service:mdns'].port))
        self._sock_tcp.listen(CONF['service:mdns'].tcp_backlog)

        # Bind to the UDP port
        LOG.info(_LI('Opening UDP Listening Socket'))
        self._sock_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_udp.bind((CONF['service:mdns'].host,
                             CONF['service:mdns'].port))

    def start(self):
        super(Service, self).start()

        self.tg.add_thread(self._handle_tcp)
        self.tg.add_thread(self._handle_udp)
        LOG.info(_LI("started mdns service"))

    def stop(self):
        # When the service is stopped, the threads for _handle_tcp and
        # _handle_udp are stopped too.
        super(Service, self).stop()

    def _handle_tcp(self):
        LOG.info(_LI("_handle_tcp thread started"))
        while True:
            client, addr = self._sock_tcp.accept()
            LOG.warn(_LW("Handling TCP Request from: %s") % addr)

            payload = client.recv(65535)

            self.tg.add_thread(self._handle, addr, payload, client)

    def _handle_udp(self):
        LOG.info(_LI("_handle_udp thread started"))
        while True:
            # TODO(kiall): Determine the appropriate default value for
            #              UDP recvfrom.
            payload, addr = self._sock_udp.recvfrom(8192)
            LOG.warn(_LW("Handling UDP Request from: %s") % addr)

            self.tg.add_thread(self._handle, addr, payload)

    def _handle(self, addr, payload, client=None):
        """
        Handle a DNS Query

        :param addr: Tuple of the client's (IP, Port)
        :param payload: Raw DNS query payload
        :param client: Client socket (for TCP only)
        """
        response = self.handler.handle(payload)

        if client is not None:
            # Handle TCP Responses
            client.send(response)
            client.close()
        else:
            # Handle UDP Responses
            self._sock_udp.sendto(response, addr)