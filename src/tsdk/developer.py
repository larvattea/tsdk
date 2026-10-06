"""Full-screen developer window: the game on the left two thirds, tracking tools on the right third."""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QRegion, QShortcut
from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
                               QSizePolicy, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from ttea.sdk.cartridge import extract_cartridge
from ttea.sdk.embedding import FrameReader
from ttea.sdk.runtime import DEFAULT_RESOLUTION, load_manifest
from ttea.sdk.simulator import write_state
from ttea.sdk.tracking import Landmark, Observation

from .simulator import BODY_EDGES, HAND_EDGES, PALM, DeveloperSimulator, palm_centre

LOW_VISIBILITY = .5
NOSE_TIP = 1  # MediaPipe face mesh vertex
# Camera controls that can drive a body game: label, MediaPipe model, and the landmarks
# drawn enlarged in the preview because they move the feet.
CONTROLS = {'palms': ('Both hands', 'hands', PALM), 'face': ('Face (nose)', 'face', (NOSE_TIP,))}
# Centre of the camera region that spans the whole game at sensitivity 1; its size is the
# editable Camera region (camera pixels), suggested from the game resolution. A palm centre has
# fingers above and the wrist below, so the hands' region sits a little low.
REGION_CENTRE = {'palms': (.5, .55), 'face': (.5, .5)}
# Default gain around the region centre per control; a nose moves far less than a hand.
SENSITIVITY = {'palms': 1., 'face': 2.5}
MOUSE, KEYBOARD, CAMERA = range(3)
BOTTOM_BAR = 48  # px: height of the size label under the game and the button row under the preview
GAMES = Path(__file__).resolve().parents[2]/'games'  # built cartridges in a tsdk checkout
# Keyboard source: W/A/S/D move the left foot, arrow keys the right one, by (side, dx, dy).
FOOT_KEYS = {Qt.Key.Key_W: ('left', 0, -1), Qt.Key.Key_A: ('left', -1, 0), Qt.Key.Key_S: ('left', 0, 1),
             Qt.Key.Key_D: ('left', 1, 0), Qt.Key.Key_Up: ('right', 0, -1), Qt.Key.Key_Left: ('right', -1, 0),
             Qt.Key.Key_Down: ('right', 0, 1), Qt.Key.Key_Right: ('right', 1, 0)}
FOOT_SPEED = .6  # game widths per second while a foot key is held
MOUSE_BUTTONS = {Qt.MouseButton.LeftButton: 1, Qt.MouseButton.MiddleButton: 2, Qt.MouseButton.RightButton: 3}
MODIFIERS = {Qt.KeyboardModifier.ShiftModifier: 'shift', Qt.KeyboardModifier.ControlModifier: 'ctrl',
             Qt.KeyboardModifier.AltModifier: 'alt', Qt.KeyboardModifier.MetaModifier: 'meta'}
# Qt keys outside printable ASCII, by pygame key name.
KEYS = {Qt.Key.Key_Escape: 'escape', Qt.Key.Key_Return: 'return', Qt.Key.Key_Enter: 'return',
        Qt.Key.Key_Space: 'space', Qt.Key.Key_Tab: 'tab', Qt.Key.Key_Backspace: 'backspace',
        Qt.Key.Key_Delete: 'delete', Qt.Key.Key_Insert: 'insert', Qt.Key.Key_Home: 'home', Qt.Key.Key_End: 'end',
        Qt.Key.Key_PageUp: 'page up', Qt.Key.Key_PageDown: 'page down', Qt.Key.Key_Left: 'left',
        Qt.Key.Key_Right: 'right', Qt.Key.Key_Up: 'up', Qt.Key.Key_Down: 'down', Qt.Key.Key_Shift: 'left shift',
        Qt.Key.Key_Control: 'left ctrl', Qt.Key.Key_Alt: 'left alt', Qt.Key.Key_Meta: 'left meta',
        **{getattr(Qt.Key, f'Key_F{n}'): f'f{n}' for n in range(1, 13)}}


def camera_devices():
    """(name, OpenCV index, is system default) for each video input, in system order.

    OpenCV's AVFoundation backend numbers macOS devices by sorted unique ID (so an iPhone
    Continuity Camera can be index 0); on Linux OpenCV opens cameras with V4L2, where index N
    is /dev/videoN; DirectShow on Windows follows system enumeration order.
    """
    devices = list(QMediaDevices.videoInputs())
    if sys.platform.startswith('linux'):
        return [(device.description(), int(bytes(device.id()).decode().removeprefix('/dev/video')), device.isDefault())
                for device in devices]
    numbered = sorted(devices, key=lambda device: bytes(device.id()).decode()) if sys.platform == 'darwin' else devices
    return [(device.description(), numbered.index(device), device.isDefault()) for device in devices]


def key_name(event):
    if event.key() in KEYS:
        return KEYS[event.key()]
    if 0x20 < event.key() <= 0x7e:
        return chr(event.key()).lower()
    return None


def mirrored(observation):
    """Selfie view of camera landmarks: x flipped, so the user's right is the image's right."""
    return replace(observation, landmarks={group: [Landmark(1-p.x, p.y, p.z, p.visibility) for p in points]
                                           for group, points in observation.landmarks.items()})


def fitted(rect, width, height):
    """Largest rect of the given aspect centred in rect."""
    scale = min(rect.width()/width, rect.height()/height)
    result = QRectF(0, 0, width*scale, height*scale)
    result.moveCenter(rect.center())
    return result


def to_screen(x, y, region, sensitivity=1.):
    """Camera-normalized point -> game-normalized point, scaled about the region centre and clamped.

    At sensitivity 1 the region spans the whole game.
    """
    left, top, right, bottom = region
    return tuple(min(max(.5+((value-low)/(high-low)-.5)*sensitivity, 0.), 1.)
                 for value, low, high in ((x, left, right), (y, top, bottom)))


class GameSpace:
    """The camera-driven body as the game receives it.

    The panel poses the body in camera coordinates (feet under the fingertips or nose); the
    game gets the same pose translated so its feet centre maps through the camera region.
    """

    def __init__(self, panel):
        self.panel = panel

    @property
    def stale(self):
        return self.panel.provider.stale

    def read(self):
        provider = self.panel.provider
        control = self.panel.control.currentData()
        (lx, ly), (rx, ry) = provider.foot('left'), provider.foot('right')
        cx, cy = (lx+rx)/2, (ly+ry)/2
        x, y = to_screen(cx, cy, self.panel.camera_region(), self.panel.sensitivity[control])
        observation = provider.read()
        # Keep both feet (heels and toes) inside the game, or the runtime drops the outside foot.
        feet = observation.landmarks['body'][29:33]
        x = min(max(x, cx-min(p.x for p in feet)), 1-(max(p.x for p in feet)-cx))
        y = min(max(y, cy-min(p.y for p in feet)), 1-(max(p.y for p in feet)-cy))
        landmarks = {group: [Landmark(p.x+x-cx, p.y+y-cy, p.z, p.visibility) for p in points]
                     for group, points in observation.landmarks.items()}
        return replace(observation, landmarks=landmarks)


class CameraSource:
    """Live camera detection on a worker thread; read() returns the latest observation without blocking.

    Frames are only previewed in the panel, never stored.
    """
    stale = False

    def __init__(self, profile, camera_index):
        self.profile = profile
        self.frame = None
        self.error = None
        self.loading = True  # opening the camera and loading the model take a few seconds
        self._provider = None
        self._observation = Observation(profile, time.monotonic(), present=False, confidence_source='measured')
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._run, args=(camera_index,), name='tsdk-camera', daemon=True)
        self._thread.start()

    def _run(self, camera_index):
        from ttea.sdk.providers import HardwareProvider
        try:
            self._provider = HardwareProvider(self.profile, camera_index=camera_index)
            while self._running:
                observation = self._provider.read()
                with self._lock:
                    self._observation, self.frame = observation, self._provider.frame
                    self.loading = False
        except Exception as exc:
            self.error = exc
        finally:
            self.loading = False

    def read(self):
        with self._lock:
            return self._observation

    def close(self):
        self._running = False
        self._thread.join()
        if self._provider is not None:
            self._provider.close()


class GameView(QWidget):
    """Shows the runtime's frames, aspect-fitted, and forwards mouse and keyboard input to it."""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.image = None
        self.pixels = None
        self.message = ''
        self.last = (0, 0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

    def show_frame(self, width, height, pixels):
        self.pixels = pixels
        self.image = QImage(pixels, width, height, 4*width, QImage.Format.Format_RGBX8888)
        self.update()

    def clear(self, message):
        self.image = self.pixels = None
        self.message = message
        self.update()

    def target(self):
        """The game's rect on screen: as large as fits, at the game's aspect ratio, centred horizontally and
        resting on the bottom so it lines up with the preview."""
        rect = fitted(QRectF(self.rect()), *self.panel.resolution())
        rect.moveBottom(self.rect().bottom())
        return rect

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.panel.show_size()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#000000'))
        if self.image is None:
            target = self.target()
            painter.fillRect(target, QColor('#0b0f14'))
            if self.panel.show_feet.isChecked():
                self.paint_feet(painter, target)
            painter.setPen(QColor('#94a3b8'))
            painter.drawText(target, Qt.AlignmentFlag.AlignCenter, self.message)
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.target(), self.image)

    def paint_feet(self, painter, target):
        """Without a game, draw the feet the game would receive, exactly as the runtime does."""
        import pygame
        from ttea.sdk.feet import draw_feet
        from ttea.sdk.tracking import TrackingPipeline
        tracking = TrackingPipeline('body').process(self.panel.tracker().read())
        if tracking.missing:
            return
        feet = {side: (point.x, point.y) for side, point in tracking.feet.items()
                if side in ('left', 'right') and not tracking.out_of_area.get(side)}
        surface = pygame.Surface((max(1, int(target.width())), max(1, int(target.height()))), pygame.SRCALPHA)
        draw_feet(surface, feet)
        pixels = pygame.image.tobytes(surface, 'RGBA')
        painter.drawImage(target.topLeft(), QImage(pixels, *surface.get_size(), QImage.Format.Format_RGBA8888))

    def game_position(self, event):
        rect = self.target()
        x = (event.position().x()-rect.x())*self.image.width()/rect.width()
        y = (event.position().y()-rect.y())*self.image.height()/rect.height()
        return (min(max(int(x), 0), self.image.width()-1), min(max(int(y), 0), self.image.height()-1))

    def mouseMoveEvent(self, event):
        if self.image is None:
            return
        position = self.game_position(event)
        buttons = [int(bool(event.buttons() & button)) for button in MOUSE_BUTTONS]
        self.panel.send({'type': 'mouse_motion', 'pos': position,
                         'rel': (position[0]-self.last[0], position[1]-self.last[1]), 'buttons': buttons})
        self.last = position

    def mousePressEvent(self, event):
        self.setFocus()
        self.send_button(event, 'mouse_down')

    def mouseReleaseEvent(self, event):
        self.send_button(event, 'mouse_up')

    def send_button(self, event, kind):
        if self.image is not None and event.button() in MOUSE_BUTTONS:
            self.last = self.game_position(event)
            self.panel.send({'type': kind, 'pos': self.last, 'button': MOUSE_BUTTONS[event.button()]})

    def keyPressEvent(self, event):
        if not self.panel.foot_key(event, True):
            self.panel.forward_key(event, 'key_down')

    def keyReleaseEvent(self, event):
        if not self.panel.foot_key(event, False):
            self.panel.forward_key(event, 'key_up')

    def focusOutEvent(self, event):
        self.panel.held.clear()
        super().focusOutEvent(event)


class HintCard(QWidget):
    """Rounded card whose title sits in the top border line, near the left."""

    TITLE = 'Hint'

    def __init__(self):
        super().__init__()
        self.text = QLabel()
        self.text.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 18, 14, 10)
        layout.addWidget(self.text)

    def setText(self, text):
        self.text.setText(text)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        metrics = painter.fontMetrics()
        top = metrics.height()/2
        box = QRectF(self.rect()).adjusted(1, top, -1, -1)
        title = QRectF(box.x()+12, 0, metrics.horizontalAdvance(self.TITLE)+12, metrics.height())
        painter.setPen(QPen(QColor('#64748b'), 1))
        painter.setClipRegion(QRegion(self.rect()).subtracted(QRegion(title.toRect())))
        painter.drawRoundedRect(box, 6, 6)
        painter.setClipping(False)
        painter.setPen(QColor('#94a3b8'))
        painter.drawText(title, Qt.AlignmentFlag.AlignCenter, self.TITLE)


class LandmarkCanvas(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.grab_offset = None  # skeleton centre minus cursor while dragging, normalized
        self.setMinimumSize(200, 200)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def area(self):
        """Unit square on screen at the camera frame's aspect, or the game's without a camera frame."""
        rect = QRectF(16, 16, self.width()-32, self.height()-32)
        frame = self.panel.camera.frame if self.panel.camera else None
        if frame is None:
            return fitted(rect, *self.panel.resolution())
        height, width = frame.shape[:2]
        return fitted(rect, width, height)

    def point(self, landmark):
        area = self.area()
        return QPointF(area.x() + landmark.x * area.width(), area.y() + landmark.y * area.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#18202b'))
        area = self.area()
        camera = self.panel.camera
        frame = camera.frame if camera else None
        if frame is not None:
            height, width = frame.shape[:2]
            image = QImage(frame.data, width, height, 3*width, QImage.Format.Format_RGB888)
            # Driving controls use a selfie view so moving right moves the body right.
            painter.drawImage(area, image.flipped(Qt.Orientation.Horizontal) if self.panel.driving else image)
        painter.setPen(QPen(QColor('#64748b'),1))
        painter.drawRect(area)
        if self.panel.driving:
            left, top, right, bottom = self.panel.camera_region()
            painter.setPen(QPen(QColor('#c4b5fd'), 1, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(area.x()+left*area.width(), area.y()+top*area.height(),
                                    (right-left)*area.width(), (bottom-top)*area.height()))
        driving = self.panel.driving
        # When hands or a face drive the body, draw the detection (violet, control points enlarged) under it.
        layers = [(mirrored(camera.read()), True)] if driving else []
        layers.append((self.panel.observation(), False))
        for observation, driver in layers:
            for group,points in observation.landmarks.items():
                base = QColor('#c4b5fd' if driver else ('#5eead4' if group != 'right' else '#fbbf24'))
                faded = not observation.present
                edges = BODY_EDGES if group == 'body' else (HAND_EDGES if group != 'face' else [])
                def color(*indices):
                    result = QColor(base)
                    if faded or any(points[i].visibility is not None and points[i].visibility < LOW_VISIBILITY for i in indices):
                        result.setAlpha(65)
                    return result
                for a,b in edges:
                    painter.setPen(QPen(color(a, b), 2 if camera else 1))
                    painter.drawLine(self.point(points[a]),self.point(points[b]))
                painter.setPen(Qt.PenStyle.NoPen)
                for index,point in enumerate(points):
                    painter.setBrush(color(index))
                    control = driver and index in CONTROLS[self.panel.control.currentData()][2]
                    radius = 5 if control else (2 if group == 'face' else 4)
                    painter.drawEllipse(self.point(point),radius,radius)
        if camera is not None and camera.loading:
            painter.fillRect(area, QColor(24, 32, 43, 200))
            centre = area.center()
            painter.setPen(QPen(QColor('#c4b5fd'), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            spin = QRectF(centre.x()-18, centre.y()-30, 36, 36)
            painter.drawArc(spin, int(-time.monotonic()*360*16) % (360*16), 270*16)
            painter.setPen(QColor('#cbd5e1'))
            painter.drawText(QRectF(area.x(), centre.y()+14, area.width(), 24), Qt.AlignmentFlag.AlignCenter,
                             'Loading camera…')

    def normalized(self, event):
        area = self.area()
        return ((event.position().x()-area.x())/area.width(), (event.position().y()-area.y())/area.height())

    def mousePressEvent(self, event):
        """Mouse source: grab the skeleton anywhere; it keeps its offset from the cursor while dragged."""
        if self.panel.source.currentIndex() != MOUSE or event.button() != Qt.MouseButton.LeftButton:
            return
        x, y = self.normalized(event)
        (lx, ly), (rx, ry) = self.panel.provider.foot('left'), self.panel.provider.foot('right')
        self.grab_offset = ((lx+rx)/2-x, (ly+ry)/2-y)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.grab_offset is not None:
            x, y = self.normalized(event)
            self.panel.move_body(x+self.grab_offset[0], y+self.grab_offset[1])

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.grab_offset is not None:
            self.grab_offset = None
            self.unsetCursor()

    def keyPressEvent(self, event):
        if not self.panel.foot_key(event, True):
            self.panel.forward_key(event, 'key_down')

    def keyReleaseEvent(self, event):
        if not self.panel.foot_key(event, False):
            self.panel.forward_key(event, 'key_up')

    def focusOutEvent(self, event):
        self.panel.held.clear()
        super().focusOutEvent(event)


class DeveloperPanel(QMainWindow):
    def __init__(self, workdir, project):
        super().__init__()
        self.workdir = workdir
        self.state_path = workdir/'simulation.json'
        self.project = project.resolve()  # Load game starts browsing here; nothing loads at startup.
        self.manifest = None
        self.process = None
        self.frames = None
        self.launches = 0
        self.cartridges = 0
        self.provider = DeveloperSimulator('body')
        self.paused = False
        self.camera = None
        self.driven = None
        self.sensitivity = dict(SENSITIVITY)
        self.game_space = GameSpace(self)
        self.held = set()
        self.ticked = time.monotonic()
        self.root_splitter = root = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(root)
        game = QWidget()
        game_column = QVBoxLayout(game)
        game_column.setContentsMargins(0, 0, 0, 0)
        game_column.setSpacing(0)
        self.game_view = GameView(self)
        game_column.addWidget(self.game_view, 1)
        self.size_label = QLabel()
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.size_label.setFixedHeight(BOTTOM_BAR)
        self.size_label.setStyleSheet('color: #94a3b8;')
        game_column.addWidget(self.size_label)
        root.addWidget(game)
        side = QWidget()
        side.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        root.addWidget(side)
        root.setStretchFactor(0, 2)
        root.setStretchFactor(1, 1)
        column = QVBoxLayout(side)
        # No bottom margin: the game, the preview, and the bottom bars of both columns line up.
        column.setContentsMargins(12, 12, 12, 0)
        column.setSpacing(0)
        self.tools_splitter = tools = QSplitter(Qt.Orientation.Vertical)
        self.sized = False
        column.addWidget(tools, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tools.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        controls = QVBoxLayout(content)
        self.form = form = QFormLayout()
        # Parameters sit in one centred column: labels right-aligned, fields left-aligned.
        form.setFormAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        controls.addLayout(form)
        self.source = QComboBox()
        self.source.addItems(['Mouse', 'Keyboard', 'Camera'])
        self.source.setCurrentIndex(MOUSE)
        self.source.setToolTip('Mouse: drag the skeleton in the preview to move the whole body. Keyboard: W/A/S/D move the '
                               'left foot, arrow keys the right foot. Camera: live detection.')
        form.addRow('Tracking source', self.source)
        self.camera_device = QComboBox()
        for name, camera_index, default in camera_devices():
            self.camera_device.addItem(name, camera_index)
            if default:
                self.camera_device.setCurrentIndex(self.camera_device.count()-1)
        form.addRow('Camera', self.camera_device)
        self.control = QComboBox()
        for control, (label, _, _) in CONTROLS.items():
            self.control.addItem(label, control)
        self.control.setToolTip('What the camera watches to move the body skeleton: your nose (the centre of '
                                'both feet) or both hands (one per foot).')
        form.addRow('Camera control', self.control)
        self.sensitivity_box = QDoubleSpinBox()
        self.sensitivity_box.setRange(.5, 6.)
        self.sensitivity_box.setSingleStep(.1)
        self.sensitivity_box.setDecimals(1)
        self.sensitivity_box.setToolTip('Gain around the centre of the dashed camera region. At 1 the region spans '
                                        'the whole game; raise it when the hand or nose cannot reach the game edges.')
        self.sensitivity_box.valueChanged.connect(self.set_sensitivity)
        form.addRow('Camera sensitivity', self.sensitivity_box)
        self.frame_size = DEFAULT_RESOLUTION  # camera pixels, updated from live frames
        self.region = QWidget()
        row = QHBoxLayout(self.region)
        row.setContentsMargins(0, 0, 0, 0)
        self.region_width, self.region_height = QSpinBox(), QSpinBox()
        for spin, size in ((self.region_width, self.frame_size[0]), (self.region_height, self.frame_size[1])):
            spin.setRange(16, size)
            spin.setSuffix(' px')
        row.addWidget(self.region_width)
        row.addWidget(QLabel('×'))
        row.addWidget(self.region_height)
        self.region.setToolTip('Size of the dashed camera region (camera pixels) that spans the whole game at '
                               'sensitivity 1. Suggested from the game resolution, scaled down to fit the camera.')
        form.addRow('Camera region', self.region)
        self.suggest_region()
        for spin in (self.region_width, self.region_height):
            spin.valueChanged.connect(lambda value: self.canvas.update())
        self.present = QCheckBox()
        self.present.setChecked(True)
        self.present.toggled.connect(self.set_present)
        form.addRow('Participant present', self.present)
        self.show_feet = QCheckBox()
        self.show_feet.setChecked(True)
        self.show_feet.setToolTip('Draw the player\'s footprints in the game frame.')
        self.show_feet.toggled.connect(self.toggle_feet)
        form.addRow('Show feet indicator', self.show_feet)
        self.developer = QCheckBox()
        self.developer.setChecked(True)
        self.developer.setToolTip('Let the tracking source move the skeleton.')
        form.addRow('Enable developer mode', self.developer)
        controls.addStretch()
        self.hint = HintCard()
        controls.addWidget(self.hint)
        self.canvas = LandmarkCanvas(self)
        tools.addWidget(self.canvas)
        tools.setStretchFactor(0, 1)
        tools.setStretchFactor(1, 1)
        bar = QWidget()
        bar.setFixedHeight(BOTTOM_BAR)
        column.addWidget(bar)
        buttons = QHBoxLayout(bar)
        buttons.setContentsMargins(0, 0, 0, 0)
        load = QPushButton('Load game')
        self.load_menu = QMenu(load)
        self.load_menu.aboutToShow.connect(self.fill_load_menu)
        self.fill_load_menu()
        load.setMenu(self.load_menu)
        buttons.addWidget(load)
        self.restart_button = QPushButton('Restart game')
        self.restart_button.clicked.connect(self.restart_game)
        buttons.addWidget(self.restart_button)
        self.pause_button = QPushButton('Pause game')
        self.pause_button.clicked.connect(self.toggle_pause)
        buttons.addWidget(self.pause_button)
        self.stop_button = QPushButton('Stop game')
        self.stop_button.clicked.connect(self.stop_game)
        buttons.addWidget(self.stop_button)
        self.exit_button = QPushButton('Exit')
        self.exit_button.clicked.connect(self.close)
        buttons.addWidget(self.exit_button)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Quit), self, self.close)
        QShortcut(QKeySequence(Qt.Key.Key_F2), self, self.toggle_pause)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, self.restart_game)
        self.source.currentIndexChanged.connect(self.set_source)
        self.camera_device.currentIndexChanged.connect(self.change_camera)
        self.control.currentIndexChanged.connect(self.change_control)
        self.show_source()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)
        self.game_view.clear('No game loaded · use Load game')

    def observation(self):
        """What the game receives."""
        return self.provider.observation

    @property
    def driving(self):
        """The camera detects hands or a face that drive the body skeleton."""
        return self.camera is not None

    def tracker(self):
        """What write_state sends to the game."""
        return self.game_space if self.driving else self.provider

    def drive(self, observation):
        """Move the body skeleton from both hands or the nose; other observations hold the pose."""
        observation = mirrored(observation)
        control = self.control.currentData()
        if control == 'palms':
            tips = [tip for tip in (palm_centre(observation.landmarks.get(side)) for side in ('left', 'right'))
                    if tip is not None]
            if len(tips) == 2:
                a, b = sorted(tips, key=lambda point: point.x)
                self.provider.set_feet((a.x, a.y), (b.x, b.y))
            elif tips:
                # One hand moves the foot nearest to it; the other stays planted.
                tip = tips[0]
                side = min(('left', 'right'), key=lambda name: abs(self.provider.foot(name)[0]-tip.x))
                self.provider.place_foot(side, tip.x, tip.y)
        elif observation.landmarks.get('face'):
            nose = observation.landmarks['face'][NOSE_TIP]
            (lx, ly), (rx, ry) = self.provider.foot('left'), self.provider.foot('right')
            self.provider.move('body', nose.x-(lx+rx)/2, nose.y-(ly+ry)/2)

    def change_camera(self):
        if self.camera:
            self.set_source(CAMERA)

    def change_control(self):
        self.show_camera_tuning()
        self.change_camera()

    def show_camera_tuning(self):
        """Camera rows appear only for the camera source; tuning only for controls that drive the body."""
        camera = self.source.currentIndex() == CAMERA
        control = self.control.currentData()
        tuned = camera and control in self.sensitivity
        self.form.setRowVisible(self.camera_device, camera)
        self.form.setRowVisible(self.control, camera)
        self.form.setRowVisible(self.sensitivity_box, tuned)
        self.form.setRowVisible(self.region, tuned)
        if control in self.sensitivity:
            self.sensitivity_box.blockSignals(True)
            self.sensitivity_box.setValue(self.sensitivity[control])
            self.sensitivity_box.blockSignals(False)

    def camera_region(self):
        """Dashed region as (left, top, right, bottom) frame fractions around the control's centre, kept in frame."""
        width = min(self.region_width.value()/self.frame_size[0], 1.)
        height = min(self.region_height.value()/self.frame_size[1], 1.)
        cx, cy = REGION_CENTRE[self.control.currentData()]
        cx, cy = min(max(cx, width/2), 1-width/2), min(max(cy, height/2), 1-height/2)
        return cx-width/2, cy-height/2, cx+width/2, cy+height/2

    def suggest_region(self):
        """Game resolution as the region size, scaled down to fit the camera frame."""
        width, height = (self.manifest or {}).get('resolution', DEFAULT_RESOLUTION)
        scale = min(1., self.frame_size[0]/width, self.frame_size[1]/height)
        self.region_width.setValue(round(width*scale))
        self.region_height.setValue(round(height*scale))

    def set_sensitivity(self, value):
        self.sensitivity[self.control.currentData()] = value

    def set_source(self, index):
        if self.camera:
            self.camera.close()
            self.camera = None
        self.held.clear()
        if index == CAMERA:
            try:
                camera_index = self.camera_device.currentData()
                if camera_index is None:
                    raise RuntimeError('No camera found')
                self.camera = CameraSource(CONTROLS[self.control.currentData()][1], camera_index)
            except Exception as exc:
                QMessageBox.warning(self, 'Cannot open camera', str(exc))
                self.source.blockSignals(True)
                self.source.setCurrentIndex(MOUSE)
                self.source.blockSignals(False)
        self.show_source()
        self.canvas.update()

    def show_source(self):
        source = self.source.currentIndex()
        control = self.control.currentData()
        if source == CAMERA and control == 'palms':
            hint = 'Each palm moves one foot. One hand moves the nearer foot.'
        elif source == CAMERA:
            hint = 'Your nose moves both feet.'
        elif source == MOUSE:
            hint = 'Drag the skeleton in the preview to move both feet.'
        else:
            hint = 'W A S D move the left foot. Arrow keys move the right foot.'
        self.show_camera_tuning()
        self.hint.setText(hint)
        self.show_state()

    def move_body(self, x, y):
        """Move the whole body so the midpoint between its feet sits at a normalized game position."""
        if not self.developer.isChecked():
            return
        (lx, ly), (rx, ry) = self.provider.foot('left'), self.provider.foot('right')
        self.provider.move('body', x-(lx+rx)/2, y-(ly+ry)/2)
        self.canvas.update()

    def foot_key(self, event, down):
        """In keyboard mode W/A/S/D and the arrow keys move the feet; the game never sees them."""
        if self.source.currentIndex() != KEYBOARD or event.key() not in FOOT_KEYS:
            return False
        if not event.isAutoRepeat():
            (self.held.add if down else self.held.discard)(event.key())
        return True

    def step_feet(self, elapsed):
        """Move each foot whose keys are held at FOOT_SPEED; diagonals combine two keys."""
        moves = {'left': [0, 0], 'right': [0, 0]}
        for key in self.held:
            side, dx, dy = FOOT_KEYS[key]
            moves[side][0] += dx
            moves[side][1] += dy
        for side, (dx, dy) in moves.items():
            if dx or dy:
                x, y = self.provider.foot(side)
                self.provider.place_foot(side, min(max(x+dx*FOOT_SPEED*elapsed, 0.), 1.),
                                         min(max(y+dy*FOOT_SPEED*elapsed, 0.), 1.))
        self.canvas.update()

    def resolution(self):
        """The game's own resolution from its manifest; its aspect ratio is kept wherever it is shown."""
        return tuple((self.manifest or {}).get('resolution', DEFAULT_RESOLUTION))

    def show_size(self):
        width, height = self.resolution()
        divisor = math.gcd(width, height)
        shown = self.game_view.target()
        self.size_label.setText(f'{width}×{height} ({width//divisor}:{height//divisor}) · '
                                f'shown {round(shown.width())}×{round(shown.height())}')

    def show_state(self):
        if self.manifest is not None:
            self.setWindowTitle(f"TSDK developer — {self.manifest['id']}")
        running = self.process is not None
        self.restart_button.setVisible(running)
        self.pause_button.setVisible(running)
        self.stop_button.setVisible(running)
        self.exit_button.setVisible(not running)
        self.canvas.update()

    def set_present(self, value):
        self.provider.observation = replace(self.provider.observation,present=value)
        self.canvas.update()

    def load_folder(self):
        directory = QFileDialog.getExistingDirectory(self, 'Load game project', str(self.project or Path.cwd()))
        if directory:
            self.load_project(Path(directory))

    def fill_load_menu(self):
        """Cartridges in the tsdk checkout's games/ folder, then the file pickers."""
        menu = self.load_menu
        menu.clear()
        cartridges = sorted(GAMES.glob('*.ttea'))
        for cartridge in cartridges:
            menu.addAction(cartridge.stem, lambda path=cartridge: self.load_cartridge(path))
        if cartridges:
            menu.addSeparator()
        menu.addAction('Project folder…', self.load_folder)
        menu.addAction('Cartridge (.ttea)…', lambda: self.load_cartridge())

    def load_cartridge(self, filename=None):
        if filename is None:
            filename,_ = QFileDialog.getOpenFileName(self, 'Load game cartridge', '', 'TSDK cartridges (*.ttea)')
        if not filename:
            return
        self.cartridges += 1
        destination = self.workdir/'cartridges'/str(self.cartridges)
        destination.parent.mkdir(exist_ok=True)
        try:
            extract_cartridge(filename, destination)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Cannot load cartridge', str(exc))
            return
        self.load_project(destination)

    def load_project(self, project):
        try:
            manifest = load_manifest(project.resolve())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Cannot load game', f'{project}: {exc}')
            return
        self.project, self.manifest = project.resolve(), manifest
        self.suggest_region()
        self.show_size()
        self.game_view.update()
        self.canvas.update()
        self.start_game()

    def game_size(self):
        """Runtime render size: the game's aspect, as large as fits two thirds of the screen."""
        geometry = self.screen().geometry()
        width, height = self.resolution()
        scale = min(geometry.width()*2/3/width, geometry.height()/height)
        return round(width*scale), round(height*scale)

    def start_game(self):
        self.stop_game()
        self.paused = False
        self.pause_button.setText('Pause game')
        self.launches += 1
        frame_buffer = self.workdir/f'frame-{self.launches}.bin'
        stop_file = self.workdir/'game.stop'
        stop_file.unlink(missing_ok=True)
        width, height = self.game_size()
        config = {'session_root': str(self.workdir/'sessions'), 'synthetic': True, 'stop_file': str(stop_file),
                  'frame_buffer': str(frame_buffer), 'input': 'stdin', 'width': width, 'height': height,
                  'show_feet': self.show_feet.isChecked()}
        command = [sys.executable, '-m', 'ttea.sdk.runtime', str(self.project)]
        write_state(self.state_path, self.tracker())
        config['simulation_state'] = str(self.state_path)
        command.append('--simulation')
        command.extend(['--config', json.dumps(config)])
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, text=True, bufsize=1)
        self.frames = FrameReader(frame_buffer)
        self.game_view.clear('Starting…')
        self.game_view.setFocus()
        self.show_state()

    def restart_game(self):
        if self.manifest is not None:
            self.start_game()

    def toggle_feet(self, checked):
        """Live: the runtime applies config messages without restarting the game."""
        self.send({'type': 'config', 'values': {'show_feet': checked}})

    def showEvent(self, event):
        super().showEvent(event)
        if not self.sized:
            self.sized = True
            QTimer.singleShot(0, self.initial_sizes)

    def initial_sizes(self):
        """Game column two thirds of the width; controls 40% and preview 60% of the side column."""
        width = self.root_splitter.width()
        self.root_splitter.setSizes([width*2//3, width-width*2//3])
        height = self.tools_splitter.height()
        self.tools_splitter.setSizes([height*2//5, height-height*2//5])

    def stop_game(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.workdir.joinpath('game.stop').touch()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self.release_game('Game stopped')

    def release_game(self, message):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        self.process = None
        self.frames.close()
        self.frames = None
        self.game_view.clear(message)
        self.show_state()

    def send(self, message):
        if self.process is not None:
            try:
                self.process.stdin.write(json.dumps(message) + '\n')
            except OSError:
                pass

    def forward_key(self, event, kind):
        name = key_name(event)
        if event.isAutoRepeat() or name is None:
            return
        mods = [label for modifier, label in MODIFIERS.items() if event.modifiers() & modifier]
        self.send({'type': kind, 'key': name, 'text': event.text(), 'mods': mods})
        if name == 'p' and kind == 'key_down':  # the runtime toggles its pause on P
            self.paused = not self.paused
            self.pause_button.setText('Resume game' if self.paused else 'Pause game')

    def toggle_pause(self):
        """Press P in the game: the runtime toggles its user pause."""
        if self.process is None:
            return
        for kind in ('key_down', 'key_up'):
            self.send({'type': kind, 'key': 'p', 'text': 'p', 'mods': []})
        self.paused = not self.paused
        self.pause_button.setText('Resume game' if self.paused else 'Pause game')
        self.game_view.setFocus()

    def tick(self):
        now = time.monotonic()
        elapsed, self.ticked = now-self.ticked, now
        if self.process is not None and self.process.poll() is not None:
            self.release_game(f'Game exited with status {self.process.returncode}')
        if self.held and self.source.currentIndex() == KEYBOARD and self.developer.isChecked():
            self.step_feet(elapsed)
        if self.driving and self.developer.isChecked():
            observation = self.camera.read()
            if observation is not self.driven:
                self.drive(observation)
                self.driven = observation
        write_state(self.state_path, self.tracker())
        if self.process is None:
            self.game_view.update()  # feet follow the input even without a game
        if self.frames is not None:
            frame = self.frames.read()
            if frame is not None:
                self.game_view.show_frame(*frame)
        if self.camera:
            frame = self.camera.frame
            if frame is not None and (frame.shape[1], frame.shape[0]) != self.frame_size:
                self.frame_size = frame.shape[1], frame.shape[0]
                self.region_width.setMaximum(self.frame_size[0])
                self.region_height.setMaximum(self.frame_size[1])
                self.suggest_region()
            if self.camera.error is not None:
                error = self.camera.error
                self.source.setCurrentIndex(MOUSE)
                QMessageBox.warning(self, 'Cannot open camera', str(error))
                return
            self.canvas.update()

    def shutdown(self):
        self.timer.stop()
        if self.camera:
            self.camera.close()
            self.camera = None
        self.stop_game()

    def closeEvent(self, event):
        self.shutdown()
        event.accept()


def run(project: Path) -> int:
    app = QApplication.instance() or QApplication([sys.argv[0]])
    with tempfile.TemporaryDirectory(prefix='tsdk-developer-') as directory:
        panel = DeveloperPanel(Path(directory), project)
        try:
            panel.showFullScreen()
            return app.exec()
        finally:
            panel.shutdown()
