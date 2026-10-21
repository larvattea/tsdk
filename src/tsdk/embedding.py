"""Run a game inside the developer window: frames out through a shared file, input in through stdin.

The runtime renders with SDL's dummy video driver, so games must read input from
``context.events``; ``pygame.mouse.get_pos`` and ``pygame.key.get_pressed`` stay idle.
"""
from __future__ import annotations

import json
import mmap
import os
from pathlib import Path
import queue
import struct
import threading

# sequence (odd while a frame is being written), width, height; RGBX pixels follow.
HEADER = struct.Struct('<QII')
SEQUENCE = struct.Struct('<Q')
MODIFIERS = {'shift': 'KMOD_LSHIFT', 'ctrl': 'KMOD_LCTRL', 'alt': 'KMOD_LALT', 'meta': 'KMOD_LMETA'}


class FrameWriter:
    def __init__(self, path: Path, size: tuple[int, int]):
        width, height = size
        self._file = open(path, 'w+b')
        self._file.truncate(HEADER.size + width * height * 4)
        self._map = mmap.mmap(self._file.fileno(), HEADER.size + width * height * 4)
        self._sequence = 0
        HEADER.pack_into(self._map, 0, 0, width, height)

    def write(self, pixels: bytes):
        self._sequence += 1
        SEQUENCE.pack_into(self._map, 0, self._sequence)
        self._map[HEADER.size:] = pixels
        self._sequence += 1
        SEQUENCE.pack_into(self._map, 0, self._sequence)

    def close(self):
        self._map.close()
        self._file.close()


class FrameReader:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._map = None
        self._sequence = 0

    def read(self):
        """(width, height, RGBX bytes) of a frame completed since the last call, else None."""
        if self._map is None:
            try:
                file = open(self.path, 'rb')
            except FileNotFoundError:
                return None
            with file:
                size = os.fstat(file.fileno()).st_size
                if size < HEADER.size:
                    return None
                self._map = mmap.mmap(file.fileno(), size, access=mmap.ACCESS_READ)
        sequence, width, height = HEADER.unpack_from(self._map)
        if sequence == self._sequence or sequence % 2 or HEADER.size + width * height * 4 != len(self._map):
            return None
        pixels = self._map[HEADER.size:]
        if SEQUENCE.unpack_from(self._map)[0] != sequence:
            return None
        self._sequence = sequence
        return width, height, pixels

    def close(self):
        if self._map is not None:
            self._map.close()
            self._map = None


class InputReader:
    """Collect JSON-line input messages from a stream on a worker thread."""

    def __init__(self, stream):
        self.closed = False
        self.config = {}  # live config updates sent as {"type": "config", "values": {...}}
        self._queue = queue.SimpleQueue()
        threading.Thread(target=self._run, args=(stream,), name='tsdk-input', daemon=True).start()

    def _run(self, stream):
        for line in stream:
            if line.strip():
                self._queue.put(json.loads(line))
        self._queue.put(None)

    def post(self, pygame):
        """Post queued messages as pygame events; returns False once the stream has ended."""
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                return not self.closed
            if message is None:
                self.closed = True
                continue
            if message['type'] == 'config':
                self.config.update(message['values'])
                continue
            event = to_event(pygame, message)
            if event is not None:
                pygame.event.post(event)


def to_event(pygame, message: dict):
    kind = message['type']
    if kind == 'mouse_motion':
        return pygame.event.Event(pygame.MOUSEMOTION, pos=tuple(message['pos']), rel=tuple(message['rel']),
                                  buttons=tuple(message['buttons']), touch=False)
    if kind in ('mouse_down', 'mouse_up'):
        return pygame.event.Event(pygame.MOUSEBUTTONDOWN if kind == 'mouse_down' else pygame.MOUSEBUTTONUP,
                                  pos=tuple(message['pos']), button=message['button'], touch=False)
    if kind in ('key_down', 'key_up'):
        try:
            key = pygame.key.key_code(message['key'])
        except ValueError:
            return None
        mod = 0
        for name in message.get('mods', ()):
            mod |= getattr(pygame, MODIFIERS[name])
        return pygame.event.Event(pygame.KEYDOWN if kind == 'key_down' else pygame.KEYUP, key=key, mod=mod,
                                  unicode=message.get('text', ''), scancode=0)
    raise ValueError(f'Unknown input message: {kind}')
