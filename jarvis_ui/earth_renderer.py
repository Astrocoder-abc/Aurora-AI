"""
Satellite-imagery Earth for satellite mode: textured, lit, tilted and slowly rotating sphere with
coastlines/country borders baked into the texture, plus continent, ocean and country labels.
Imagery and borders are downloaded once into earth_data/ (Pillow required).
Entry points: draw(hologram, brightness) -> bool (False = use the fallback), draw_labels(hologram).
"""
import json
import math
import os
import threading
import time
import urllib.request

import numpy as np
from OpenGL.GL import *
from OpenGL.GLU import gluBuild2DMipmaps, gluProject

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = os.path.join(BASE, "earth_data")
IMAGE, LABELS = os.path.join(DATA_DIR, "earth_satellite.jpg"), os.path.join(DATA_DIR, "earth_labels.json")
W, H, RADIUS, RETRY_SEC = 4096, 2048, 0.5, 60
IMAGE_URLS = ["https://cdn.jsdelivr.net/gh/mrdoob/three.js@r150/examples/textures/planets/earth_atmos_2048.jpg",
              "https://threejs.org/examples/textures/planets/earth_atmos_2048.jpg"]
GEO_URLS = ["https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@master/geojson/ne_110m_admin_0_countries.geojson",
            "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"]
CONTINENTS = [("NORTH AMERICA", 45, -100), ("SOUTH AMERICA", -15, -60), ("EUROPE", 52, 15), ("AFRICA", 5, 20),
              ("ASIA", 45, 90), ("AUSTRALIA", -25, 134), ("ANTARCTICA", -80, 0)]
OCEANS = [("PACIFIC OCEAN", 0, -150), ("PACIFIC OCEAN", -10, 175), ("ATLANTIC OCEAN", 10, -35), ("INDIAN OCEAN", -15, 80),
          ("ARCTIC OCEAN", 85, 0), ("SOUTHERN OCEAN", -62, 90)]
NL, NO = 48, 96

_S = {"status": "idle", "tex": None, "mesh": None, "countries": [], "t": 0.0, "logged": False}


# ---- asset preparation (background thread, no GL) ---------------------------
def _fetch(urls, dest):
    for u in urls:
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "aurora-earth/1.0"}), timeout=40) as r:
                data = r.read()
            if len(data) > 10000:
                with open(dest, "wb") as f:
                    f.write(data)
                return True
        except Exception:
            continue
    return False


def _rings(geom):
    if geom.get("type") == "Polygon":
        return list(geom["coordinates"])
    if geom.get("type") == "MultiPolygon":
        return [r for poly in geom["coordinates"] for r in poly]
    return []


def _prop(p, *keys):
    for k in keys:
        if p.get(k) is not None:
            return p[k]
    return None


def _build():
    from PIL import Image, ImageDraw
    os.makedirs(DATA_DIR, exist_ok=True)
    if not (os.path.exists(IMAGE) and os.path.exists(LABELS)):
        base, geo = os.path.join(DATA_DIR, "earth_base.jpg"), os.path.join(DATA_DIR, "countries.geojson")
        if not os.path.exists(base) and not _fetch(IMAGE_URLS, base):
            raise RuntimeError("could not download satellite imagery")
        features = []
        if os.path.exists(geo) or _fetch(GEO_URLS, geo):
            with open(geo, encoding="utf-8") as f:
                features = json.load(f).get("features", [])
        im = Image.open(base).convert("RGBA").resize((W, H), Image.LANCZOS)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for lon in range(-180, 181, 30):                       # graticule
            x = (lon + 180) / 360 * W
            d.line([(x, 0), (x, H)], fill=(130, 180, 255, 55), width=1)
        for lat in range(-90, 91, 30):
            y = (90 - lat) / 180 * H
            d.line([(0, y), (W, y)], fill=(255, 220, 120, 90) if lat == 0 else (130, 180, 255, 55), width=1)
        labels = []
        for ft in features:
            for ring in _rings(ft.get("geometry") or {}):
                pts = [((lo + 180) / 360 * W, (90 - la) / 180 * H) for lo, la in (c[:2] for c in ring)]
                seg = [pts[0]] if pts else []
                for a, b in zip(pts, pts[1:]):
                    if abs(b[0] - a[0]) > W / 2:               # antimeridian jump: break the line
                        if len(seg) > 1:
                            d.line(seg, fill=(255, 255, 255, 185), width=2, joint="curve")
                        seg = [b]
                    else:
                        seg.append(b)
                if len(seg) > 1:
                    d.line(seg, fill=(255, 255, 255, 185), width=2, joint="curve")
            p = ft.get("properties") or {}
            name, lx, ly = _prop(p, "NAME", "name"), _prop(p, "LABEL_X", "label_x"), _prop(p, "LABEL_Y", "label_y")
            if name and lx is not None and ly is not None:
                labels.append([name, float(ly), float(lx), int(_prop(p, "LABELRANK", "labelrank") or 5)])
        with open(LABELS, "w", encoding="utf-8") as f:
            json.dump(labels, f)
        Image.alpha_composite(im, layer).convert("RGB").save(IMAGE, quality=93)
    with open(LABELS, encoding="utf-8") as f:
        _S["countries"] = json.load(f)


def _prepare():
    try:
        _build()
        _S["status"] = "ready"
    except Exception as e:
        _S.update(status="failed", t=time.time(), error=f"{type(e).__name__}: {str(e)[:80]}", logged=False)


# ---- GL resources (render thread) ---------------------------------------------
def _upload():
    try:
        from PIL import Image
        im = Image.open(IMAGE).convert("RGB").transpose(Image.FLIP_TOP_BOTTOM)
        tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        gluBuild2DMipmaps(GL_TEXTURE_2D, GL_RGB, im.width, im.height, GL_RGB, GL_UNSIGNED_BYTE, im.tobytes())
        _S["tex"] = tex
        return True
    except Exception as e:
        _S.update(status="failed", t=time.time(), error=f"{type(e).__name__}: {str(e)[:80]}", logged=False)
        return False


def _mesh():
    if _S["mesh"] is None:
        la, lo = np.linspace(-math.pi / 2, math.pi / 2, NL + 1), np.linspace(-math.pi, math.pi, NO + 1)
        LA, LO = np.meshgrid(la, lo, indexing="ij")
        pos = np.ascontiguousarray(np.stack([np.cos(LA) * np.sin(LO), np.sin(LA), np.cos(LA) * np.cos(LO)], -1).reshape(-1, 3), np.float32)
        uv = np.ascontiguousarray(np.stack([(LO + math.pi) / (2 * math.pi), (LA + math.pi / 2) / math.pi], -1).reshape(-1, 2), np.float32)
        ii, jj = np.meshgrid(np.arange(NL), np.arange(NO), indexing="ij")
        a = (ii * (NO + 1) + jj).ravel()
        c = a + NO + 1
        idx = np.stack([a, c, a + 1, a + 1, c, c + 1], 1).ravel().astype(np.uint32)
        _S["mesh"] = (pos, uv, idx)
    return _S["mesh"]


# ---- drawing -------------------------------------------------------------------
def _labels(h, mv, proj, vp):
    zoom_rank = 2 if h.zoom < 1.4 else 4 if h.zoom < 2.2 else 6
    items = [(n, la, lo, "continent") for n, la, lo in CONTINENTS] + [(n, la, lo, "ocean") for n, la, lo in OCEANS] \
        + [(n, la, lo, "country") for n, la, lo, rk in _S["countries"] if rk <= zoom_rank]
    lat, lon = np.radians([i[1] for i in items]), np.radians([i[2] for i in items])
    p = np.stack([np.cos(lat) * np.sin(lon), np.sin(lat), np.cos(lat) * np.cos(lon)], 1) * 1.01
    eye = p @ mv[:3, :3] + mv[3, :3]
    n = eye - mv[3, :3]
    facing = (n * -eye).sum(1) / (np.linalg.norm(n, axis=1) * np.linalg.norm(eye, axis=1) + 1e-9)
    out = []
    for i in np.nonzero(facing > 0.2)[0]:
        sx, sy, sz = gluProject(*p[i], mv, proj, vp)
        if 0 <= sz <= 1:
            out.append((sx, h.height - sy, items[i][0], items[i][3]))
    h._earth_labels = out


def draw(h, brightness):
    """Draws the textured Earth at the current origin. False -> caller draws its fallback."""
    h._earth_labels = []
    st = _S["status"]
    if st == "failed" and time.time() - _S["t"] > RETRY_SEC:
        st = _S["status"] = "idle"
    if st == "idle":
        _S["status"] = "loading"
        h.log_event("EARTH: loading satellite imagery")
        threading.Thread(target=_prepare, daemon=True).start()
        return False
    if st == "failed":
        if not _S["logged"]:
            _S["logged"] = True
            h.log_event(f"EARTH: imagery unavailable ({_S.get('error', '')})")
        return False
    if st != "ready" or (_S["tex"] is None and not _upload()):
        return False

    pos, uv, idx = _mesh()
    b = min(1.0, 0.7 + 0.3 * brightness)
    glPushMatrix()
    glRotatef(23.4, 0, 0, 1)                                     # axial tilt
    glRotatef(h.elapsed * 4.0, 0, 1, 0)                          # slow spin
    glScalef(RADIUS, RADIUS, RADIUS)
    mv = np.array(glGetDoublev(GL_MODELVIEW_MATRIX))
    proj, vp = glGetDoublev(GL_PROJECTION_MATRIX), glGetIntegerv(GL_VIEWPORT)

    glPushMatrix()
    glLoadIdentity()                                             # sun fixed relative to the viewer
    glLightfv(GL_LIGHT0, GL_POSITION, (-0.6, 0.5, 1.0, 0.0))
    glPopMatrix()
    glLightfv(GL_LIGHT0, GL_DIFFUSE, (1.0, 1.0, 0.95, 1.0))
    glLightfv(GL_LIGHT0, GL_AMBIENT, (0.45, 0.45, 0.5, 1.0))
    glEnable(GL_LIGHTING)
    glEnable(GL_LIGHT0)
    glEnable(GL_COLOR_MATERIAL)
    glEnable(GL_TEXTURE_2D)
    glBindTexture(GL_TEXTURE_2D, _S["tex"])
    glColor4f(b, b, b, 1.0)
    glEnableClientState(GL_VERTEX_ARRAY)
    glEnableClientState(GL_NORMAL_ARRAY)
    glEnableClientState(GL_TEXTURE_COORD_ARRAY)
    glVertexPointer(3, GL_FLOAT, 0, pos)
    glNormalPointer(GL_FLOAT, 0, pos)
    glTexCoordPointer(2, GL_FLOAT, 0, uv)
    glDrawElements(GL_TRIANGLES, len(idx), GL_UNSIGNED_INT, idx)
    glDisableClientState(GL_TEXTURE_COORD_ARRAY)
    glDisableClientState(GL_NORMAL_ARRAY)
    glDisableClientState(GL_VERTEX_ARRAY)
    glDisable(GL_TEXTURE_2D)
    glDisable(GL_COLOR_MATERIAL)
    glDisable(GL_LIGHTING)
    try:
        _labels(h, mv, proj, vp)
    except Exception:
        h._earth_labels = []
    glPopMatrix()
    h._draw_glow_fan(RADIUS, 0.3 * b, 0.6 * b, 1.0 * b, passes=4)   # atmosphere halo
    return True


def draw_labels(h):
    """Screen-space continent / ocean / country names (called from the dashboard pass)."""
    if not h.font_small:
        return
    styles = {"continent": (h.font or h.font_small, (255, 244, 200)), "ocean": (h.font_small, (130, 195, 255)),
              "country": (h.font_small, (235, 242, 255))}
    for x, y, text, kind in getattr(h, "_earth_labels", []):
        font, col = styles[kind]
        x -= font.size(text)[0] / 2
        h._blit_text(font, text, x + 1, y + 1, color=(0, 0, 0))
        h._blit_text(font, text, x, y, color=col)
