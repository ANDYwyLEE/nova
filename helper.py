#[helper.py]

from typing import Dict
from player import Player
from PySide6.QtCore import Signal


ARG_DESCRIPTIONS = {
    '-i, --input': "Video source: rtsp/rtmp/srt URL, onvif://, xmeye://, hik://, dahua://, '0' (USB cam), dshow://video=NAME",
    '-n, --name, --camera-name' : 'Logical camera name for output recording folders.',
    '--opengl': 'Enable hardware acceleration using OpenGL (recommended for high performance).',
    '--accel': 'HW Acceleration backend. cuda, qsv, vaapi, videotoolbox, d3d11va.',
    '--gpu': 'GPU index for HW decode/encode (0, 1, or a list 0,1 to pick at random; default: NOVA_GPU_DEVICES).',
    '-f, --fps': 'Target video frame rate (default: 30).',
    '-b, --border': 'Enable the colored window border. (default: no border line)',
    '-g, --grid-size': 'Grid size for window alignment in pixels (default: 20).',
    '--geometry': 'Initial window placement x,y,w,h; overrides the remembered one (nova start --layout uses it).',
    '--zoom': 'Initial zoom region as ratios rx,ry,rw,rh of the picture (0,0,1,1 = whole picture).',
    '-a, --audio, --enable-audio': 'Enable audio playback automatically on startup.',
    '--headless, --no-gui': 'Run in headless mode without showing the visual camera window.',
    '-r, --record, --auto-record': 'Start recording automatically on startup.',
    '-e, --encoder': 'Explicitly specify video encoder (e.g., av1_nvenc, hevc_nvenc)',
    '--record-interval': 'Recording segment length (e.g. 10m, 1h, 3600s; default: 30m).',
    '--output-dir': 'Base directory for snapshots and recordings.',
    '--snapshot-dir': 'Directory for snapshots (overrides --output-dir for snapshots).',
    '--recording-dir': 'Directory for recorded videos (overrides --output-dir for recordings).',
    '--ffmpeg' : 'FFmpeg options: -rtsp_transport udp -b:v 4M -crf 23 -g 60 -s WxH -r N -vf transpose=1 -pix_fmt -ar -ac -b:a -metadata k=v -movflags (README lists what works)',
    '-t, --target': 'Live stream URL (e.g. YouTube, Twitch) or output path',
    '-l, --log-level': 'Logging level (NAME or NUMBER; e.g., DEBUG, INFO, 5, 10; default: INFO).',
}

CONTROL_DESCRIPTIONS = {
    'H / F1': 'Show or hide the help/controls overlay.',
    'F / F11': 'Toggle fullscreen mode.',
    'A / F5': 'Enable or disable audio playback.',
    'R / F7': 'Toggle video recording to disk.',
    'T / F8': 'Toggle transmitting live stream (e.g. YouTube, Twitch).',
    'S': 'Save a snapshot to disk.',
    'B': 'Toggle the window border on or off.',
    'P': 'Print the stream URL (the password only to a terminal, masked in a log file).',
    'ESC': 'Reset the zoom; leave fullscreen.',
    'Home': 'Reset to the video’s original 1:1 size, centred on screen.',
    'Up/Down/Left/Right': 'Move the window by one grid step.',
    'Ctrl + Arrow': 'Grow the window on that edge by one grid step.',
    'Alt + Arrow': 'Shrink the window on that edge by one grid step.',
    'Shift + Arrow': 'Move the zoomed region inside the window (a tenth of the view per step).',
    'PgUp / PgDown': 'Shrink / enlarge the window by 10% (keeps the top-left corner).',
    'I / + / =': 'Zoom in (Ctrl: enlarge the window around its centre).',
    'O / - / _': 'Zoom out (Ctrl: shrink the window around its centre).',
    'Mouse Wheel': 'Zoom in or out around the cursor.',
    'Mouse Double Click': 'Toggle fullscreen mode.',
    'Mouse Right Click': 'Fit the window to the video (or the zoomed region).',
    'Mouse Middle Click': 'Make what the window shows the zoomed region (unzoomed: cut the video to the window shape).',
    'Mouse Drag': 'Move the window.',
    'Ctrl + Mouse Drag': 'Move the zoomed region inside the window.',
    'Mouse Border Drag': 'Resize the window.',
    'Mouse Top-Left Corner': 'Show the control strip (stream, record, audio, help).',
    'Ctrl-C': 'Exit the program (both terminal and GUI).',
}


def generate_help_text(arg_descriptions: Dict[str, str],
                       control_descriptions: Dict[str, str]) -> str:
    arg_lines = ["\n--- Command Line Arguments ---"]
    max_arg_len = max(len(arg) for arg in arg_descriptions.keys())
    for arg, desc in arg_descriptions.items():
        arg_lines.append(f"{arg:<{max_arg_len}} : {desc}")

    control_lines = ["\n--- In-App Controls ---"]
    max_control_len = max(len(ctrl) for ctrl in control_descriptions.keys())
    for ctrl, desc in control_descriptions.items():
        control_lines.append(f"{ctrl:<{max_control_len}} : {desc}")

    return "\n".join(arg_lines + control_lines)



class Helper(Player):
    hlp_sig = Signal(str)

    def __init__(self):
        super().__init__('Helper', 'hlp.png')
        self.flag = False
        self.help_text = generate_help_text(
            ARG_DESCRIPTIONS,
            CONTROL_DESCRIPTIONS
        )

    #@abstractmethod
    def available(self):
        return True

    #@abstractmethod
    def is_on(self): 
        return self.flag

    #@abstractmethod
    def is_off(self):
        return not self.is_on()

    #@abstractmethod
    def on(self):
        if self.is_on(): return
        self._on()

    def _on(self): # Refresh force
        self.flag = True
        self.hlp_sig.emit(self.help_text)

    #@abstractmethod
    def off(self):
        if self.is_off(): return
        self.flag = False
        self.hlp_sig.emit(None)

     #@abstractmethod
    def close(self):
        self.off()

    def toggle_help(self):
        if self.is_on(): self.off()
        else: self.on()

    # Note super().get_icon() - the get_icon method from the base class can be used directly