"""Software preview of live 3D geometry, not a captured OpenGL window.

Optional dependency: Pillow. Use --motion for an animated design preview.
Point smoothing and bloom approximate the actual OpenGL rasterizer.
"""
from pathlib import Path
import argparse
import math
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jarvis_ui.particle_core import ParticleCore

CORE = ParticleCore()


def render_frame(elapsed=12, width=1200, height=800, state='idle'):
    scale = 2
    size = (width * scale, height * scale)
    image = Image.new('RGB', size, (2, 2, 1))
    light = Image.new('RGB', size)
    draw = ImageDraw.Draw(light)
    cx, cy, radius = CORE.layout(width, height)
    frame = CORE.frame(elapsed, state)
    def point(x, y):
        return ((cx + x * radius) * scale, (cy + y * radius) * scale)
    def amber(alpha, head=False):
        return tuple(int(min(255, value * alpha)) for value in ((255, 197, 76) if head else (255, 143, 15)))
    for trail in frame.orbits:
        for a, b in zip(trail, trail[1:]):
            draw.line([point(*a[:2]), point(*b[:2])], fill=amber(b[2]), width=scale)
    for x0, y0, x1, y1, alpha in (*frame.rays, *frame.links):
        draw.line([point(x0, y0), point(x1, y1)], fill=amber(alpha), width=scale)
    for x, y, alpha, diameter in frame.particles:
        px, py = point(x, y)
        r = diameter * scale / 2
        draw.ellipse((px-r, py-r, px+r, py+r), fill=amber(alpha, diameter == 3))
    image = ImageChops.add(image, light.filter(ImageFilter.GaussianBlur(2 * scale)))
    image = ImageChops.add(image, light.filter(ImageFilter.GaussianBlur(.7 * scale)))
    image = ImageChops.add(image, light)
    center = Image.new('RGB', size)
    draw = ImageDraw.Draw(center)
    for r in range(int(radius * .28 * scale), 0, -1):
        norm = r / (radius * .28 * scale)
        a = math.exp(-norm * 7)
        draw.ellipse((cx*scale-r, cy*scale-r, cx*scale+r, cy*scale+r),
                     fill=(int(255*a), int(220*a), int(75*a)))
    image = ImageChops.add(image, center)
    draw = ImageDraw.Draw(image)
    r = radius * .021 * scale
    draw.ellipse((cx*scale-r, cy*scale-r, cx*scale+r, cy*scale+r), fill=(255,246,175))
    def font(size):
        for candidate in ('DejaVuSans.ttf', 'C:/Windows/Fonts/segoeui.ttf',
                          '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
            try:
                return ImageFont.truetype(candidate, size * scale)
            except OSError:
                continue
        return ImageFont.load_default(size=size * scale)
    def text(content, x, y, size=13, color=(145,120,85), centered=False):
        draw.text((x*scale, y*scale), content, font=font(size), fill=color,
                  anchor='mt' if centered else 'lt')
    text('A U R O R A', width/2, 30, 16, (210,175,115), True)
    text('P A R T I C L E   F L O W', width/2, 57, 11, (105,86,61), True)
    text('"Aurora, show me the solar system"', width/2, height-120,
         size=11, color=(160,142,113), centered=True)
    for i in range(41):
        envelope = math.sin(math.pi*i/40)**2
        h = 3 + envelope * (7 if state == 'idle' else 20) * abs(math.sin(elapsed*2+i*.48))
        x, y = width/2 + (i-20)*5, height-80
        draw.rectangle((x*scale,(y-h/2)*scale,(x+2)*scale,(y+h/2)*scale),fill=amber(.45+envelope*.5))
    text('SOFTWARE MOTION PREVIEW / NOT A DESKTOP CAPTURE', width/2, height-40,
         size=9, color=(125,105,75), centered=True)
    return image.resize((width,height),Image.Resampling.LANCZOS)


def render(destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    render_frame().save(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--motion', action='store_true')
    args = parser.parse_args()
    render(ROOT/'docs'/'core-preview.png')
    if args.motion:
        frames = [render_frame(10+i/15, 720, 540) for i in range(60)]
        frames[0].save(ROOT/'docs'/'core-motion.gif', save_all=True, append_images=frames[1:],
                       duration=67, loop=0, optimize=True)
