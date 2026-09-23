# Aurora — Voice-Controlled Holographic AI Dashboard

A local, laptop-only AI assistant with a hand-gesture-controlled 3D
holographic UI. Runs entirely on your machine's webcam and mic — no
external hardware required.

## Features

- **Hologram display** (pygame + OpenGL): atoms, the solar system, real
  star systems, wireframe shapes, an Eiffel Tower / skyscraper / DNA
  model, and a live constellation view of Aurora's own subsystems.
- **Hand tracking** (MediaPipe): gestures rotate, zoom, pan, and edit the
  display; a full static-pose gesture set (fist, open palm, pinch, peace,
  thumbs up/down, OK, rock sign, point).
- **Voice assistant** (Groq API): wake word "Aurora", speech-to-text,
  streamed spoken replies, and built-in web search for time-sensitive
  questions.
- **Face recognition** (OpenCV/LBPH): consent-based enrollment only —
  nobody is identified unless they explicitly ask to be remembered.
- **System control**: volume, media keys, launching apps, timers, quick
  math, notes, screenshots, locking the PC.
- **Phone control** (ADB, optional): calls, WhatsApp calls, opening apps,
  wifi, and search on a connected Android phone.
- **Weather** (Open-Meteo, free/keyless): current conditions by city or
  auto-detected location.

## 1. Install

```
python -m venv venv
```

Activate it:
- Windows: `venv\Scripts\activate`
- Mac/Linux: `source venv/bin/activate`

Install dependencies:
```
pip install -r requirements.txt
```

> MediaPipe is CPU-friendly — no dedicated GPU needed.

If `pyaudio` fails to install on Windows:
```
python -m pip install --user pipwin
python -m pipwin install pyaudio
```

## 2. Run

```
python main.py
```

Two windows open:
1. **Hologram dashboard** — the main holographic display.
2. **Debug window** — webcam feed with hand landmarks and live
   gesture/finger-count labels, to confirm tracking is working.

Press **F11** to toggle fullscreen, **ESC** or **q** (in the debug
window) to quit.

## 3. Voice setup (optional but recommended — free)

Uses Groq's API, which is free with no credit card required.

1. Get a free key at https://console.groq.com/keys.
2. Rename `api_key.txt.example` to `api_key.txt` and paste your key in
   (no quotes, no extra text). Keep this file private.
3. Run `python main.py` as usual. You should see
   `VOICE: listening for wake word 'Aurora'` in the log.
4. Say **"Aurora"** followed by your request.

If voice doesn't activate, check the dashboard's event log or
`voice_debug.log` — the most common causes are a missing/invalid API
key, no microphone detected, or a failed `pyaudio` install. Everything
else (gestures, hologram) keeps working even if voice can't start.

## 4. Optional setup

| Feature | Requirement |
|---|---|
| Phone control (calls, WhatsApp, app launch, wifi) | [ADB](https://developer.android.com/tools/releases/platform-tools) installed, USB debugging enabled on your phone — see `jarvis_ui/phone_control.py` |
| Volume control | `pycaw` (already in `requirements.txt`) |
| Custom background | drop `background.jpg`/`.png` in the project root |
| Custom fonts | drop `Rajdhani-*.ttf` / `Orbitron-Bold.ttf` in a `fonts/` folder |
| Code editing by voice | uses the same Groq key as voice — see `jarvis_ui/code_control.py` |

## Voice commands

```
"Aurora, show me a carbon atom"          real Bohr-model diagram
"Aurora, add a proton" / "remove 2 electrons" / "add 3 neutrons"
"Aurora, start a new element"
"Aurora, show me the solar system"
"Aurora, show me a sphere"               (also: cube, torus, pyramid, cylinder)
"Aurora, show the Eiffel Tower" / "show a skyscraper"
"Aurora, show me a double helix"
"Aurora, show my systems"                Aurora's own subsystems, as a network
"Aurora, select orbit one" / "deselect orbit"
"Aurora, reset the display"
"Aurora, what's the weather right now?" / "weather in Tokyo"
"Aurora, close the weather"
"Aurora, what time is it?"
"Aurora, what's 47 times 12" / "15 percent of 200"
"Aurora, set a timer for 5 minutes" / "how much time is left"
"Aurora, remember my face as Sam" / "forget Sam's face"
"Aurora, open notepad"                   (also: calc, spotify, chrome, explorer...)
"Aurora, volume up/down" / "volume to 50" / "mute"
"Aurora, play music" / "pause" / "next song" / "previous song"
"Aurora, take a screenshot" / "system status" / "lock my computer"
"Aurora, take a note: ..." / "read my notes"
"Aurora, call mom" / "call me"           requires ADB + contacts.json
"Aurora, call mom on WhatsApp"           requires ADB
"Aurora, stop"                           interrupts speech mid-sentence
"Aurora, hello"                          general chat, shown as a response card
```

## Gesture guide

**One hand**
| Gesture | Action |
|---|---|
| Move hand | Rotate the hologram |
| Wrist twist | Roll the hologram |
| Open palm | "Listening" glow |
| Fist (held) | "Speaking" glow |
| Fist + move closer/away | Zoom (grab-and-pull) |
| Fist → fling open fast | "Throw" flash |
| Point | Select the next orbit/shell |
| Pinch (orbit selected) + move | Reshape it — vertical = radius, horizontal = spin speed |
| Pinch (nothing selected) + move | Pan the whole hologram |
| Peace sign | Save a snapshot to `snapshots/` |
| Thumbs up | Green confirm flash |
| Thumbs down | Red flash + reset display |
| OK sign | Cyan flash + media play/pause |
| Rock sign, held + move up/down | System volume |
| Swipe left/right (open palm) | Cycle color theme |
| Swipe up/down (open palm) | Adjust brightness |

**Two hands**
| Gesture | Action |
|---|---|
| Spread apart / bring together | Zoom in/out |
| Both hands rotate together | Roll the hologram |

## Troubleshooting

- **Webcam doesn't open / black debug window** — another app (Zoom,
  Teams, a browser tab) may be holding the camera. Close it and rerun.
- **Low frame rate / laggy tracking** — lower the resolution in
  `jarvis_ui/hand_tracker.py`, or close other heavy background apps.
- **Hand not detected reliably** — use decent lighting and keep your
  hand roughly centered in frame.
- **`ImportError` on mediapipe/OpenGL** — confirm the virtual
  environment is activated before `pip install`.
- **Face recognition disabled** — usually an OpenCV install conflict.
  Run: `pip uninstall opencv-python opencv-python-headless
  opencv-contrib-python -y && pip install opencv-contrib-python`. Say
  "Aurora, check face recognition" for a live diagnosis.
- **Voice never picks up audio** — create `mic_index.txt` with the
  device number shown in the log at startup.
- **Groq free tier limit** — rate-limited (not a token/dollar cap),
  roughly 30 requests/minute with a per-day cap on the search-capable
  model. Resets the next day.

## Project structure

```
main.py                     entry point, main loop
jarvis_ui/
  hologram.py                OpenGL dashboard + all visual models
  hand_tracker.py             MediaPipe gesture recognition
  voice_assistant.py          wake word, STT, Groq brain, TTS
  face_id.py                  consent-based face enrollment/recognition
  system_control.py           volume, media keys, app launching
  phone_control.py            ADB-based phone control
  code_control.py             voice-driven code generation/editing
requirements.txt
api_key.txt                  your Groq key (create from .example)
```
