"""Platform-drawn footprints showing where the player's feet are on the floor."""

from functools import cache

import pygame


PROJECTION_WIDTH_M = 2.58  # 800x600 over ~5 m^2 (Trindade, Pereira, Hounsell, SBGames 2022)
FOOT_LENGTH_M = 0.26       # average adult foot length
MIDPOINT_COLOR = (250, 204, 21)
SKIN_COLOR = (234, 182, 140)
OUTLINE_COLOR = (120, 72, 44)
SUPERSAMPLE = 4


def foot_length(surface_width: int) -> int:
    return max(1, round(surface_width * FOOT_LENGTH_M / PROJECTION_WIDTH_M))


@cache
def foot_image(side: str, length: int) -> pygame.Surface:
    """Top-down footprint, toes up, big toe on the inner side."""
    if side not in ("left", "right"):
        raise ValueError(f"Unknown foot side: {side}")
    width = max(1, round(length * 0.38))
    s = SUPERSAMPLE
    w, h = width * s, length * s
    big = pygame.Surface((w, h), pygame.SRCALPHA)
    border = max(1, round(w * 0.06))
    # Drawn as a right foot (big toe on the left); mirrored for the left foot.
    sole = [
        (w * 0.10, h * 0.30), (w * 0.50, h * 0.24), (w * 0.94, h * 0.34),
        (w * 0.90, h * 0.55), (w * 0.78, h * 0.80), (w * 0.70, h * 0.97),
        (w * 0.45, h * 0.995), (w * 0.24, h * 0.95), (w * 0.20, h * 0.75),
        (w * 0.10, h * 0.50),
    ]
    toes = [  # (centre x, centre y, radius) as fractions; big toe first
        (0.24, 0.17, 0.20), (0.50, 0.12, 0.11), (0.66, 0.15, 0.10),
        (0.80, 0.20, 0.09), (0.90, 0.27, 0.075),
    ]
    for color, grow in ((OUTLINE_COLOR, border), (SKIN_COLOR, 0)):
        if grow:
            pygame.draw.polygon(big, color, sole, 0)
            pygame.draw.lines(big, color, True, sole, border * 2)
        else:
            inset = [(x + (w * 0.5 - x) * 0.08, y + (h * 0.6 - y) * 0.04) for x, y in sole]
            pygame.draw.polygon(big, color, inset)
        for cx, cy, r in toes:
            pygame.draw.circle(big, color, (cx * w, cy * h), r * w + (border if grow else 0))
    if side == "left":
        big = pygame.transform.flip(big, True, False)
    return pygame.transform.smoothscale(big, (width, length))


def draw_feet(surface, feet: dict[str, tuple[float, float]]) -> None:
    length = foot_length(surface.get_width())
    sw, sh = surface.get_size()
    points = {}
    for side in ("left", "right"):
        if side not in feet:
            continue
        x, y = feet[side][0] * sw, feet[side][1] * sh
        points[side] = (x, y)
        surface.blit(foot_image(side, length), foot_image(side, length).get_rect(center=(round(x), round(y))))
    if len(points) == 2:
        (lx, ly), (rx, ry) = points["left"], points["right"]
        pygame.draw.circle(surface, MIDPOINT_COLOR, ((lx + rx) / 2, (ly + ry) / 2), max(1, length / 8))
