"""Software design preview using live particle geometry, not an OpenGL screenshot.

Optional dependency: pip install Pillow
Run from any directory: python tools/render_core_preview.py
Glow is approximated in Pillow; exact driver rasterization can differ.
"""
from pathlib import Path
import math
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jarvis_ui.particle_core import ParticleCore


def render(destination):
    width, height, scale = 1200, 800, 2
    size = (width * scale, height * scale)
    image = Image.new('RGB', size, (2, 2, 1))
    light = Image.new('RGB', size)
    draw = ImageDraw.Draw(light)
    cx, cy, radius = ParticleCore.layout(width, height)
    frame = ParticleCore().frame(12, 'idle')

    def point(x, y):
        return ((cx + x * radius) * scale, (cy + y * radius) * scale)

    def amber(alpha):
        return tuple(int(min(255, value * alpha)) for value in (255, 143, 15))

    for orbit in frame.orbits:
        for a, b in zip(orbit, orbit[1:]):
            draw.line([point(*a[:2]), point(*b[:2])], fill=amber(a[2] * .95), width=scale)
    for x, y, alpha, diameter in frame.particles:
        px, py = point(x, y)
        r = diameter * scale / 2
        draw.ellipse((px - r, py - r, px + r, py + r), fill=amber(alpha))
    for x, y, alpha in frame.rays:
        draw.line([point(0, 0), point(x, y)], fill=amber(alpha * 1.8), width=scale)
    # Bloom approximation: same layout and geometry, software glow compositing.
    image = ImageChops.add(image, light.filter(ImageFilter.GaussianBlur(2 * scale)))
    image = ImageChops.add(image, light.filter(ImageFilter.GaussianBlur(.7 * scale)))
    image = ImageChops.add(image, light)
    center = Image.new('RGB', size)
    draw = ImageDraw.Draw(center)
    for r in range(int(radius * .28 * scale), 0, -1):
        norm = r / (radius * .28 * scale)
        a = math.exp(-norm * 7)
        draw.ellipse((cx * scale - r, cy * scale - r, cx * scale + r, cy * scale + r),
                     fill=(int(255 * a), int(220 * a), int(75 * a)))
    image = ImageChops.add(image, center)
    draw = ImageDraw.Draw(image)
    r = radius * .035 * scale
    draw.ellipse((cx * scale-r, cy * scale-r, cx * scale+r, cy * scale+r), fill=(255, 246, 175))

    font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    def font(size):
        if font_path.exists():
            return ImageFont.truetype(str(font_path), size * scale)
        return ImageFont.load_default()
    def text(content, x, y, size=13, color=(145, 120, 85), centered=False):
        draw.text((x * scale, y * scale), content, font=font(size), fill=color,
                  anchor='mt' if centered else 'lt')

    text('A U R O R A', width / 2, 30, 16, (210, 175, 115), True)
    text('P E R S O N A L   I N T E L L I G E N C E', width / 2, 57, 13, (105, 86, 61), True)
    text('CORE / VOICE', 36, 38, color=(118, 95, 65))
    text('19:00', width - 83, 38)
    text('"Aurora, show me the solar system"', width / 2, height - 132, color=(160, 142, 113), centered=True)
    for i in range(49):
        envelope = math.sin(math.pi * i / 48)**2
        wave = abs(math.sin(12 * 1.3 + i * .48))
        h = 3 + envelope * 6 * wave
        x, y = width / 2 + (i - 24) * 5, height - 80
        draw.rectangle((x*scale, (y-h/2)*scale, (x+2)*scale, (y+h/2)*scale), fill=amber(.45 + envelope*.5))
    text('SAY "AURORA" TO BEGIN', width / 2, height - 55, color=(195, 158, 100), centered=True)
    text('SOFTWARE DESIGN PREVIEW', 32, height - 40, color=(90, 77, 57))
    text('STATE ANIMATION / NOT AUDIO LEVELS', width - 315, height - 40, 11, (90, 77, 57))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.resize((width, height), Image.Resampling.LANCZOS).save(destination)


if __name__ == '__main__':
    render(ROOT / 'docs' / 'core-preview.png')
