import random
import ssl
import websockets
import asyncio
import os
import sys
import json
import argparse

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp

from threading import Timer

# Pipeline with unoptimized H264 stream
# PIPELINE_DESC = '''
#   webrtcbin name=sendrecv bundle-policy=max-bundle stun-server=stun://stun.l.google.com:19302
#     autovideosrc device=/dev/video0 ! videoconvert ! queue !
#     x264enc ! video/x-h264, profile=baseline ! rtph264pay !
#     queue ! application/x-rtp,media=video,encoding-name=H264,payload=96  ! sendrecv.
# '''

# Here's is the full pipeline (with 5 sources to be BUNDLED into one stream)
#   webrtcbin name=sendrecv bundle-policy=max-bundle
#     videotestsrc is-live=true pattern=snow     ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay ! queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
#     videotestsrc is-live=true pattern=ball     ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay ! queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
#     videotestsrc is-live=true pattern=smpte    ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay ! queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
#     videotestsrc is-live=true pattern=gradient ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay ! queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
#

# Pipeline that we'll extend dynamically
PIPELINE_DESC = '''
  webrtcbin name=sendrecv bundle-policy=max-bundle
    autovideosrc device=/dev/video0            ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay !  queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
'''

from websockets.version import version as wsv

class WebRTCClient:
    def __init__(self, id_, peer_id, server):
        self.id_ = id_
        self.idx = 0
        self.conn = None
        self.pipe = None
        self.webrtc = None
        self.peer_id = peer_id
        self.server = server or 'wss://webrtc.nirbheek.in:8443'


    async def connect(self):
        sslctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        self.conn = await websockets.connect(self.server, ssl=sslctx)
        await self.conn.send('HELLO %d' % self.id_)

    async def setup_call(self):
        await self.conn.send('SESSION {}'.format(self.peer_id))

    def send_sdp_offer(self, offer):
        text = offer.sdp.as_text()
        print ('Sending offer:\n%s' % text)
        msg = json.dumps({'sdp': {'type': 'offer', 'sdp': text}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(msg))
        loop.close()

    def on_offer_created(self, promise, _, __):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.send_sdp_offer(offer)

    def on_negotiation_needed(self, element):
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit('create-offer', None, promise)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        icemsg = json.dumps({'ice': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(icemsg))
        loop.close()

    def start_pipeline(self):
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)
        self.pipe.set_state(Gst.State.PLAYING)

        # Wait then add some test sources
        async_test1 = Timer(2.0, self.add_test_src, ["snow"],{})
        async_test1.start()
        async_test2 = Timer(4.0, self.add_test_src, ["ball"],{})
        async_test2.start()
        async_test3 = Timer(6.0, self.add_test_src, ["smpte"],{})
        async_test3.start()

    def add_test_src(self, pattern):
        self.idx = self.idx + 1
        print ("Adding test source (%s) for pod nr %s\n" % (pattern, self.idx))

        # Take source
        test_src = Gst.ElementFactory.make("videotestsrc",      "test_src_%s" % self.idx)
        test_src.set_property("is-live", "true")
        test_src.set_property("pattern", pattern)

        # split frames
        converter = Gst.ElementFactory.make("videoconvert",     "test_con_%s" % self.idx)

        # encode
        encoder = Gst.ElementFactory.make("vp8enc",             "test_enc_%s" % self.idx)
        encoder.set_property("deadline", 1)

        # wrap (puts VP8 video in RTP packets)
        rtp = Gst.ElementFactory.make("rtpvp8pay",              "test_rtp_%s" % self.idx)

        # Set caps (capabilities - lightweight refcounted objects describing media types)
        cap = "application/x-rtp,media=video,encoding-name=VP8,payload=97"
        caps = Gst.caps_from_string(cap)
        caps_filter = Gst.ElementFactory.make("capsfilter",     "test_cap_%s" % self.idx)
        caps_filter.set_property("caps", caps)

        # add to pipeline and link
        self.pipe.add(test_src)
        self.pipe.add(converter)
        self.pipe.add(encoder)
        self.pipe.add(rtp)
        self.pipe.add(caps_filter)

        test_src.link(converter)
        converter.link(encoder)
        encoder.link(rtp)
        rtp.link(caps_filter)
        caps_filter.link(self.webrtc)

        self.start_stop_stream()

    def start_stop_stream(self):
        self.pipe.set_state(Gst.State.PAUSED)
        self.pipe.set_state(Gst.State.PLAYING)

    def handle_sdp(self, message):
        assert (self.webrtc)
        msg = json.loads(message)
        if 'sdp' in msg:
            sdp = msg['sdp']
            assert(sdp['type'] == 'answer')
            sdp = sdp['sdp']
            print ('Received answer:\n%s' % sdp)
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()
        elif 'ice' in msg:
            ice = msg['ice']
            candidate = ice['candidate']
            print ('Received candidate: ', candidate)
            sdpmlineindex = ice['sdpMLineIndex']
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)

    def close_pipeline(self):
        self.pipe.set_state(Gst.State.NULL)
        self.pipe = None
        self.webrtc = None

    async def loop(self):
        assert self.conn
        async for message in self.conn:
            if message == 'HELLO':
                await self.setup_call()
            elif message == 'SESSION_OK':
                self.start_pipeline()
            elif message.startswith('ERROR'):
                print (message)
                self.close_pipeline()
                return 1
            else:
                self.handle_sdp(message)
        self.close_pipeline()
        return 0

    async def stop(self):
        if self.conn:
            await self.conn.close()
        self.conn = None


def check_plugins():
    needed = ["opus", "vpx", "nice", "webrtc", "dtls", "srtp", "rtp",
              "rtpmanager", "videotestsrc", "audiotestsrc"]
    missing = list(filter(lambda p: Gst.Registry.get().find_plugin(p) is None, needed))
    if len(missing):
        print('Missing gstreamer plugins:', missing)
        return False
    return True


if __name__=='__main__':
    Gst.init(None)
    if not check_plugins():
        sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument('peerid', help='String ID of the peer to connect to')
    parser.add_argument('--server', help='Signalling server to connect to, eg "wss://127.0.0.1:8443"')
    args = parser.parse_args()
    our_id = random.randrange(10, 10000)
    c = WebRTCClient(our_id, args.peerid, args.server)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(c.connect())
    res = loop.run_until_complete(c.loop())
    sys.exit(res)
