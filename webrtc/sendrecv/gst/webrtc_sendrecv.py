'''
  These are the official GStreamer binding for Pything
  @source https://gitlab.freedesktop.org/gstreamer/gst-python

  1. Run HOST the UI

  cd ~/development/gst-examples/webrtc/sendrecv/js
  http-server --ssl --cert /etc/editshare/ssl/server-cert.pem --key /etc/editshare/ssl/server.key

  2. Run the signaling server and GStreamer Peer:

  cd ~/development/gst-examples/webrtc/sendrecv/gst
  python3 webrtc_signalling.py
  python3 webrtc_sendrecv.py


 Refereces:
 * https://brettviren.github.io/pygst-tutorial-org/pygst-tutorial.html
 * https://github.com/GStreamer/gst-python/tree/master/testsuite/old

'''

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
from websockets.version import version as wsv

DEFAULT_WS_SERVER = 'wss://127.0.0.1:8443'
DEFAULT_STUN_SERVER = 'stun://stun.l.google.com:19302' # or 'stun://stun.services.mozilla.com'



# Here's is the full pipeline (with 5 sources to be BUNDLED into one stream)
# PIPELINE_DESC = '''
#   webrtcbin name=sendrecv bundle-policy=max-bundle stun-server=stun://stun.l.google.com:19302
#     videotestsrc is-live=true pattern=snow     ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay ! queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
#     videotestsrc is-live=true pattern=ball     ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay ! queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
#     videotestsrc is-live=true pattern=smpte    ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay ! queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
#     videotestsrc is-live=true pattern=gradient ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay ! queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
# '''

# Initial pipeline with RTP stream and fakesink that we'll extend dynamically as peers connect in
PIPELINE_DESC = '''
    autovideosrc device=/dev/video0 ! videoconvert ! vp8enc deadline=1 ! rtpvp8pay ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! tee name=teapod teapod. ! queue ! fakesink
'''


# A bin typically starts with a source, elements (mux, demux, encode, decode) and ends with a sink
class GStreamberWebRTCBin:
    def __init__(self, pipeline, peer_id):
        self.pipeline = pipeline
        self.peer_id = peer_id
        self.webrtc = None

        # Create webrtc bin (here's a template that we'd normally use):
        # queue ! webrtcbin name=sendrecv bundle-policy=max-bundle stun-server=stun://stun.l.google.com:19302
        self.queue = Gst.ElementFactory.make('queue', 'queue_%s' % peer_id)

        self.webrtc = Gst.ElementFactory.make('webrtcbin', 'webrtc_bin_%s' % peer_id)
        self.webrtc.set_property('name', 'webrtc_bin_%s' % peer_id)
        self.webrtc.set_property('bundle-policy', 'max-bundle')
        self.webrtc.set_property('stun-server', DEFAULT_STUN_SERVER)
        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)

    def get_queue(self):
        return self.queue

    def get_sink(self):
        return self.webrtc

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

    def on_negotiation_needed(self, element):
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit('create-offer', None, promise)

    def on_offer_created(self, promise, _, __):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        self.send_sdp_offer(offer)

    def send_sdp_offer(self, offer):
        text = offer.sdp.as_text()
        print ('Sending offer:\n%s' % text)
        sdp_msg = json.dumps({'sdp': {'type': 'offer', 'sdp': text}})
        self.pipeline.msg_peer(self.peer_id, sdp_msg)

    def send_ice_candidate_message(self, _, mlineindex, candidate):
        ice_msg = json.dumps({'ice': {'candidate': candidate, 'sdpMLineIndex': mlineindex}})
        self.pipeline.msg_peer(self.peer_id, ice_msg)


# Pipeline consists of GST bins and is the top-level structure
class GStreamerWebRTCPipeline:
    def __init__(self, server):
        self.server = server or DEFAULT_WS_SERVER

        self.bins = dict()
        self.src_idx = 0

        # Initially our pipline will stream to fakesink until browser connects 
        self.start_pipeline()

    # Connect to signaling server
    async def connect(self):
        sslctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        self.conn = await websockets.connect(self.server, ssl=sslctx)
        await self.conn.send('HELLO_SERVER')

    # Send signal to WebRTC peer (eg. the browser)
    def msg_peer(self, peer_id, msg):
        data = 'MSG_PEER %s %s' % ( peer_id, msg )

        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.conn.send(data))
        loop.close()

    def start_pipeline(self):
        print("Starting pipeline")
        self.pipe = Gst.parse_launch(PIPELINE_DESC)

        # Wait then add some test sources
        # async_test1 = Timer(5.0, self.add_test_src, ["snow"],{})
        # async_test1.start()
        # async_test2 = Timer(10.0, self.add_test_src, ["ball"],{})
        # async_test2.start()
        # async_test3 = Timer(15.0, self.add_test_src, ["smpte"],{})
        # async_test3.start()

    def add_test_src(self, pattern):
        self.src_idx = self.src_idx + 1
        print ("Adding test source (%s) for pod nr %s" % (pattern, self.src_idx))

        # create test source
        test_src = Gst.ElementFactory.make("videotestsrc",      "test_src_%s" % self.src_idx)
        test_src.set_property("is-live", "true")
        test_src.set_property("pattern", pattern)

        # split frames
        converter = Gst.ElementFactory.make("videoconvert",     "test_con_%s" % self.src_idx)

        # encode
        encoder = Gst.ElementFactory.make("vp8enc",             "test_enc_%s" % self.src_idx)
        encoder.set_property("deadline", 1)

        # wrap (puts VP8 video in RTP packets)
        rtp = Gst.ElementFactory.make("rtpvp8pay",              "test_rtp_%s" % self.src_idx)

        # Set caps (capabilities - lightweight refcounted objects describing media types)
        cap = "application/x-rtp,media=video,encoding-name=VP8,payload=97"
        caps = Gst.caps_from_string(cap)
        caps_filter = Gst.ElementFactory.make("capsfilter",     "test_cap_%s" % self.src_idx)
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
        caps_filter.link(self.rtp)

        self.restart_stream()

    def restart_stream(self):
        if self.pipe.get_state(1)[1] == Gst.State.PLAYING:
            self.pipe.set_state(Gst.State.PAUSED)
            self.pipe.set_state(Gst.State.PLAYING)
        else:
            self.pipe.set_state(Gst.State.PLAYING)

    def add_webrtc_peer(self, peer_id):
        if peer_id in self.bins:
            return print ("WARN: Peer already registered: %s" % peer_id) 

        # Create new GStreamer webrtc bin and link it to our pipeline
        bin = self.bins[peer_id] = GStreamberWebRTCBin(self, peer_id)
        queue = bin.get_queue()
        sink = bin.get_sink()

        self.pipe.add(queue)
        self.pipe.add(sink)

        tee = self.pipe.get_by_name('teapod')
        tee.link(queue)
        queue.link(sink)

        print("Starting stream for peer %s" % peer_id)
        self.restart_stream()


    def msg_webrtc_peer(self, peer_id, msg):
        if peer_id not in self.bins:
            return print ("WARN: %s does not exist" % peer_id) 

        bin = self.bins[peer_id]
        bin.handle_sdp(msg)

    #
    # TODO: This maybe incomplete according to thi source
    # https://gstreamer.freedesktop.org/documentation/application-development/advanced/pipeline-manipulation.html?gi-language=c#changing-elements-in-a-pipeline
    #
    def remove_webrtc_peer(self, peer_id):
        if peer_id not in self.bins:
            return print ("WARN: %s does not exist" % peer_id) 

        print("Stopping stream for peer %s" % peer_id)

        needsRestart = False
        if self.pipe.get_state(1)[1] == Gst.State.PLAYING:
            self.pipe.set_state(Gst.State.PAUSED)
            needsRestart = True

        queue = self.bins[peer_id].get_queue()
        sink = self.bins[peer_id].get_sink()

        queue.unlink(sink)
        self.pipe.remove(queue)
        self.pipe.remove(sink)
        del self.bins[peer_id]

        if needsRestart:
            self.pipe.set_state(Gst.State.PLAYING)

    def close_pipeline(self):
        self.pipe.set_state(Gst.State.NULL)
        self.bins = dict()

    async def loop(self):
        assert self.conn
        async for message in self.conn:
            print("signal: %s" % message); 

            if message.startswith('HELLO'):
                print("Received HELLO. Awaiting peers...")

            elif message.startswith('PEER_CONNECTED'):
                _, peer_id = message.split(maxsplit=1)
                self.add_webrtc_peer(peer_id)
                print('New peer added. Total peers: %d' % len(self.bins))

            elif message.startswith('PEER_MESSAGE'):
                _, peer_id, msg = message.split(maxsplit=2)
                self.msg_webrtc_peer(peer_id, msg)

            elif message.startswith('PEER_DISCONNECTED'):
                _, peer_id = message.split(maxsplit=1)
                self.remove_webrtc_peer(peer_id)
                print('Peer removed. Total peers: %d' % len(self.bins))

            elif message.startswith('ERROR'):
                self.close_pipeline()
                return 1

            else:
                print("WARN: signal was unhandled"); 

        self.close_pipeline()
        return 0

    async def stop(self):
        if self.conn:
            await self.conn.close()
        self.conn = None


# Check that our gstreamer setup is complete
def check_plugins():
    needed = ["opus", "vpx", "nice", "webrtc", "dtls", "srtp", "rtp",
              "rtpmanager", "videotestsrc", "audiotestsrc"]
    missing = list(filter(lambda p: Gst.Registry.get().find_plugin(p) is None, needed))
    if len(missing):
        print('ERROR: Missing gstreamer plugins:', missing)
        return False
    return True

def check_versions():
    if Gst.VERSION_MAJOR < 1 or Gst.VERSION_MINOR < 14:
        print('ERROR: Please use GStreamer version 1.14 (ideally 1.18)')
        return False
    return True

if __name__=='__main__':
    Gst.init(None)
    if not check_plugins():
        sys.exit(1)
    if not check_versions():
        sys.exit(2)

    parser = argparse.ArgumentParser()
    parser.add_argument('--server', help='Signalling server to connect to (defaults to "%s")' % DEFAULT_WS_SERVER)
    args = parser.parse_args()

    pipeline = GStreamerWebRTCPipeline(args.server)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(pipeline.connect())
    res = loop.run_until_complete(pipeline.loop())
    sys.exit(res)


