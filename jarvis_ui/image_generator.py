"""
Image generator: creates AI images by voice. Files land in generated_images/ and open automatically.

  "Aurora, generate an image of a neon city at night"      (also: create / make / draw / paint / render)
  "Aurora, make a picture of a dragon wallpaper"           ("wallpaper"/"landscape" -> wide, "portrait"/"phone" -> tall)

Uses Pollinations' free keyless endpoint (Flux model). Anonymous use is rate-limited (about one image per 15 s)
and may carry a watermark; Aurora retries politely. Needs internet; standard library only.
Entry points: handle_command(voice, text) for voice_assistant.py, generate(prompt) for tools.

One-time setup (adds the voice hook, the AI tool and the tool mapping, then you can delete nothing):
    python image_generator.py --install
"""
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT_DIR = os.path.join(BASE, "generated_images")
API = "https://image.pollinations.ai/prompt/"
RETRIES, RETRY_WAIT = 3, 16

_REQ = re.compile(r"\b(?:generate|create|make|draw|paint|render|produce)\s+(?:me\s+)?(?:an?\s+)?(?:ai\s+)?"
                  r"(?:image|picture|photo|painting|illustration|drawing|artwork|art)\s+(?:of|about|showing|with)\s+(.+)$", re.I)


def _size(prompt):
    p = prompt.lower()
    if re.search(r"\b(?:wallpaper|landscape|panorama|widescreen|banner)\b", p):
        return 1344, 768
    if re.search(r"\b(?:portrait|phone|poster|vertical)\b", p):
        return 768, 1344
    return 1024, 1024


def generate(prompt):
    """-> (path, error). Never raises."""
    prompt = " ".join(prompt.split())[:400]
    if not prompt:
        return None, "Tell me what to draw."
    w, h = _size(prompt)
    url = (API + urllib.parse.quote(prompt) + "?" + urllib.parse.urlencode(
        {"width": w, "height": h, "model": "flux", "nologo": "true", "seed": random.randint(1, 10 ** 6)}))
    last = "unknown error"
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aurora-image/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                data = r.read()
            if len(data) < 1000 or not (data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n"):
                raise ValueError("the service did not return an image")
            os.makedirs(OUT_DIR, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "_", prompt.lower()).strip("_")[:40] or "image"
            ext = "png" if data[:4] == b"\x89PNG" else "jpg"
            path = os.path.join(OUT_DIR, f"{slug}_{time.strftime('%Y%m%d_%H%M%S')}.{ext}")
            with open(path, "wb") as f:
                f.write(data)
            return path, None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(getattr(e, "reason", e))[:80]
        if attempt < RETRIES - 1:
            time.sleep(RETRY_WAIT)
    return None, last


def handle_command(voice, text):
    """Voice entry point for voice_assistant.py. Returns True if handled."""
    m = _REQ.search(text.strip(" .!?,"))
    if not m:
        return False
    prompt = m.group(1).strip(" .")
    plus = getattr(voice, "plus", None)
    if plus is not None and not plus.offline.online():
        voice._speak("Image generation needs the internet, and I'm offline right now.")
        return True
    voice._speak(f"Generating an image of {prompt}. This can take up to a minute.")
    path, err = generate(prompt)
    if err:
        voice._log(f"IMAGE: failed ({err})")
        voice._speak("Sorry, I couldn't generate that image." + (" The free service is rate limited, so try again in a few seconds." if "429" in err else ""))
        return True
    voice._log(f"IMAGE: created {path}")
    try:
        voice.hologram.show_info_card(f"Image: {prompt[:50]}", os.path.basename(path))
    except Exception:
        pass
    try:
        if os.name == "nt":
            os.startfile(path)
    except OSError:
        pass
    voice._speak("Your image is ready and saved in the generated images folder.")
    return True


# ---------------------------------------------------------------------------- installer
_EDITS = {
    "voice_assistant.py": [
        ('"system_monitor", "document_generator"))', '"system_monitor", "document_generator", "image_generator"))'),
        ("sandbox_labs, addons, productivity_commands, dashboard_modes, system_monitor, document_generator = (",
         "sandbox_labs, addons, productivity_commands, dashboard_modes, system_monitor, document_generator, image_generator = ("),
        ("        if document_generator and document_generator.handle_command(self, text):   # presentations, documents, spreadsheets\n            return True\n",
         "        if document_generator and document_generator.handle_command(self, text):   # presentations, documents, spreadsheets\n            return True\n"
         "        if image_generator and image_generator.handle_command(self, text):         # AI image generation\n            return True\n"),
        ("        elif name == \"create_document\":\n",
         "        elif name == \"generate_image\":\n            self._turn_display = True\n            phrase = f\"generate an image of {g('prompt')}\"\n"
         "        elif name == \"create_document\":\n"),
    ],
    "brain.py": [
        ("    _fn(\"search_files\", \"Search the user's local knowledge vault (their notes/documents).\", {\"query\": _S(\"Keywords\")}, [\"query\"]),\n",
         "    _fn(\"search_files\", \"Search the user's local knowledge vault (their notes/documents).\", {\"query\": _S(\"Keywords\")}, [\"query\"]),\n"
         "    _fn(\"generate_image\", \"Generate an AI image from a text description and open it.\", {\"prompt\": _S(\"What the image shows\")}, [\"prompt\"]),\n"),
    ],
}


def install():
    here = os.path.dirname(os.path.abspath(__file__))
    for name, edits in _EDITS.items():
        path = os.path.join(here, name)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if "image_generator" in src or "generate_image" in src:
            print(f"{name}: already installed")
            continue
        for old, new in edits:
            if src.count(old) != 1:
                sys.exit(f"{name}: anchor matched {src.count(old)} times, nothing changed:\n{old[:80]}")
            src = src.replace(old, new)
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(open(path, encoding="utf-8").read())
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        print(f"{name}: patched (backup {name}.bak)")


if __name__ == "__main__":
    if "--install" in sys.argv:
        install()
    else:
        print("Usage: python jarvis_ui/image_generator.py --install")
