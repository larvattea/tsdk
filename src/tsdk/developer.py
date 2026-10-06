"""Full-screen developer window: the game on the left two thirds, tracking tools on the right third."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
                               QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget)

from ttea.sdk.cartridge import extract_cartridge
from ttea.sdk.embedding import FrameReader
from ttea.sdk.runtime import DEFAULT_RESOLUTION, load_manifest
from ttea.sdk.simulator import EXPRESSIONS, write_state
from ttea.sdk.tracking import Landmark, Observation, Recording, ReplayProvider

from .simulator import (BODY_EDGES, BODY_NAMES, HAND_EDGES, HAND_NAMES, PALM, DeveloperSimulator, palm_centre,
                        pointing_down_fingertip)

LOW_VISIBILITY = .5
PROFILES = {'body': 'Body', 'face': 'Face', 'hands': 'Hands'}
NOSE_TIP = 1  # MediaPipe face mesh vertex
# Camera controls that can drive a body game: label, MediaPipe model, and the landmarks
# drawn enlarged in the preview because they move the feet.
CONTROLS = {'body': ('Body', 'body', ()), 'fingers': ('Hands (index fingers down)', 'hands', (8,)),
            'palms': ('Whole hands', 'hands', PALM), 'face': ('Face (nose)', 'face', (NOSE_TIP,))}
# Centre of the camera region that spans the whole game at sensitivity 1; its size is the
# editable Camera region (camera pixels), suggested from the game resolution. Fingers pointing
# down hang below the fists, so their region sits low; a palm centre has fingers above, wrist below.
REGION_CENTRE = {'fingers': (.5, .675), 'palms': (.5, .55), 'face': (.5, .5)}
# Default gain around the region centre per control; a nose moves far less than a hand.
SENSITIVITY = {'fingers': 1., 'palms': 1., 'face': 2.5}
MOUSE, KEYBOARD, CAMERA = range(3)
# Keyboard source: W/A/S/D move the left foot, arrow keys the right one, by (side, dx, dy).
FOOT_KEYS = {Qt.Key.Key_W: ('left', 0, -1), Qt.Key.Key_A: ('left', -1, 0), Qt.Key.Key_S: ('left', 0, 1),
             Qt.Key.Key_D: ('left', 1, 0), Qt.Key.Key_Up: ('right', 0, -1), Qt.Key.Key_Left: ('right', -1, 0),
             Qt.Key.Key_Down: ('right', 0, 1), Qt.Key.Key_Right: ('right', 1, 0)}
FOOT_SPEED = .6  # game widths per second while a foot key is held
PRESETS = {'body': ['standing', 'arms_up', 'squat'], 'hands': ['open', 'fist', 'pinch'],
           'face': ['neutral', 'smile', 'surprise', 'blink']}
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
        landmarks = {group: [Landmark(p.x+x-cx, p.y+y-cy, p.z, p.visibility) for p in points]
                     for group, points in observation.landmarks.items()}
        return replace(observation, landmarks=landmarks)


class CameraSource:
    """Live camera detection on a worker thread; read() returns the latest observation without blocking.

    Frames are only previewed in the panel; recordings still store observations, never video.
    """
    stale = False

    def __init__(self, profile, camera_index):
        from ttea.sdk.providers import HardwareProvider
        self.profile = profile
        self.frame = None
        self.error = None
        self._provider = HardwareProvider(profile, camera_index=camera_index)
        self._observation = Observation(profile, time.monotonic(), present=False, confidence_source='measured')
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._run, name='tsdk-camera', daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while self._running:
                observation = self._provider.read()
                with self._lock:
                    self._observation, self.frame = observation, self._provider.frame
        except Exception as exc:
            self.error = exc

    def read(self):
        with self._lock:
            return self._observation

    def close(self):
        self._running = False
        self._thread.join()
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
        size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        rect = QRectF(0, 0, size.width(), size.height())
        rect.moveCenter(QRectF(self.rect()).center())
        return rect

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#000000'))
        if self.image is None:
            painter.setPen(QColor('#94a3b8'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(self.target(), self.image)

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


class LandmarkCanvas(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.dragging = False
        self.setMinimumSize(200, 200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def area(self):
        """Camera-normalized unit square on screen; with a camera frame, its aspect-fitted image."""
        rect = QRectF(16, 16, self.width()-32, self.height()-32)
        frame = self.panel.camera.frame if self.panel.camera else None
        if frame is None:
            side = min(rect.width(), rect.height())
            square = QRectF(0, 0, side, side)
            square.moveCenter(rect.center())
            return square
        height, width = frame.shape[:2]
        scale = min(rect.width()/width, rect.height()/height)
        fitted = QRectF(0, 0, width*scale, height*scale)
        fitted.moveCenter(rect.center())
        return fitted

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
        detected = camera is not None and not driving
        for observation, driver in layers:
            for group,points in observation.landmarks.items():
                base = QColor('#c4b5fd' if driver else ('#5eead4' if group != 'right' else '#fbbf24'))
                faded = (group in self.panel.provider.hidden and not driver and not detected) or not observation.present
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
                    selected = not driver and group == self.panel.group.currentText() and index == self.panel.joint.currentIndex()
                    painter.setBrush(QColor('#f87171') if selected else color(index))
                    control = driver and index in CONTROLS[self.panel.control.currentData()][2]
                    radius = 5 if selected or control else (2 if group == 'face' else 4)
                    painter.drawEllipse(self.point(point),radius,radius)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        group = self.panel.group.currentText()
        points = self.panel.observation().landmarks.get(group)
        if not points:
            return
        index = min(range(len(points)), key=lambda i:(self.point(points[i])-event.position()).manhattanLength())
        if (self.point(points[index])-event.position()).manhattanLength() < 25:
            self.panel.joint.setCurrentIndex(index)
            if self.panel.source.currentIndex() == KEYBOARD:
                self.dragging = True
                self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event):
        area = self.area()
        x, y = (event.position().x()-area.x())/area.width(), (event.position().y()-area.y())/area.height()
        if self.dragging:
            self.panel.coordinates[0].setValue(x)
            self.panel.coordinates[1].setValue(y)
        elif self.panel.source.currentIndex() == MOUSE:
            self.panel.move_body(x, y)

    def mouseReleaseEvent(self, event):
        self.dragging = False

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
        self.replay = None
        self.profile = 'body'
        self.providers = {}
        self.camera = None
        self.driven = None
        self.sensitivity = dict(SENSITIVITY)
        self.game_space = GameSpace(self)
        self.held = set()
        self.ticked = time.monotonic()
        self.recorder = None
        self.recorded = None
        self.loading = False
        self.synthetic_controls = []
        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.game_view = GameView(self)
        layout.addWidget(self.game_view, 2)
        side = QWidget()
        side.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(side, 1)
        column = QVBoxLayout(side)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        column.addWidget(scroll, 1)
        content = QWidget()
        scroll.setWidget(content)
        controls = QVBoxLayout(content)
        self.title = QLabel()
        self.title.setWordWrap(True)
        controls.addWidget(self.title)
        self.info = QLabel()
        self.info.setWordWrap(True)
        controls.addWidget(self.info)
        self.form = form = QFormLayout()
        controls.addLayout(form)
        self.source = QComboBox()
        self.source.addItems(['Mouse', 'Keyboard', 'Camera'])
        self.source.setCurrentIndex(MOUSE)
        self.source.setToolTip('Mouse: the mouse over the skeleton preview moves the whole body. Keyboard: W/A/S/D move the '
                               'left foot, arrow keys the right foot. Camera: live detection. '
                               'Nothing is recorded unless you record observations.')
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
        self.control.setToolTip('What the camera watches to move the body skeleton: your body, both index fingers '
                                'pointing down or both whole hands (one per foot), or your nose (the centre of both feet).')
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
        self.group = QComboBox()
        form.addRow('Subject', self.group)
        self.joint = QComboBox()
        form.addRow('Joint / mesh vertex',self.joint)
        self.coordinates = []
        for label in ('X (camera normalized)','Y (camera normalized)','Z (image-relative depth)','Visibility'):
            spin = QDoubleSpinBox()
            spin.setRange(-1.,1.) if label == 'Visibility' else spin.setRange(-100.,100.)
            if label == 'Visibility':
                spin.setSpecialValueText('Unknown')
            spin.setDecimals(4)
            spin.setSingleStep(.01)
            spin.valueChanged.connect(self.edit_joint)
            self.coordinates.append(spin)
            self.synthetic_controls.append(spin)
            form.addRow(label,spin)
        self.preset = QComboBox()
        form.addRow('Preset',self.preset)
        self.expression = QComboBox()
        self.expression.addItems(EXPRESSIONS)
        form.addRow('Expression coefficient',self.expression)
        self.expression_value = QSlider(Qt.Orientation.Horizontal)
        self.expression_value.setRange(0,100)
        form.addRow('Expression 0–1',self.expression_value)
        self.expression.currentTextChanged.connect(self.load_expression)
        self.expression_value.valueChanged.connect(self.set_expression)
        button = QPushButton('Apply preset to selected subject')
        button.clicked.connect(self.apply_preset)
        controls.addWidget(button)
        self.synthetic_controls += [self.preset, button, self.expression, self.expression_value]
        for axis,label in enumerate(('Move X','Move Y','Move Z')):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            for sign in (-1,1):
                button = QPushButton('−' if sign < 0 else '+')
                button.clicked.connect(lambda checked=False,a=axis,s=sign:self.move_subject(a,s*.025))
                row.addWidget(button)
                self.synthetic_controls.append(button)
            controls.addLayout(row)
        for axis,label in enumerate(('Pitch','Yaw','Roll')):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            for sign in (-1,1):
                button = QPushButton('−5°' if sign < 0 else '+5°')
                button.clicked.connect(lambda checked=False,a=axis,s=sign:self.rotate_subject(a,s*5))
                row.addWidget(button)
                self.synthetic_controls.append(button)
            controls.addLayout(row)
        self.present = QCheckBox('Participant present')
        self.present.setChecked(True)
        self.present.toggled.connect(self.set_present)
        controls.addWidget(self.present)
        self.subject_present = QCheckBox('Selected subject visible')
        self.subject_present.setChecked(True)
        self.subject_present.toggled.connect(self.set_subject_present)
        controls.addWidget(self.subject_present)
        self.stale = QCheckBox('Freeze timestamp (stale input)')
        self.stale.toggled.connect(self.set_stale)
        controls.addWidget(self.stale)
        self.synthetic_controls += [self.present, self.subject_present, self.stale]
        self.record_button = QPushButton('Record observations…')
        self.record_button.clicked.connect(self.toggle_record)
        controls.addWidget(self.record_button)
        button = QPushButton('Replay recording…')
        button.clicked.connect(self.start_replay)
        controls.addWidget(button)
        button = QPushButton('Return to live simulation')
        button.clicked.connect(self.stop_replay)
        controls.addWidget(button)
        controls.addStretch()
        self.canvas = LandmarkCanvas(self)
        column.addWidget(self.canvas, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        column.addWidget(self.status)
        buttons = QHBoxLayout()
        column.addLayout(buttons)
        load = QPushButton('Load game')
        menu = QMenu(load)
        menu.addAction('Project folder…', self.load_folder)
        menu.addAction('Cartridge (.ttea)…', self.load_cartridge)
        load.setMenu(menu)
        buttons.addWidget(load)
        self.restart_button = QPushButton('Restart game')
        self.restart_button.clicked.connect(self.restart_game)
        buttons.addWidget(self.restart_button)
        self.stop_button = QPushButton('Stop game')
        self.stop_button.clicked.connect(self.stop_game)
        buttons.addWidget(self.stop_button)
        self.exit_button = QPushButton('Exit')
        self.exit_button.clicked.connect(self.close)
        buttons.addWidget(self.exit_button)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Quit), self, self.close)
        self.group.currentTextChanged.connect(self.load_group)
        self.joint.currentIndexChanged.connect(self.load_joint)
        self.source.currentIndexChanged.connect(self.set_source)
        self.camera_device.currentIndexChanged.connect(self.change_camera)
        self.control.currentIndexChanged.connect(self.change_control)
        self.set_profile('body')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)
        self.game_view.clear('No game loaded · use Load game')

    @property
    def provider(self):
        if self.profile not in self.providers:
            self.providers[self.profile] = DeveloperSimulator(self.profile)
        return self.providers[self.profile]

    def observation(self):
        """What the game receives."""
        return self.camera.read() if self.camera and not self.driving else self.provider.observation

    @property
    def driving(self):
        """The camera detects hands or a face that drive the body skeleton."""
        return self.camera is not None and self.camera.profile != self.profile

    def tracker(self):
        """What write_state sends to the game."""
        if self.driving:
            return self.game_space
        return self.camera if self.camera else self.provider

    def set_profile(self, profile):
        self.profile = profile
        self.show_camera_tuning()
        for widget in (self.expression, self.expression_value):
            self.form.setRowVisible(widget, self.profile == 'face')
        self.preset.clear()
        self.preset.addItems(PRESETS[self.profile])
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItems(list(self.provider.observation.landmarks))
        self.group.blockSignals(False)
        self.present.blockSignals(True)
        self.present.setChecked(self.provider.observation.present)
        self.present.blockSignals(False)
        self.stale.blockSignals(True)
        self.stale.setChecked(self.provider.stale)
        self.stale.blockSignals(False)
        if self.profile == 'face':
            self.load_expression()
        if self.camera:
            self.set_source(CAMERA)
        self.load_group()
        self.show_source()

    def drive(self, observation):
        """Move the body skeleton from both hands or the nose; other observations hold the pose."""
        observation = mirrored(observation)
        control = self.control.currentData()
        if control in ('fingers', 'palms'):
            locate = pointing_down_fingertip if control == 'fingers' else palm_centre
            tips = [tip for tip in (locate(observation.landmarks.get(side)) for side in ('left', 'right'))
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

    def load_group(self):
        self.joint.blockSignals(True)
        self.joint.clear()
        group = self.group.currentText()
        names = BODY_NAMES if group == 'body' else (HAND_NAMES if group != 'face' else [f'vertex {i}' for i in range(478)])
        self.joint.addItems([f'{i}: {name}' for i,name in enumerate(names)])
        self.joint.blockSignals(False)
        self.subject_present.blockSignals(True)
        self.subject_present.setChecked(group not in self.provider.hidden)
        self.subject_present.blockSignals(False)
        self.load_joint()

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
        tuned = camera and self.profile == 'body' and control in self.sensitivity
        self.form.setRowVisible(self.camera_device, camera)
        self.form.setRowVisible(self.control, camera and self.profile == 'body')
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
                profile = CONTROLS[self.control.currentData()][1] if self.profile == 'body' else self.profile
                self.camera = CameraSource(profile, camera_index)
            except Exception as exc:
                QMessageBox.warning(self, 'Cannot open camera', str(exc))
                self.source.blockSignals(True)
                self.source.setCurrentIndex(MOUSE)
                self.source.blockSignals(False)
        self.show_source()
        self.load_joint()

    def show_source(self):
        source = self.source.currentIndex()
        control = self.control.currentData()
        if source == CAMERA and self.driving and control == 'fingers':
            text = tip = ('Camera · point both index fingers down, other fingers folded: each fingertip places a foot. '
                          'One finger moves the nearer foot; any other hand shape holds the pose.')
        elif source == CAMERA and self.driving and control == 'palms':
            text = tip = ('Camera · each hand places a foot at its palm centre, in any shape. '
                          'With one hand visible it moves the nearer foot.')
        elif source == CAMERA and self.driving:
            text = tip = 'Camera · your nose moves the centre of both feet.'
        elif source == CAMERA:
            text = tip = 'Live camera detection · click a joint to inspect it.'
        elif source == MOUSE:
            text = tip = ('Mouse · the mouse over the skeleton preview moves the whole body; '
                          'the feet keep their spacing.')
        else:
            text = tip = ('Keyboard · W/A/S/D move the left foot, arrow keys the right foot, while the game or the '
                          'preview has focus; feet stay between hip width and one stride apart.')
        self.info.setText(text)
        self.show_camera_tuning()
        self.canvas.setToolTip(tip)
        for control in self.synthetic_controls:
            control.setEnabled(source == KEYBOARD)
        self.show_state()

    def move_body(self, x, y):
        """Move the whole body so the midpoint between its feet sits at a normalized game position."""
        (lx, ly), (rx, ry) = self.provider.foot('left'), self.provider.foot('right')
        self.provider.move('body', x-(lx+rx)/2, y-(ly+ry)/2)
        self.load_joint()

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
        self.load_joint()

    def show_state(self):
        if self.manifest is None:
            self.title.setText('No game loaded')
        else:
            expected = self.manifest['tracking_profile']
            self.setWindowTitle(f"TSDK developer — {self.manifest['id']}")
            self.title.setText(f"<b>{self.manifest['id']}</b> {self.manifest['version']} · uses {PROFILES[expected].lower()} tracking")
        running = self.process is not None
        self.restart_button.setVisible(running)
        self.stop_button.setVisible(running)
        self.exit_button.setVisible(not running)
        if not running:
            self.status.setText('Game not running')
        elif self.replay is not None:
            self.status.setText(f'Replaying {self.replay.name} with its recorded calibration and timing.')
        else:
            mode = {MOUSE: 'Mouse', KEYBOARD: 'Keyboard', CAMERA: 'Live camera'}[self.source.currentIndex()]
            self.status.setText(f"{mode} tracking · Escape ends the game, P pauses it · Ctrl/⌘+Q quits")
        self.canvas.update()

    def load_joint(self):
        index = self.joint.currentIndex()
        points = self.observation().landmarks.get(self.group.currentText(), [])
        if not 0 <= index < len(points):
            self.canvas.update()
            return
        point = points[index]
        self.loading = True
        for spin,value in zip(self.coordinates,(point.x,point.y,point.z or 0.,point.visibility if point.visibility is not None else -1.)):
            spin.setValue(value)
        self.loading = False
        self.canvas.update()

    def edit_joint(self):
        if not self.loading:
            x,y,z,visibility = [spin.value() for spin in self.coordinates]
            self.provider.set_joint(self.group.currentText(),self.joint.currentIndex(),x,y,z,
                                    None if visibility < 0 else visibility)
            self.canvas.update()

    def apply_preset(self):
        self.provider.preset(self.group.currentText(),self.preset.currentText())
        self.load_joint()
        if self.profile == 'face':
            self.load_expression()

    def move_subject(self, axis, amount):
        delta = [0.,0.,0.]
        delta[axis] = amount
        self.provider.move(self.group.currentText(),*delta)
        self.load_joint()

    def rotate_subject(self, axis, amount):
        angles = [0.,0.,0.]
        angles[axis] = amount
        self.provider.rotate(self.group.currentText(),*angles)
        self.load_joint()

    def set_present(self, value):
        self.provider.observation = replace(self.provider.observation,present=value)
        self.canvas.update()

    def set_subject_present(self, value):
        if value:
            self.provider.hidden.discard(self.group.currentText())
        else:
            self.provider.hidden.add(self.group.currentText())
        self.canvas.update()

    def set_stale(self, value):
        self.provider.stale = value

    def load_expression(self):
        self.expression_value.blockSignals(True)
        self.expression_value.setValue(round(self.provider.observation.expressions.get(self.expression.currentText(),0.)*100))
        self.expression_value.blockSignals(False)

    def set_expression(self, value):
        self.provider.observation.expressions[self.expression.currentText()] = value/100

    def toggle_record(self):
        if self.recorder:
            self.recorder.close()
            self.recorder = None
            self.record_button.setText('Record observations…')
            return
        filename,_ = QFileDialog.getSaveFileName(self,'Record observations','','JSON Lines (*.jsonl)')
        if filename:
            self.recorder = Recording(Path(filename),self.profile,calibration=None,config={})
            self.record_button.setText('Stop recording')

    def start_replay(self):
        if self.manifest is None:
            return
        filename,_ = QFileDialog.getOpenFileName(self,'Replay observations','','JSON Lines (*.jsonl)')
        if not filename:
            return
        try:
            replay = ReplayProvider(Path(filename))
            try:
                if replay.profile != self.manifest['tracking_profile']:
                    raise ValueError('Recording profile does not match this game')
                replay.read()
            finally:
                replay.close()
        except (OSError, ValueError, StopIteration) as exc:
            QMessageBox.warning(self, 'Cannot replay', str(exc))
            return
        if self.recorder:
            self.toggle_record()
        self.start_game(Path(filename))

    def stop_replay(self):
        if self.manifest is not None and (self.replay is not None or self.process is None):
            self.start_game()

    def load_folder(self):
        directory = QFileDialog.getExistingDirectory(self, 'Load game project', str(self.project or Path.cwd()))
        if directory:
            self.load_project(Path(directory))

    def load_cartridge(self):
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
        self.set_profile(manifest['tracking_profile'])
        self.start_game()

    def game_size(self):
        geometry = self.screen().geometry()
        return geometry.width()*2//3, geometry.height()

    def start_game(self, replay=None):
        self.stop_game()
        self.launches += 1
        frame_buffer = self.workdir/f'frame-{self.launches}.bin'
        stop_file = self.workdir/'game.stop'
        stop_file.unlink(missing_ok=True)
        width, height = self.game_size()
        config = {'session_root': str(self.workdir/'sessions'), 'synthetic': True, 'stop_file': str(stop_file),
                  'frame_buffer': str(frame_buffer), 'input': 'stdin', 'width': width, 'height': height}
        command = [sys.executable, '-m', 'ttea.sdk.runtime', str(self.project)]
        if replay is None:
            write_state(self.state_path, self.tracker())
            config['simulation_state'] = str(self.state_path)
            command.append('--simulation')
        else:
            command.extend(['--replay', str(replay)])
        command.extend(['--config', json.dumps(config)])
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, text=True, bufsize=1)
        self.frames = FrameReader(frame_buffer)
        self.replay = replay
        self.record_button.setEnabled(replay is None)
        self.game_view.clear('Starting…')
        self.game_view.setFocus()
        self.show_state()

    def restart_game(self):
        self.start_game(self.replay)

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
        self.replay = None
        self.record_button.setEnabled(True)
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

    def tick(self):
        now = time.monotonic()
        elapsed, self.ticked = now-self.ticked, now
        if self.process is not None and self.process.poll() is not None:
            self.release_game(f'Game exited with status {self.process.returncode}')
        if self.held and self.source.currentIndex() == KEYBOARD:
            self.step_feet(elapsed)
        if self.driving:
            observation = self.camera.read()
            if observation is not self.driven:
                self.drive(observation)
                self.driven = observation
        if self.replay is None:
            observation = write_state(self.state_path, self.tracker())
            # The camera source repeats its latest observation until a new frame is detected.
            if self.recorder and observation is not self.recorded:
                self.recorder.write(observation)
                self.recorded = observation
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
                self.status.setText(f'Camera failed: {self.camera.error}')
            self.load_joint()

    def shutdown(self):
        self.timer.stop()
        if self.recorder:
            self.recorder.close()
            self.recorder = None
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
