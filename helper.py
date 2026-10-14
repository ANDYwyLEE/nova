#[helper.py]

from typing import Dict
from player import Player
from novaargs import option_table
from PySide6.QtCore import Signal


ARG_DESCRIPTIONS = option_table()       # nova.py's options, as --help describes them (novaargs.py)

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
    'Mouse Wheel': 'Zoom in (push the wheel away) or out (pull it toward you) around the cursor; a trackpad pinch on macOS too.',
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