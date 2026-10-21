"""Platform overlays drawn over every game frame, so all games look the same."""

TEXTS = {"en": ("Paused", "Press F2 to resume"), "pt": ("Pausado", "Pressione F2 para continuar")}


def draw_pause(surface, language: str = "en") -> None:
    """Dim the frame and show the pause title with how to resume."""
    import pygame

    title, hint = TEXTS["pt" if language.lower().startswith("pt") else "en"]
    width, height = surface.get_size()
    shade = pygame.Surface((width, height), pygame.SRCALPHA)
    shade.fill((0, 0, 0, 170))
    surface.blit(shade, (0, 0))
    for text, size, y in ((title, height // 8, height * 0.44), (hint, height // 20, height * 0.58)):
        label = pygame.font.Font(None, max(12, size)).render(text, True, (255, 255, 255))
        surface.blit(label, label.get_rect(center=(width // 2, int(y))))
