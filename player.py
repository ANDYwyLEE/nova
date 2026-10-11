
from abc import abstractmethod, ABCMeta
from PySide6.QtCore import QObject
from PySide6.QtGui import QImage, QColor

class QABCMeta(type(QObject), ABCMeta):
    pass

# Declare an abstract base class Player
class Player(QObject, metaclass=QABCMeta):
    def __init__(self, name, iconname):
        super().__init__()
        self.name = name
        self.icon = QImage(iconname)
        self.icon_gray = self._make_gray(self.icon)

    # Define the required abstract methods

    @abstractmethod
    def available(self):
        return True

    @abstractmethod
    def is_on(self): 
        return False

    @abstractmethod
    def is_off(self): 
        return not self.is_on()

    @abstractmethod
    def on(self): pass

    @abstractmethod
    def off(self): pass

    @abstractmethod
    def close(self): pass

    # parent default methods
    def get_icon(self):
        return self.icon if self.is_on() else self.icon_gray


    def _make_gray(self, image):
        # Ensure image has alpha channel
        img = image.convertToFormat(
            QImage.Format_ARGB32).copy()
        for y in range(img.height()):
            for x in range(img.width()):
                pixel = img.pixelColor(x, y)
                if pixel.alpha() == 0:
                    continue
                # Grayscale conversion using luminosity weights
                gray = int(0.299 * pixel.red() + 0.587 * pixel.green() + 0.114 * pixel.blue())
                img.setPixelColor(x, y, QColor(gray, gray, gray, pixel.alpha() // 2 ))
        return img

