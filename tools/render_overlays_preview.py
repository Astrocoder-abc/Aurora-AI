"""Software preview of the real overlay methods, with fixture weather, not live data.

Requires Pillow. No microphone, OpenGL context, or web service is accessed.
The test harness replaces graphics imports; this script supplies Pillow primitives.
"""
from pathlib import Path
import sys
import types
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_functionality import make_display
from jarvis_ui.overlays import overlay_rect


class Font:
    def __init__(self, size):
        for candidate in ('DejaVuSans.ttf', 'C:/Windows/Fonts/segoeui.ttf',
                          '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
            try:
                self.font = ImageFont.truetype(candidate, size)
                break
            except OSError:
                continue
        else:
            self.font = ImageFont.load_default(size=size)
    def size(self, text):
        bounds = self.font.getbbox(text)
        return bounds[2] - bounds[0], bounds[3] - bounds[1]


def preview(kind, width=1200, height=800):
    hud, _ = make_display()
    hud.width, hud.height = width, height
    hud.font_small, hud.font, hud.font_big, hud.font_huge = (Font(s) for s in (13, 16, 28, 54))
    image = Image.new('RGB', (width, height), (3, 3, 2))
    draw = ImageDraw.Draw(image, 'RGBA')
    def color(rgb, alpha=1):
        return tuple(round(v * 255) for v in rgb) + (round(alpha * 255),)
    def rect(x, y, w, h, r, g, b, a, filled=True):
        draw.rectangle((x, y, x+w, y+h), fill=color((r, g, b), a) if filled else None,
                       outline=None if filled else color((r, g, b), a))
    def panel(x, y, w, h, accent, chamfer=12, fill_alpha=.96, **kwargs):
        pts = [(x+chamfer,y),(x+w,y),(x+w,y+h-chamfer),(x+w-chamfer,y+h),
               (x,y+h),(x,y+chamfer),(x+chamfer,y)]
        draw.polygon(pts, fill=(13, 10, 6, 245))
        draw.line(pts, fill=color(accent, .3), width=1)
        draw.line([(x,y+chamfer+10),(x,y+chamfer),(x+chamfer,y)], fill=color(accent,.8),width=2)
    def text(font, message, x, y, color=(238,220,190)):
        draw.text((x,y), message, font=font.font, fill=(*color,255), anchor='lt')
        return font.size(message)[1]
    def cloud(condition, cx, cy, scale, theme):
        # Icon silhouette is a software approximation of the OpenGL cloud primitive.
        for dx, dy, radius in ((-.5,.1,.36),(0,-.1,.5),(.5,.1,.35)):
            x,y,r = cx+dx*scale, cy+dy*scale, scale*radius
            draw.ellipse((x-r,y-r,x+r,y+r),fill=(192,192,177,230))
    hud._draw_rect, hud._draw_panel, hud._blit_text, hud._draw_weather_icon = rect, panel, text, cloud
    theme = (1,.56,.06)
    bounds = overlay_rect(width,height,'weather' if kind=='weather' else 'answer')
    if width >= 1000:
        # Reuse the particle design preview, cropped to its visual area, as a secondary core.
        core_path = ROOT / 'docs' / 'core-preview.png'
        if core_path.exists():
            core = Image.open(core_path).crop((300, 110, 900, 640)).resize((435,385))
            image.paste(core, (45,180))
            draw = ImageDraw.Draw(image,'RGBA')
        text(hud.font_small, 'VOICE CORE / OVERLAY ACTIVE', 115, 596, (129,103,63))
    text(hud.font, 'A U R O R A', width/2-59, 30, (210,175,115))
    text(hud.font_small, 'SOFTWARE DESIGN PREVIEW / SAMPLE DATA', width/2-165, 62, (129,103,63))
    if kind == 'weather':
        hud.show_weather({'location':'Ghaziabad, Uttar Pradesh, India', 'temp_c':30,
                          'description':'overcast', 'condition':'cloudy', 'humidity':65,
                          'wind_kph':8, 'source':'Open-Meteo', 'updated_at':'2026-09-08 15:30',
                          'timezone':'Asia/Kolkata'})
        hud._draw_weather_panel(theme)
    elif kind == 'answer':
        hud.show_info_card('Why does the sky turn orange at sunset?',
            'At sunset, sunlight travels through more of the atmosphere before reaching your eyes. '
            'Much of the blue light is scattered away, leaving warmer red and orange wavelengths. '
            'Dust, moisture and clouds can make those colors even more vivid. '
            'The same scattering process gives a clear daytime sky its blue appearance. '
            'This effect is called Rayleigh scattering.')
        hud._draw_info_card(theme)
    else:
        hud.set_weather_status('error', 'Could not reach the weather provider. Check your internet connection, then repeat your request.')
        hud._draw_weather_panel(theme)
    text(hud.font_small, 'Voice operated. No clickable controls.', width/2-124,height-70,(155,130,90))
    return image


if __name__ == '__main__':
    weather = preview('weather')
    answer = preview('answer')
    sheet = Image.new('RGB',(1200,1000),(3,3,2))
    sheet.paste(weather.resize((900,600)),(150,0))
    sheet.paste(answer.resize((600,400)),(0,600))
    sheet.paste(preview('error').resize((600,400)),(600,600))
    (ROOT/'docs').mkdir(exist_ok=True)
    sheet.save(ROOT/'docs'/'overlays-preview.png')
