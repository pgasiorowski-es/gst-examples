#!/usr/bin/env python3

import os
import sys
import ssl
import logging
import asyncio
import websockets
import argparse
import http
import concurrent

class WebRTCSignalingServer(object):

    def __init__(self, loop, options):
        ############### Global data ###############

        # Event loop
        self.loop = loop

        # Websocket Server Instance
        self.server = None

        # Peers: Browsers
        self.peers = dict()

        # Peer: Gstreamer
        self.gstreamer = None

        # Options
        self.addr = options.addr
        self.port = options.port
        self.keepalive_timeout = options.keepalive_timeout
        self.cert_restart = options.cert_restart
        self.cert_path = options.cert_path
        self.disable_ssl = options.disable_ssl
        self.health_path = options.health

        # Certificate mtime, used to detect when to restart the server
        self.cert_mtime = -1

    ############### Helper functions ###############

    async def health_check(self, path, request_headers):
        if path == self.health_path:
            return http.HTTPStatus.OK, [], b"OK\n"
        return None

    async def recv_msg_ping(self, ws, raddr):
        '''
        Wait for a message forever, and send a regular ping to prevent bad routers
        from closing the connection.
        '''
        msg = None
        while msg is None:
            try:
                msg = await asyncio.wait_for(ws.recv(), self.keepalive_timeout)
            except (asyncio.TimeoutError, concurrent.futures._base.TimeoutError):
                # print('Sending keepalive ping to {!r} in recv'.format(raddr))
                await ws.ping()
        return msg

    ############### Handler functions ###############

    
    async def connection_handler(self, ws):
        while True:
            # Receive command, wait forever if necessary
            msg = await self.recv_msg_ping(ws, ws.remote_address)

            print(msg)

            # GStreamer server connected: 
            # HELLO_SERVER
            if msg.startswith('HELLO_SERVER'):
                print ('GStreamer connected')
                self.gstreamer = ws
                await self.gstreamer.send('HELLO')

                # Let peers know
                for peer_id in self.peers:
                    await self.peers[peer_id].send('SERVER_CONNECTED')


            # Browser connected
            # HELLO_PEER 1234
            elif msg.startswith('HELLO_PEER'):
                _, peer_id = msg.split(maxsplit=1)
                self.peers[peer_id] = ws
                await self.peers[peer_id].send('HELLO')

                # Let server know
                if self.gstreamer:
                    await self.gstreamer.send('PEER_CONNECTED %s' % peer_id)

            # Browser wants to message server
            # MSG_SERVER {"sdp": {...}}
            elif msg.startswith('MSG_SERVER'):
                _, json = msg.split(maxsplit=1)
                if self.gstreamer:
                    await self.gstreamer.send(json)
                else:
                    print('WARN: Server not set yet')

            # Server wants to meesage prowser
            # MSG_PEER 1234 {"sdp": {...}}
            elif msg.startswith('MSG_PEER'):
                _, peer_id, json = msg.split(maxsplit=2)
                if self.peers[peer_id]:
                    await self.peers[peer_id].send(json)
                else:
                    print('WARN: Peer %s does not exist' % peer_id)

    def get_ssl_certs(self):
        chain_pem = '/etc/editshare/ssl/server-cert.pem'
        key_pem = '/etc/editshare/ssl/server.key'
        return chain_pem, key_pem

    def get_ssl_ctx(self):
        if self.disable_ssl:
            return None
        # Create an SSL context to be used by the websocket server
        print('Using TLS with keys in {!r}'.format(self.cert_path))
        chain_pem, key_pem = self.get_ssl_certs()
        sslctx = ssl.create_default_context()
        try:
            sslctx.load_cert_chain(chain_pem, keyfile=key_pem)
        except FileNotFoundError:
            print("Certificates not found, did you run generate_cert.sh?")
            sys.exit(1)
        # FIXME
        sslctx.check_hostname = False
        sslctx.verify_mode = ssl.CERT_NONE
        return sslctx

    def run(self):
        async def handler(ws, path):
            '''
            All incoming messages are handled here. @path is unused.
            '''
            raddr = ws.remote_address
            print("Connected to {!r}".format(raddr))

            try:
                await self.connection_handler(ws)
            except websockets.ConnectionClosed:
                print("Connection to peer {!r} closed, exiting handler".format(raddr))
            finally:
                if self.gstreamer == ws:
                    self.gstreamer = None
                else:
                    for peer_id in self.peers:
                        if self.peers[peer_id] == ws:
                            del self.peers[peer_id]


        sslctx = self.get_ssl_ctx()

        print("Listening on https://{}:{}".format(self.addr, self.port))
        # Websocket server
        wsd = websockets.serve(handler, self.addr, self.port, ssl=sslctx, process_request=self.health_check if self.health_path else None,
                               # Maximum number of messages that websockets will pop
                               # off the asyncio and OS buffers per connection. See:
                               # https://websockets.readthedocs.io/en/stable/api.html#websockets.protocol.WebSocketCommonProtocol
                               max_queue=16)

        # Setup logging
        logger = logging.getLogger('websockets')
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler())

        # Run the server
        self.server = self.loop.run_until_complete(wsd)
        # Stop the server if certificate changes
        self.loop.run_until_complete(self.check_server_needs_restart())

    async def stop(self):
        print('Stopping server... ', end='')
        self.server.close()
        await self.server.wait_closed()
        self.loop.stop()
        print('Stopped.')

    def check_cert_changed(self):
        chain_pem, key_pem = self.get_ssl_certs()
        mtime = max(os.stat(key_pem).st_mtime, os.stat(chain_pem).st_mtime)
        if self.cert_mtime < 0:
            self.cert_mtime = mtime
            return False
        if mtime > self.cert_mtime:
            self.cert_mtime = mtime
            return True
        return False

    async def check_server_needs_restart(self):
        "When the certificate changes, we need to restart the server"
        if not self.cert_restart:
            return
        while True:
            await asyncio.sleep(10)
            if self.check_cert_changed():
                print('Certificate changed, stopping server...')
                await self.stop()
                return


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    # See: host, port in https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.create_server
    parser.add_argument('--addr', default='', help='Address to listen on (default: all interfaces, both ipv4 and ipv6)')
    parser.add_argument('--port', default=8443, type=int, help='Port to listen on')
    parser.add_argument('--keepalive-timeout', dest='keepalive_timeout', default=30, type=int, help='Timeout for keepalive (in seconds)')
    parser.add_argument('--cert-path', default=os.path.dirname(__file__))
    parser.add_argument('--disable-ssl', default=False, help='Disable ssl', action='store_true')
    parser.add_argument('--health', default='/health', help='Health check route')
    parser.add_argument('--restart-on-cert-change', default=False, dest='cert_restart', action='store_true', help='Automatically restart if the SSL certificate changes')

    options = parser.parse_args(sys.argv[1:])

    loop = asyncio.get_event_loop()

    r = WebRTCSignalingServer(loop, options)

    print('Starting server...')
    while True:
        r.run()
        loop.run_forever()
        print('Restarting server...')
    print("Goodbye!")

if __name__ == "__main__":
    main()
