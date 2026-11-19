"""-e copy: a camera's video written as it comes, without encoding.  What the decoder's thread hands to the
recorder and the streamer in place of pictures: the camera's packets (CamPacket), and what a file has to
know of the stream they belong to (CamStream)."""
import io

import av

from util import short_err as _short_err

FRAME, AUDIO, PACKET = 1, 2, 3      # what an item of the recorder's and the streamer's queue is


def wants_copy(encoder) -> bool:
    """Is this -e (or --ffmpeg "-c:v ...") the name FFmpeg has for no encoding?"""
    return (encoder or "").strip().lower() == "copy"


def is_annexb(data) -> bool:
    """Does this buffer begin with a start code?  (An MP4's or a Matroska's packets begin with a length.)"""
    return data[:3] == b"\0\0\1" or data[:4] == b"\0\0\0\1"


def param_sets(data, hevc) -> bytes:
    """The parameter sets (H.264 SPS/PPS, HEVC VPS/SPS/PPS) in front of the first slice of an Annex B
    buffer, each behind a four-byte start code: an access unit's own, or a stream's extradata in one
    spelling.  b"" when there are none, or when the buffer is not Annex B."""
    if not is_annexb(data):
        return b""
    out, n, i = [], len(data), data.find(b"\0\0\1")
    while 0 <= i and i + 3 < n:
        j = data.find(b"\0\0\1", i + 3)
        h = data[i + 3]
        t = (h >> 1) & 0x3f if hevc else h & 0x1f
        if (t < 32) if hevc else (1 <= t <= 5):
            break                                   # a slice: the picture begins
        if t in ((32, 33, 34) if hevc else (7, 8)):
            # (the zero bytes at its end belong to the next start code: a parameter set ends with a 1 bit)
            out.append(b"\0\0\0\1" + bytes(data[i + 3:n if j < 0 else j]).rstrip(b"\0"))
        i = j
    return b"".join(out)


class CamStream:
    """The video stream of one connection, for the files its packets go into.  Made on the decoder's thread
    while the connection is open.  `template` is a stream of a container of its own that is never written:
    a writer makes its stream from it on its own thread, whatever has become of the connection by then.
    `why`: the reason there is no template (the packets cannot be handed on)."""

    def __init__(self, stream):
        ctx = stream.codec_context
        self.codec = getattr(ctx, "name", None) or "unknown"
        self.width = int(getattr(ctx, "width", 0) or 0)
        self.height = int(getattr(ctx, "height", 0) or 0)
        self.extradata = bytes(getattr(ctx, "extradata", None) or b"")
        self.hevc = self.codec == "hevc"
        self.has_sets = self.codec in ("h264", "hevc")      # pictures that need parameter sets to be read
        # an MP4's "avcC" / "hvcC" begins with a version byte of 1, parameter sets in Annex B with a zero
        self.annexb = not self.extradata or self.extradata[0] != 1
        self.n = 0                                          # packets handed on (CamPacket.n)
        self.template = self.why = self._scratch = None
        try:
            if ctx is None:
                raise ValueError("no codec is known for it")
            self._scratch = av.open(io.BytesIO(), "w", format="matroska")
            self.template = self._scratch.add_stream_from_template(stream, opaque=True)
        except Exception as e:
            self.why = f"the camera's {self.codec} stream cannot be handed on as it is: {_short_err(e)}"

    def header(self) -> bytes:
        """The stream's own parameter sets in the spelling of param_sets(); b"" when it names none that way."""
        return param_sets(self.extradata, self.hevc) if self.has_sets and self.annexb else b""


class CamPacket:
    """One packet of the camera's video in memory of its own, with its times as the camera gave them
    (in `time_base`).  `n` counts the packets of its stream: a writer sees from it that one was dropped
    on the way (a full queue), after which only a key picture can go on."""
    __slots__ = ("data", "pts", "dts", "duration", "time_base", "key", "cam", "n")

    def __init__(self, data, pts, dts, time_base, key, cam, duration=0):
        self.data, self.pts, self.dts, self.duration = data, pts, dts, duration or 0
        self.time_base, self.key, self.cam = time_base, bool(key), cam
        cam.n += 1
        self.n = cam.n

    @classmethod
    def of(cls, packet, cam):
        return cls(bytes(packet), packet.pts, packet.dts, packet.time_base, packet.is_keyframe, cam, packet.duration)

    @property
    def width(self): return self.cam.width

    @property
    def height(self): return self.cam.height

    def sets(self) -> bytes:
        """The parameter sets in front of this packet's picture, b"" for none."""
        cam = self.cam
        return param_sets(self.data, cam.hevc) if cam.has_sets and cam.annexb else b""
