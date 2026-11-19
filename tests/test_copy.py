"""-e copy: the camera's video written as it comes.  The packets (campacket), the writer without an encoder,
the recorder's files that begin with key pictures, what happens where it cannot be done, the decoder
thread's hand-over, and nova.py end to end."""
import glob
import logging
import os
import queue
import signal
import subprocess
import sys
import time
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest

import logsync  # noqa: F401  (logging.verbose)
import avwriter
import recorder
import streamer
from avwriter import AVWriter, CopyRefused
from campacket import AUDIO, FRAME, PACKET, CamPacket, CamStream, param_sets, wants_copy
from test_output_recorder import Clock, app, local, segments  # noqa: F401  (app: fixture)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = os.name == "nt"
GOP = 10


def has_encoder(name):
    try:
        av.codec.Codec(name, "w")
        return True
    except Exception:
        return False


def camera_file(folder, name="cam.ts", codec="libx264", n=60, size=(320, 180), **opts):
    """What a camera sends, as a file: MPEG-TS holds H.264 and HEVC as RTSP hands them on, in Annex B with
    the parameter sets in front of every key picture.  A key picture every GOP pictures, 25 a second."""
    path = os.path.join(str(folder), name)
    with av.open(path, "w", format="mpegts" if name.endswith(".ts") else None) as c:
        s = c.add_stream(codec, rate=25)
        s.width, s.height, s.pix_fmt = size[0], size[1], "yuv420p"
        s.codec_context.gop_size = GOP
        s.codec_context.options = {"bf": "0", **({"x265-params": "log-level=error:bframes=0"} if codec == "libx265"
                                                 else {"preset": "ultrafast"}), **opts}
        for i in range(n):
            img = np.full((size[1], size[0], 3), (i * 4) % 255, np.uint8)
            img[:, (i * 5) % size[0]:(i * 5) % size[0] + 6] = 255
            f = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
            f.pts = i
            c.mux(s.encode(f))
        c.mux(s.encode())
    return path


def packets(path):
    """(the stream, its packets) as the decoder's thread hands them on."""
    with av.open(path) as c:
        vs = c.streams.video[0]
        cam = CamStream(vs)
        return cam, [CamPacket.of(p, cam) for p in c.demux(vs) if p.dts is not None]


def slices(data, hevc=False, lengths=False):
    """The slices of an access unit, from Annex B or from an MP4's length-prefixed spelling."""
    units, i = [], 0
    if lengths:
        while i + 4 <= len(data):
            n = int.from_bytes(data[i:i + 4], "big")
            units.append(bytes(data[i + 4:i + 4 + n]))
            i += 4 + n
    else:
        i = data.find(b"\0\0\1")
        while i >= 0:
            j = data.find(b"\0\0\1", i + 3)
            units.append(bytes(data[i + 3:len(data) if j < 0 else j]))
            i = j
    vcl = (lambda b: ((b[0] >> 1) & 0x3f) < 32) if hevc else (lambda b: 1 <= (b[0] & 0x1f) <= 5)
    return [u.rstrip(b"\0") for u in units if u and vcl(u)]


def read(path):
    """What a file holds: (codec name, tag, [(is key, data)], pictures that decode, the stream's seconds)."""
    with av.open(str(path)) as c:
        v = c.streams.video[0]
        pk = [(p.is_keyframe, bytes(p), p.dts, p.pts) for p in c.demux(v) if p.dts is not None]
        c.seek(0)
        frames = sum(1 for _ in c.decode(v))
        return SimpleNamespace(codec=v.codec_context.name, tag=v.codec_context.codec_tag, packets=pk, frames=frames,
                               size=(v.codec_context.width, v.codec_context.height),
                               audio=[a.codec_context.name for a in c.streams.audio],
                               seconds=float((pk[-1][2] - pk[0][2]) * v.time_base) if pk else 0.0)


def copy_writer(path, first, **kw):
    cam = first.cam
    kw.setdefault("audio_enable", False)
    return AVWriter(None, path=str(path), width=cam.width, height=cam.height, fps=25, encoder="copy",
                    source_codec=cam.codec, first=first, **kw)


def written(path, pkts, **kw):
    w = copy_writer(path, pkts[0], **kw)
    for p in pkts:
        w.write_packet(p)
    w.close()
    return w


# ---------------------------------------------------------------- the packets

def test_copy_is_ffmpegs_word_for_no_encoding():
    assert wants_copy("copy") and wants_copy(" Copy ")
    assert not wants_copy("libx264") and not wants_copy(None) and not wants_copy("")


def test_a_key_picture_s_parameter_sets_are_read_from_its_front(tmp_path):
    cam, pkts = packets(camera_file(tmp_path))
    assert cam.codec == "h264" and cam.annexb and (cam.width, cam.height) == (320, 180) and cam.template is not None
    assert [p.key for p in pkts[:GOP + 1]] == [True] + [False] * (GOP - 1) + [True]
    sets = pkts[0].sets()
    assert sets and avwriter._only_param_sets(sets, False) and sets.count(b"\0\0\0\1") == 2      # SPS, PPS
    assert pkts[GOP].sets() == sets and pkts[1].sets() == b""
    assert cam.header() == sets                         # what the connection named, in the same spelling
    assert [p.n for p in pkts[:3]] == [1, 2, 3]


def test_start_codes_of_either_length_give_the_same_parameter_sets():
    sps, pps, idr = b"\x67\x42\x00\x1e\xab", b"\x68\xce\x3c\x80", b"\x65\x88\x84\x00\x10"
    long = b"\0\0\0\1" + sps + b"\0\0\0\1" + pps + b"\0\0\0\1" + idr
    short = b"\0\0\1" + sps + b"\0\0\1" + pps + b"\0\0\1" + idr
    assert param_sets(long, False) == param_sets(short, False) == b"\0\0\0\1" + sps + b"\0\0\0\1" + pps
    assert param_sets(b"\0\0\1" + idr + b"\0\0\1" + sps, False) == b""          # behind the picture: not its own
    vps, hsps, hpps = b"\x40\x01\x0c", b"\x42\x01\x01", b"\x44\x01\xc1"
    assert param_sets(b"\0\0\1" + vps + b"\0\0\1" + hsps + b"\0\0\1" + hpps + b"\0\0\1\x26\x01\xaf", True) \
        == b"\0\0\0\1" + vps + b"\0\0\0\1" + hsps + b"\0\0\0\1" + hpps


def test_a_packet_that_carries_lengths_is_not_read_as_annex_b(test_clip):
    """An MP4's packets begin with a length, and a length of 256 to 511 bytes looks like a start code."""
    cam, pkts = packets(str(test_clip))
    assert not cam.annexb and cam.extradata[0] == 1 and cam.header() == b""
    looks_like = CamPacket(b"\0\0\1\x28" + bytes(0x128), 0, 0, Fraction(1, 90000), True, cam)
    assert looks_like.sets() == b"" and all(p.sets() == b"" for p in pkts)


# ---------------------------------------------------------------- the writer

@pytest.mark.parametrize("bf", ["0", "2"])
@pytest.mark.parametrize("movflags", [None, "+faststart"])
def test_a_camera_s_packets_reach_the_file_as_they_are(tmp_path, bf, movflags):
    cam, pkts = packets(camera_file(tmp_path, bf=bf))
    out = tmp_path / "out.mp4"
    w = written(out, pkts, mux_opts={"movflags": movflags} if movflags else None)
    assert w.v_encoder == "copy" and w.v_frames == len(pkts) and w.encoder_note() is None
    f = read(out)
    assert (f.codec, f.tag, f.size) == ("h264", "avc1", (320, 180))
    assert f.frames == len(f.packets) == len(pkts) and f.packets[0][0]
    # every slice is the camera's, byte for byte
    assert [s for _, data, _, _ in f.packets for s in slices(data, lengths=True)] == [s for p in pkts for s in slices(p.data)]
    assert abs(f.seconds - (len(pkts) - 1) / 25) < 0.01
    if bf == "2":
        assert any(pts != dts for _, _, dts, pts in f.packets)      # the pictures keep their order of showing
        assert sorted(pts for _, _, _, pts in f.packets) != [pts for _, _, _, pts in f.packets]


def test_no_codec_is_opened_for_a_copied_stream(tmp_path):
    cam, pkts = packets(camera_file(tmp_path))
    w = copy_writer(tmp_path / "out.mp4", pkts[0])
    for p in pkts:
        w.write_packet(p)
    assert not w.v_stream.codec_context.is_open
    w.close()


@pytest.mark.skipif(not has_encoder("libx265"), reason="no libx265 in this FFmpeg to make an HEVC stream with")
def test_an_hevc_camera_s_packets_reach_the_file_as_they_are(tmp_path):
    cam, pkts = packets(camera_file(tmp_path, codec="libx265"))
    assert cam.codec == "hevc" and pkts[0].sets().count(b"\0\0\0\1") == 3              # VPS, SPS, PPS
    out = tmp_path / "out.mp4"
    written(out, pkts)
    f = read(out)
    assert (f.codec, f.tag) == ("hevc", "hvc1") and f.frames == len(pkts)
    assert [s for _, data, _, _ in f.packets for s in slices(data, True, True)] == [s for p in pkts for s in slices(p.data, True)]


def test_packets_that_come_from_an_mp4_are_written_as_they_are(tmp_path, test_clip):
    cam, pkts = packets(str(test_clip))
    out = tmp_path / "out.mp4"
    written(out, pkts)
    f = read(out)
    assert f.codec == "h264" and f.frames == len(pkts) == 75
    assert [data for _, data, _, _ in f.packets] == [p.data for p in pkts]


def test_what_refers_to_a_missing_packet_is_left_out(tmp_path, caplog):
    cam, pkts = packets(camera_file(tmp_path))
    out = tmp_path / "out.mp4"
    with caplog.at_level(logging.INFO):
        written(out, pkts[:14] + pkts[15:])                 # one of the second group was dropped on the way
    f = read(out)
    assert len(f.packets) == f.frames == len(pkts) - (2 * GOP - 14)
    assert "left out" in caplog.text and "key picture" in caplog.text


def test_a_file_never_begins_in_the_middle_of_a_group(tmp_path):
    cam, pkts = packets(camera_file(tmp_path))
    out = tmp_path / "out.mp4"
    w = copy_writer(out, pkts[GOP])
    for p in pkts[3:]:                                      # (the recorder never does this: the writer's own care)
        w.write_packet(p)
    w.close()
    f = read(out)
    assert f.packets[0][0] and len(f.packets) == f.frames == len(pkts) - GOP


def test_a_new_connection_of_the_same_stream_goes_on_in_the_file(tmp_path):
    src = camera_file(tmp_path)
    cam, pkts = packets(src)
    again, more = packets(src)                              # the camera after a reconnect
    out = tmp_path / "out.mp4"
    w = copy_writer(out, pkts[0])
    for p in pkts[:25]:
        w.write_packet(p)
    assert w.takes(more[0]) and w.cam is again
    for p in more[:25]:
        w.write_packet(p)
    w.close()
    f = read(out)
    assert len(f.packets) == f.frames == 50


def test_another_size_or_other_parameter_sets_do_not_fit_the_file(tmp_path):
    cam, pkts = packets(camera_file(tmp_path))
    w = copy_writer(tmp_path / "out.mp4", pkts[0])
    w.write_packet(pkts[0])
    assert w.takes(pkts[1]) and w.takes(pkts[GOP])
    _, small = packets(camera_file(tmp_path, "small.ts", size=(160, 96)))
    assert not w.takes(small[0])
    _, other = packets(camera_file(tmp_path, "other.ts", profile="high", preset="medium"))
    assert other[0].sets() != pkts[0].sets() and not w.takes(other[0])
    # ... also in the middle of a connection: the same stream object, a key picture with other sets
    changed = CamPacket(other[0].data, pkts[GOP].pts, pkts[GOP].dts, pkts[GOP].time_base, True, cam)
    assert not w.takes(changed)
    assert w.cam is cam
    w.close()


def test_copy_is_refused_where_the_container_does_not_carry_the_codec(tmp_path):
    cam, pkts = packets(camera_file(tmp_path))
    cam.codec, cam.hevc = "hevc", True                      # an HEVC camera, and an RTMP server's container
    with pytest.raises(CopyRefused, match="flv carries h264 only, and the camera sends hevc"):
        copy_writer(tmp_path / "out.flv", pkts[0])
    assert not os.path.exists(tmp_path / "out.flv")


def test_copy_is_refused_for_a_stream_that_cannot_be_handed_on(tmp_path):
    cam, pkts = packets(camera_file(tmp_path))
    cam.template, cam.why = None, "the camera's rawvideo stream cannot be handed on as it is: no"
    with pytest.raises(CopyRefused, match="cannot be handed on"):
        copy_writer(tmp_path / "out.mp4", pkts[0])
    cam2, pkts2 = packets(camera_file(tmp_path, "b.ts"))
    cam2.extradata = b""
    with pytest.raises(CopyRefused, match="names no parameter sets"):
        copy_writer(tmp_path / "out.mp4", pkts2[1])         # a picture without sets, a connection that named none


def test_parameter_sets_come_from_the_connection_where_the_pictures_carry_none(tmp_path):
    """A camera that names its parameter sets in the SDP and never sends them (the four 10 fps cameras)."""
    cam, pkts = packets(camera_file(tmp_path))
    sets = pkts[0].sets()
    bare = [CamPacket(p.data[p.data.index(b"\0\0\1\x65") - 1:] if p.key else p.data, p.pts, p.dts, p.time_base, p.key, cam)
            for p in pkts]
    assert bare[0].sets() == b"" and cam.header() == sets
    out = tmp_path / "out.mp4"
    written(out, bare)
    assert read(out).frames == len(pkts)


def test_an_encoder_s_options_are_named_once_and_not_used(tmp_path, caplog):
    cam, pkts = packets(camera_file(tmp_path))
    avwriter._WARNED.clear()
    with caplog.at_level(logging.WARNING):
        written(tmp_path / "a.mp4", pkts, enc_v_opts={"b": "4M", "g": "60", "s": "160x96"}, vf="hflip")
        written(tmp_path / "b.mp4", pkts, enc_v_opts={"b": "4M", "g": "60", "s": "160x96"}, vf="hflip")
    said = [r.getMessage() for r in caplog.records if "-e copy" in r.getMessage()]
    assert len(said) == 1 and all(x in said[0] for x in ("-b 4M", "-g 60", "-s 160x96", "-vf hflip", "are not used"))
    assert read(tmp_path / "a.mp4").size == (320, 180)      # the camera's size, whatever -s said


def test_the_sound_is_encoded_beside_copied_pictures(tmp_path):
    cam, pkts = packets(camera_file(tmp_path))
    out = tmp_path / "out.mp4"
    w = copy_writer(out, pkts[0], audio_enable=True, sample_rate=8000, channels=1, audio_wait=0)
    tone = (np.sin(np.arange(320) * 0.3) * 8000).astype(np.int16).reshape(1, -1)
    for i, p in enumerate(pkts):
        w.write_packet(p)
        a = av.AudioFrame.from_ndarray(tone, format="s16", layout="mono")
        a.sample_rate, a.pts, a.time_base = 8000, i * 320, Fraction(1, 8000)
        w.write_audio(a)
    w.close()
    f = read(out)
    assert f.frames == len(pkts) and f.audio == ["aac"] and w.a_frames == len(pkts)


# ---------------------------------------------------------------- the recorder

class Worker:
    """The decoder's side as the recorder and the streamer see it."""
    def __init__(self, v_encoder=None, codec="h264"):
        self.rec_on = self.stream_on = False
        self.asked, self.refused, self.codec, self.v_encoder = {}, {}, codec, v_encoder

    def width(self): return 320
    def height(self): return 180
    def set_rec_on(self, v): self.rec_on = v
    def set_stream_on(self, v): self.stream_on = v
    def set_rec_copy(self, v): self.asked["recorder"] = v
    def set_stream_copy(self, v): self.asked["streamer"] = v
    def refuse_copy(self, who, codec): self.refused[who] = codec
    def allow_copy(self, who): self.refused.pop(who, None)
    def get_fps(self): return 25
    def get_stream_codec(self): return self.codec
    def get_sample_rate(self): return 0
    def get_channels(self): return 0
    def get_gpu_idx(self): return 0

    def get_ffopts(self):
        return SimpleNamespace(enc_v={"preset": "ultrafast"}, enc_a={}, mux={}, v_encoder=self.v_encoder,
                               no_audio=True, v_tag=None, a_encoder=None, vf=None, metadata=None)


def copy_recorder(tmp_path, monkeypatch, encoder="copy", worker=None):
    monkeypatch.chdir(tmp_path)
    r = recorder.AVRecorder("cam", queue.Queue(), worker or Worker(), None, encoder)
    r.set_dir(str(tmp_path / "videos"))
    r.flag = True
    r._open_writer()
    return r


def picture(i):
    f = av.VideoFrame.from_ndarray(np.full((180, 320, 3), (i * 3) % 256, np.uint8), format="rgb24")
    f.pts, f.time_base = i, Fraction(1, 25)
    return f


def test_the_recorder_asks_the_decoder_for_packets(app, tmp_path, monkeypatch):
    for encoder, ffmpeg, want in (("copy", None, True), (None, "copy", True), ("libx264", "copy", False), (None, None, False)):
        w = Worker(v_encoder=ffmpeg)
        w.refused["recorder"] = "hevc"                      # what an earlier recording found
        r = recorder.AVRecorder("cam", queue.Queue(), w, None, encoder)
        r.set_dir(str(tmp_path / "videos"))
        monkeypatch.setattr(r, "_open_writer", lambda: None)
        r.on()
        try:
            assert w.asked == {"recorder": want} and w.rec_on and w.refused == {}
        finally:
            r.off()


def test_a_copied_recording_begins_with_a_key_picture(app, tmp_path, monkeypatch):
    cam, pkts = packets(camera_file(tmp_path))
    r = copy_recorder(tmp_path, monkeypatch)
    for p in pkts[4:]:                                      # switched on in the middle of a group
        r.write(PACKET, p)
    assert r.writer.copy and r.encoder_note() is None
    r._close_writer()
    (name,) = segments(tmp_path / "videos" / "cam")
    f = read(tmp_path / "videos" / "cam" / name)
    assert f.packets[0][0] and len(f.packets) == f.frames == len(pkts) - GOP


def test_a_copied_segment_ends_in_front_of_the_first_key_picture_past_its_boundary(app, tmp_path, monkeypatch):
    cam, pkts = packets(camera_file(tmp_path, n=100))
    r = copy_recorder(tmp_path, monkeypatch)
    r.set_interval(3600)
    clock = Clock(local(2026, 11, 19, 10, 59, 58) + 0.5)
    monkeypatch.setattr(recorder, "time", clock)
    for p in pkts:                                          # 25 a second: the hour turns at the 38th
        r.write(PACKET, p)
        clock.t += 1 / 25
    r._close_writer()
    first, second = segments(tmp_path / "videos" / "cam")
    assert first.startswith("20261119_1059_1100") and second.startswith("20261119_1100_")
    a, b = read(tmp_path / "videos" / "cam" / first), read(tmp_path / "videos" / "cam" / second)
    assert len(a.packets) == 4 * GOP and a.frames == 4 * GOP       # up to the key picture that follows 11:00:00
    assert b.packets[0][0] and len(b.packets) == b.frames == len(pkts) - 4 * GOP


def test_a_camera_that_sends_another_stream_gets_a_new_file(app, tmp_path, monkeypatch):
    cam, pkts = packets(camera_file(tmp_path))
    _, small = packets(camera_file(tmp_path, "small.ts", size=(160, 96)))
    r = copy_recorder(tmp_path, monkeypatch)
    for p in pkts[:30] + small[:30]:
        r.write(PACKET, p)
    r._close_writer()
    files = [read(tmp_path / "videos" / "cam" / n) for n in segments(tmp_path / "videos" / "cam")]
    assert sorted((f.size, f.frames) for f in files) == [((160, 96), 30), ((320, 180), 30)]


def test_copy_that_cannot_be_done_is_encoded_and_says_so(app, tmp_path, monkeypatch, caplog):
    cam, pkts = packets(camera_file(tmp_path))
    cam.template, cam.why = None, "the camera's h264 stream cannot be handed on as it is: no"
    w = Worker()
    r = copy_recorder(tmp_path, monkeypatch, worker=w)
    with caplog.at_level(logging.WARNING):
        for p in pkts[:GOP + 1]:                            # two key pictures: asked twice, said once
            r.write(PACKET, p)
    assert r.writer is None and w.refused == {"recorder": "h264"}
    said = [x.getMessage() for x in caplog.records if "-e copy is not possible here" in x.getMessage()]
    assert len(said) == 1 and "cannot be handed on" in said[0] and "encoded in its place" in said[0]
    assert r.encoder_note() == "encoded in place of -e copy (the camera's h264 stream cannot be handed on as it is: no)"
    for i in range(10):                                     # the decoder sends pictures from here on
        r.write(FRAME, picture(i))
    assert not r.writer.copy and r.writer.v_encoder == "libx264"
    assert r.encoder_note() == ("libx264 on the CPU in place of -e copy "
                                "(the camera's h264 stream cannot be handed on as it is: no)")
    r._close_writer()
    (name,) = segments(tmp_path / "videos" / "cam")
    assert read(tmp_path / "videos" / "cam" / name).frames == 10


def test_packets_and_pictures_never_share_a_file(app, tmp_path, monkeypatch):
    """A camera whose stream can be copied again (it went back to H.264): the encoded file ends there."""
    cam, pkts = packets(camera_file(tmp_path))
    r = copy_recorder(tmp_path, monkeypatch)
    for i in range(10):
        r.write(FRAME, picture(i))
    assert not r.writer.copy
    for p in pkts[3:25]:                                    # the first of them are in the middle of a group
        r.write(PACKET, p)
    assert r.writer.copy and r.encoder_note() is None
    r._close_writer()
    files = [read(tmp_path / "videos" / "cam" / n) for n in segments(tmp_path / "videos" / "cam")]
    assert sorted(f.frames for f in files) == [10, 25 - GOP]


def test_sound_goes_into_a_copied_file_as_into_any(app, tmp_path, monkeypatch):
    cam, pkts = packets(camera_file(tmp_path))
    w = Worker()
    w.get_sample_rate, w.get_channels = (lambda: 8000), (lambda: 1)
    w.get_ffopts = lambda: SimpleNamespace(enc_v={}, enc_a={}, mux={}, v_encoder=None, no_audio=False, v_tag=None,
                                           a_encoder=None, vf=None, metadata=None)
    r = copy_recorder(tmp_path, monkeypatch, worker=w)
    tone = (np.sin(np.arange(320) * 0.3) * 8000).astype(np.int16).reshape(1, -1)
    for i, p in enumerate(pkts):
        a = av.AudioFrame.from_ndarray(tone, format="s16", layout="mono")
        a.sample_rate, a.pts, a.time_base = 8000, i * 320, Fraction(1, 8000)
        r.write(AUDIO, a)                                   # the first of it comes before the file exists
        r.write(PACKET, p)
    r._close_writer()
    (name,) = segments(tmp_path / "videos" / "cam")
    f = read(tmp_path / "videos" / "cam" / name)
    assert f.frames == len(pkts) and f.audio == ["aac"]


# ---------------------------------------------------------------- the streamer

def test_a_stream_is_sent_as_the_camera_sends_it(app, tmp_path):
    cam, pkts = packets(camera_file(tmp_path))
    w = Worker()
    s = streamer.AVStreamer(str(tmp_path / "out.flv"), queue.Queue(), w, None, "copy")
    s._on = True
    s._open_writer()
    for p in pkts[2:]:
        s.write(PACKET, p)
        time.sleep(0.002)
    assert s.writer.copy and s.encoder_note() is None
    s._close_writer()
    f = read(tmp_path / "out.flv")
    assert f.codec == "h264" and f.packets[0][0] and f.frames == len(f.packets) > 0


def test_a_stream_the_server_s_container_cannot_carry_is_encoded_and_says_so(app, tmp_path, caplog):
    cam, pkts = packets(camera_file(tmp_path))
    cam.codec, cam.hevc = "hevc", True
    w = Worker(codec="hevc")
    s = streamer.AVStreamer(str(tmp_path / "out.flv"), queue.Queue(), w, None, "copy")
    s._on = True
    s._open_writer()
    with caplog.at_level(logging.WARNING):
        for p in pkts[:GOP + 1]:
            s.write(PACKET, p)
    assert s.writer is None and w.refused == {"streamer": "hevc"}
    assert caplog.text.count("-e copy is not possible here") == 1
    for i in range(5):
        s.write(FRAME, picture(i))
    assert s.encoder_note() == "libx264 on the CPU in place of -e copy (flv carries h264 only, and the camera sends hevc)"
    s._close_writer()


# ---------------------------------------------------------------- the decoder's thread

def a_worker(tmp_path):
    from videoworker import VideoWorker
    return VideoWorker(str(tmp_path / "none.ts"), 30, False, None, True, None)


def raw_packets(path):
    c = av.open(path)
    vs = c.streams.video[0]
    return c, vs, [p for p in c.demux(vs) if p.dts is not None]


def test_packets_are_handed_on_from_a_key_picture(app, tmp_path):
    c, vs, pkts = raw_packets(camera_file(tmp_path))
    w = a_worker(tmp_path)
    w.codec_name = "h264"
    cam, handed = CamStream(vs), {"recorder": False, "streamer": False}
    w.set_rec_copy(True)
    for p in pkts[:5]:
        w._hand_on(p, cam, handed)
    assert w.rec_q.empty()                                  # nobody records yet
    w.set_rec_on(True)
    for p in pkts[5:25]:
        w._hand_on(p, cam, handed)
    got = [w.rec_q.get_nowait() for _ in range(w.rec_q.qsize())]
    assert [t for t, _ in got] == [PACKET] * 15 and got[0][1].key and got[0][1].data == bytes(pkts[GOP])
    assert w.stream_q.empty()
    # switched off and on again: from the next key picture
    w.set_rec_on(False)
    w._hand_on(pkts[25], cam, handed)
    w.set_rec_on(True)
    for p in pkts[26:31]:
        w._hand_on(p, cam, handed)
    assert w.rec_q.qsize() == 1 and w.rec_q.get_nowait()[1].key
    # both write packets: one copy of each for the two of them
    w.set_stream_copy(True); w.set_stream_on(True)
    for p in pkts[40:42]:
        w._hand_on(p, cam, handed)
    assert w.rec_q.qsize() == 2 == w.stream_q.qsize() and w.rec_q.get_nowait()[1] is w.stream_q.get_nowait()[1]
    c.close()


def test_no_packet_is_handed_on_before_the_picture_s_size_is_known(app, tmp_path):
    c, vs, pkts = raw_packets(camera_file(tmp_path))
    w = a_worker(tmp_path)
    w.codec_name = "h264"
    cam, handed = CamStream(vs), {"recorder": False, "streamer": False}
    cam.width = cam.height = 0                              # a connection that did not say
    w.set_rec_copy(True); w.set_rec_on(True)
    for p in pkts[:12]:
        w._hand_on(p, cam, handed)
    assert w.rec_q.empty()
    cam.width, cam.height = 320, 180                        # the first decoded picture said
    for p in pkts[12:22]:
        w._hand_on(p, cam, handed)
    assert w.rec_q.qsize() == 2 and w.rec_q.get_nowait()[1].key
    c.close()


def test_pictures_are_queued_only_for_who_encodes(app, tmp_path):
    w = a_worker(tmp_path)
    w.codec_name = "h264"
    w.set_rec_on(True); w.set_stream_on(True)
    w.set_rec_copy(True)
    w._dispatch_av(FRAME, picture(0))
    assert w.rec_q.empty() and w.stream_q.qsize() == 1      # the streamer encodes, the recorder copies
    sound = av.AudioFrame.from_ndarray(np.zeros((1, 320), np.int16), format="s16", layout="mono")
    sound.sample_rate, sound.pts, sound.time_base = 8000, 0, Fraction(1, 8000)
    w._dispatch_av(AUDIO, sound)
    assert w.rec_q.qsize() == 1 and w.stream_q.qsize() == 2 # sound goes to both, as ever
    w.refuse_copy("recorder", "h264")                       # its container does not carry what the camera sends
    assert not w._wants_packets("recorder")
    w._dispatch_av(FRAME, picture(1))
    assert w.rec_q.qsize() == 2
    w.codec_name = "hevc"                                   # the camera sends another codec: asked again
    assert w._wants_packets("recorder")
    w.allow_copy("recorder")
    w.codec_name = "h264"
    assert w._wants_packets("recorder")


def test_nothing_is_decoded_where_nothing_takes_pictures(app, tmp_path):
    c, vs, _ = raw_packets(camera_file(tmp_path))
    cam = CamStream(vs)
    w = a_worker(tmp_path)                                  # headless
    w.codec_name = "h264"
    assert w._pictures_wanted(None)                         # nobody asked for packets: as ever
    w.set_rec_copy(True); w.set_rec_on(True)
    assert not w._pictures_wanted(cam)                      # the recorder writes packets, and there is no window
    w.set_stream_on(True)
    assert w._pictures_wanted(cam)                          # ... but the streamer encodes
    w.set_stream_copy(True)
    assert not w._pictures_wanted(cam)
    w.refuse_copy("streamer", "h264")                       # its server's container does not carry the codec
    assert w._pictures_wanted(cam)
    w.allow_copy("streamer")
    cam.width = 0
    assert w._pictures_wanted(cam)                          # the size of the picture has to come from one
    cam.width = 320
    w.headless = False
    assert w._pictures_wanted(cam)                          # a window shows them
    c.close()


def test_an_accel_that_is_not_in_use_is_not_named_where_nothing_is_decoded(app, tmp_path):
    w = a_worker(tmp_path)
    w.accel, w._hw_fail, w.decoder, w.decoder_hw, w._session_frames = "cuda", "h264_cuvid: no", "h264", False, 5
    assert "decoded on the CPU" in w.accel_note()
    w.decoder = ""                                          # the decoder was let go: -e copy, no window
    assert w.accel_note() is None


# ---------------------------------------------------------------- end to end

def test_nova_records_a_camera_as_it_sends(tmp_path):
    src = camera_file(tmp_path, n=75)
    _, pkts = packets(src)
    outdir = tmp_path / "rec"
    cmd = [sys.executable, "-u", os.path.join(ROOT, "nova.py"), "--headless", "-r", "-i", src, "-n", "copied",
           "--recording-dir", str(outdir), "-e", "copy", "--ffmpeg", "-b:v 4M", "-l", "INFO"]
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {}
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=ROOT)
    # (the log is UTF-8, its warning sign included: Windows would read it as cp1252 and stop at that)
    p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", **kw)
    try:
        time.sleep(7)
        assert p.poll() is None, "nova.py exited early:\n" + p.stdout.read()
        p.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        try:
            out, _ = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
            assert WINDOWS, "nova.py did not quit on SIGINT:\n" + out
    finally:
        if p.poll() is None:
            p.kill()
    assert "copy (h264 as the camera sends it)" in out and "libx264" not in out, out
    # the first picture is decoded (it starts the recording); nothing after it, on no later connection
    assert out.count("First frame (h264") == 1 and "From here on (h264 320x180) not decoded" in out, out
    assert "First packet (h264 320x180) not decoded" in out, out
    assert "-e copy writes the camera's pictures as they come: -b 4M is not used" in out, out
    files = [f for f in glob.glob(str(outdir / "copied" / "*.mp4"))]
    assert files, out
    f = read(files[0])
    assert f.codec == "h264" and f.packets[0][0] and f.frames == len(f.packets) >= GOP, out
    # the file is the clip, key picture to end, over and over (the worker reads a file again when it ends)
    mine = [s for _, data, _, _ in f.packets for s in slices(data, lengths=True)]
    clip = [s for q in pkts for s in slices(q.data)]
    start = clip.index(mine[0])
    n = min(len(mine), len(clip) - start)
    assert n >= GOP and mine[:n] == clip[start:start + n], out
