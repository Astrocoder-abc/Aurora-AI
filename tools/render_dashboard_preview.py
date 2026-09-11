"""Software preview that executes the real Hologram HUD draw methods.

A tiny immediate-mode OpenGL emulator rasterizes the same glBegin/glVertex/
glDrawArrays calls the live renderer issues, onto Pillow layers. This runs
the shipped code paths (layout, colors, text, particle vertex arrays) —
only the GPU rasterizer is replaced. NOT a desktop capture.

Optional dependency: Pillow.
"""
import ast
import ctypes
import importlib.util
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pygame  # real pygame: fonts and text surfaces are the real thing


class Canvas:
    """Immediate-mode GL stand-in: one composite per begin/end block."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.base = Image.new('RGBA', (w, h), (3, 3, 5, 255))
        self.layer = None
        self.draw = None
        self.additive = False
        self.color = (1.0, 1.0, 1.0, 1.0)
        self.mode = None
        self.verts = []
        self.line_width = 1
        self.point_size = 1
        self.raster = (0, 0)
        self.vertex_ptr = self.color_ptr = None

    # -- state ------------------------------------------------------------
    def glClearColor(self, r, g, b, a):
        pass

    def glClear(self, _mask):
        self.base = Image.new('RGBA', (self.w, self.h), (3, 3, 5, 255))

    def glBlendFunc(self, src, dst):
        self.additive = dst == 'GL_ONE'

    def glColor4f(self, r, g, b, a):
        self.color = (r, g, b, a)

    def glLineWidth(self, w):
        self.line_width = max(1, int(round(w)))

    def glPointSize(self, s):
        self.point_size = max(1.0, s)

    def glBegin(self, mode):
        self.mode = mode
        self.verts = []
        self.layer = Image.new('RGBA', (self.w, self.h), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.layer)

    def glEnd(self):
        if self.layer is None:
            return
        fill = self._fill(self.color)
        mode, verts, draw = self.mode, self.verts, self.draw
        if mode == 'GL_POINTS':
            r = max(1, self.point_size / 2)
            for x, y, f in verts:
                draw.ellipse((x - r, y - r, x + r, y + r), fill=self._fill(f))
        elif mode == 'GL_LINES':
            for i in range(0, len(verts) - 1, 2):
                draw.line([verts[i][:2], verts[i + 1][:2]],
                          fill=self._fill(verts[i][2]), width=self.line_width)
        elif mode in ('GL_LINE_STRIP', 'GL_LINE_LOOP'):
            pts = [v[:2] for v in verts]
            if mode == 'GL_LINE_LOOP':
                pts.append(pts[0])
            for i in range(len(pts) - 1):
                draw.line([pts[i], pts[i + 1]], fill=fill, width=self.line_width)
        elif mode in ('GL_QUADS', 'GL_POLYGON'):
            draw.polygon([v[:2] for v in verts], fill=fill)
        elif mode == 'GL_TRIANGLE_FAN':
            # approximate the radial fade with concentric shrunken polygons,
            # colored by the CENTER vertex (rim vertices carry alpha 0)
            if len(verts) > 3:
                cx, cy = verts[0][:2]
                cr, cg, cb, ca = verts[0][2]
                rim = [v[:2] for v in verts[1:]]
                # linear cone: equal-alpha rings sum to the center alpha
                for scale, mult in ((1.0, .25), (.75, .25), (.5, .25), (.25, .25)):
                    pts = [(cx + (px - cx) * scale, cy + (py - cy) * scale) for px, py in rim]
                    if self.additive:
                        f = (int(min(255, cr * ca * mult * 255)), int(min(255, cg * ca * mult * 255)),
                             int(min(255, cb * ca * mult * 255)), 255)
                    else:
                        f = (int(cr * 255), int(cg * 255), int(cb * 255), min(255, int(ca * mult * 255)))
                    draw.polygon(pts, fill=f)
        elif mode == 'GL_TRIANGLE_STRIP':
            for i in range(len(verts) - 2):
                draw.polygon([v[:2] for v in verts[i:i + 3]], fill=fill)
        self._composite()
        self.layer = self.draw = None
        self.mode = None

    def _fill(self, color):
        r, g, b, a = color
        premult = a if self.additive else 1.0
        return (int(min(255, r * 255 * premult)), int(min(255, g * 255 * premult)),
                int(min(255, b * 255 * premult)), int(min(255, a * 255)))

    def _composite(self):
        if self.additive:
            summed = ImageChops.add(self.base.convert('RGB'), self.layer.convert('RGB'))
            self.base = summed.convert('RGBA')
        else:
            self.base = Image.alpha_composite(self.base, self.layer)

    def glVertex2f(self, x, y):
        self.verts.append((x, y, self.color))

    def glVertex3f(self, x, y, z):
        self.verts.append((x, y, self.color))

    # -- vertex arrays ------------------------------------------------------
    def glVertexPointer(self, size, _t, _stride, ptr):
        self.vertex_ptr = ptr.value if hasattr(ptr, 'value') else ptr

    def glColorPointer(self, size, _t, _stride, ptr):
        self.color_ptr = ptr.value if hasattr(ptr, 'value') else ptr

    def glDrawArrays(self, mode, first, count):
        layer = Image.new('RGBA', (self.w, self.h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        v = (ctypes.c_float * (count * 2)).from_address(self.vertex_ptr)
        c = (ctypes.c_float * (count * 4)).from_address(self.color_ptr)
        if mode == 'GL_POINTS':
            r = max(0.6, self.point_size / 2)
            for i in range(count):
                x, y = v[i * 2], v[i * 2 + 1]
                cr, cg, cb, ca = c[i * 4:i * 4 + 4]
                pre = ca if self.additive else 1.0
                draw.ellipse((x - r, y - r, x + r, y + r),
                             fill=(int(min(255, cr * pre * 255)), int(min(255, cg * pre * 255)),
                                   int(min(255, cb * pre * 255)), int(min(255, ca * 255))))
        elif mode == 'GL_LINES':
            for i in range(0, count - 1, 2):
                cr, cg, cb, ca = c[i * 4:i * 4 + 4]
                pre = ca if self.additive else 1.0
                fa = (int(min(255, cr * pre * 255)), int(min(255, cg * pre * 255)),
                      int(min(255, cb * pre * 255)), int(min(255, ca * 255)))
                draw.line([(v[i * 2], v[i * 2 + 1]), (v[i * 2 + 2], v[i * 2 + 3])], fill=fa, width=1)
        self.layer = layer
        self._composite()
        self.layer = None

    # -- pixels (text blits) -------------------------------------------------
    def glRasterPos2f(self, x, y):
        self.raster = (x, y)

    def glDrawPixels(self, w, h, _fmt, _type, data):
        img = Image.frombytes('RGBA', (w, h), bytes(data))
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
        x, y = int(self.raster[0]), int(self.raster[1] - h)
        layer = Image.new('RGBA', (self.w, self.h), (0, 0, 0, 0))
        layer.paste(img, (x, y))
        self.layer = layer
        prev = self.additive
        self.additive = False
        self._composite()
        self.additive = prev
        self.layer = None

    # -- no-ops ---------------------------------------------------------------
    def noop(self, *args, **kwargs):
        pass


FUNCTION_NAMES = {
    'glClearColor', 'glClear', 'glBlendFunc', 'glColor4f', 'glLineWidth', 'glPointSize',
    'glBegin', 'glEnd', 'glVertex2f', 'glVertex3f', 'glVertexPointer', 'glColorPointer',
    'glDrawArrays', 'glRasterPos2f', 'glDrawPixels',
}


def load_module(canvas):
    # No system libGL in headless CI: stub the OpenGL package like the test
    # suite does, then install the emulator functions below.
    import types
    from unittest.mock import patch
    gl = types.ModuleType('OpenGL.GL')
    gl.__all__ = []
    glu = types.ModuleType('OpenGL.GLU')
    glu.gluPerspective = canvas.noop
    with patch.dict(sys.modules, {'OpenGL': types.ModuleType('OpenGL'),
                                  'OpenGL.GL': gl, 'OpenGL.GLU': glu}):
        spec = importlib.util.spec_from_file_location('preview_hologram', ROOT / 'jarvis_ui' / 'hologram.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    module.PSUTIL_AVAILABLE = False  # stats show N/A in the preview
    tree = ast.parse(Path(module.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and (node.id.startswith('GL_') or node.id.startswith('gl')):
            names.add(node.id)
    for name in names:
        if name in FUNCTION_NAMES:
            setattr(module, name, getattr(canvas, name))
        elif name.startswith('gl') or name.startswith('glu'):
            setattr(module, name, canvas.noop)
        else:  # GL_* constants
            setattr(module, name, name)
    return module


def make_hud(module, width, height):
    module.Hologram._set_display_mode = lambda self, full: None
    pygame.font.init()
    hud = module.Hologram()
    hud.width, hud.height = width, height
    return hud


def snapshot(canvas, hud, path):
    canvas.glClear(0)
    hud._draw_dashboard(hud._current_theme_color())
    canvas.base.convert('RGB').save(path)
    print(f"wrote {path}")


def main():
    width, height = 1280, 800
    canvas = Canvas(width, height)
    module = load_module(canvas)
    hud = make_hud(module, width, height)
    hud.elapsed = 14.0
    hud.voice_available = True
    hud._fps = 60.0
    hud.set_hud_lines([
        "Zoom 1.00  FPS 60",
        "No orbit selected (point to select)",
        "Voice: ready",
        "H1 (L): open_palm x5",
    ])
    hud.log_event("VOICE: online")
    hud.log_event("GRAPHICS: balanced quality")
    hud.log_event("CAMERA OFF: no device")
    snapshot(canvas, hud, ROOT / 'docs' / 'dashboard-preview.png')

    hud.show_weather({
        'location': 'Ghaziabad, Uttar Pradesh, India', 'temp_c': 30,
        'condition': 'cloudy', 'description': 'Overcast', 'humidity': 65,
        'wind_kph': 8, 'source': 'Open-Meteo', 'updated_at': '2026-09-11 15:30',
        'timezone': 'Asia/Kolkata',
    })
    hud.elapsed = 16.0
    hud._core_dock = 1.0  # settled dock position (live app eases there)
    snapshot(canvas, hud, ROOT / 'docs' / 'dashboard-docked-preview.png')


if __name__ == '__main__':
    main()
