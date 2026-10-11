#[helper.py]

from typing import Dict
from player import Player
from PySide6.QtCore import Signal


ARG_DESCRIPTIONS = {
    '-i, --input': "Video source (e.g., rtsp, rtmp, srt, onvif, xmeye, '0' for USB cam)",
    '-n, --name, --camera-name' : 'Logical camera name for output recording folders.',
    '--opengl': 'Enable hardware acceleration using OpenGL (recommended for high performance).',
    '--accel': 'HW Acceleration backend. cuda, qsv, vaapi, videotoolbox, d3d11va.',
    '-f, --fps': 'Target video frame rate (default: 30).',
    '-b, --border': 'Enable the colored window border. (default: no border line)',
    '-g, --grid-size': 'Grid size for window alignment in pixels (default: 20).',
    '-a, --audio, --enable-audio': 'Enable audio playback automatically on startup.',
    '--headless, --no-gui': 'Run in headless mode without showing the visual camera window.',
    '-r, --record, --auto-record': 'Start recording automatically on startup.',
    '-e, --encoder': 'Explicitly specify video encoder (e.g., av1_nvenc, hevc_nvenc)',
    '--record-interval': 'Recording segment length (e.g. 10m, 1h, 3600s; default: 30m).',
    '--output-dir': 'Base directory for snapshots and recordings.',
    '--snapshot-dir': 'Directory for snapshots (overrides --output-dir for snapshots).',
    '--recording-dir': 'Directory for recorded videos (overrides --output-dir for recordings).',
    '--ffmpeg' : 'Direct FFmpeg options (e.g., "-rtsp_transport tcp -lowres 1")',
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
    'ESC': 'Restore zoom or exit fullscreen to the previous window size.',
    'Home': 'Reset to the video’s original 1:1 size.',
    'Right (+Ctrl/Alt)': 'Move the window right (Ctrl: expand, Alt: shrink).',
    'Left (+Ctrl/Alt)': 'Move the window left (Ctrl: expand, Alt: shrink).',
    'Up (+Ctrl/Alt)': 'Move the window up (Ctrl: expand, Alt: shrink).',
    'Down (+Ctrl/Alt)': 'Move the window down (Ctrl: expand, Alt: shrink).',
    'Up/Down/Left/Right': 'Move the window by one grid step.',
    'PgUp / PgDown': 'Resize the window.',
    'O / - / _ (+Ctrl)': 'Zoom out (Ctrl: shrink window).',
    'I / + / = (+Ctrl)': 'Zoom in (Ctrl: expand window).',
    'Mouse Wheel': 'Zoom in or out.',
    'Mouse Right Click': 'Fit the frame to the current video size.',
    'Mouse Middle Click': 'Fit the video to the current frame size.',
    'Mouse Drag': 'Move the window.',
    'Mouse Border Drag': 'Resize the window.',
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
    def aclose(self):
        self.off()

    def toggle_help(self):
        if self.is_on(): self.off()
        else: self.on()

    # Note super().get_icon() - the get_icon method from the base class can be used directly