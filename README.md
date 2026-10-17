# Aurora: Voice and Gesture Controlled Holographic AI Dashboard

A local, laptop-only AI assistant with a 3D holographic UI (pygame + OpenGL). It uses your webcam and microphone; Arduino/ESP32 and an Android phone are optional. Aurora targets Windows (volume, lock, crash detection and app launching are Windows-only).

## Features

| Area | What it does |
|---|---|
| Holograms | Atoms (all 118 elements, editable), solar system, real star systems, constellations, molecules, math graphs, physics sims, satellites, wireframe shapes, Eiffel Tower, skyscraper, DNA, beating heart, live map of Aurora's own subsystems |
| Hand tracking | MediaPipe landmarks with soft finger scoring, intent gating, dropout bridging and One Euro smoothing. Rotate, zoom, pan, edit orbits, draw shapes in the air |
| Voice AI | Wake word "Aurora", Groq `gpt-oss-120b` with tool calling and live web search, streamed neural speech, offline fallback |
| Vision | Describe the webcam, read QR/barcodes, explain your screen or an error, solve a math problem held to the camera |
| Face recognition | OpenCV LBPH, consent-based enrollment only |
| Sandbox Labs | Rigid-body physics, 3D molecule builder, real circuit simulator (LEDs burn out without a resistor) |
| Aurora Plus | Cowork coding agent, LAN Network Hub, Offline Mode, semantic file search |
| Productivity | Notes, skills (voice macros), calendar, Gmail (OAuth), WhatsApp replies, notifications, file assistant, study mode, translation |
| Creation | PowerPoint / Word / Excel generator, AI image generator |
| Dashboard modes | Project Timeline, Presentation Mode, Adaptive Interface (coding / studying / gaming), Project Workspaces |
| Safety | Emergency System Monitor, Simulation Room (dry-run risky commands), confirmation before any data change |
| Maker tools | Arduino/ESP32 telemetry, Game Companion, Experiment Recorder, voice code writing/editing/debugging, code sandbox |
| Phone (ADB) | Calls, WhatsApp calls, open/close apps, wifi, search, notifications |

## Install

```
python -m venv venv
venv\Scripts\activate          # Mac/Linux: source venv/bin/activate
pip install -r requirements.txt
```

If `pyaudio` fails on Windows: `python -m pip install --user pipwin && python -m pipwin install pyaudio`.

Optional extras: `pip install faster-whisper` (better offline speech), `pocketsphinx`, `pytesseract` (+ the Tesseract program, for offline math reading), `ffmpeg` on PATH (screen recording).

## Run

```
python main.py
```

Two windows open: the hologram dashboard and a debug window with hand landmarks. **F11** toggles fullscreen, **ESC** (or **q** in the debug window) quits.

## Voice setup

1. Get a free key at https://console.groq.com/keys.
2. Copy `api_key.txt.example` to `api_key.txt` and paste the key (no quotes).
3. Run `python main.py`; the log should say `VOICE: listening for wake word 'Aurora'`.
4. Say "Aurora", then your request.

Timers, notes, math, apps, volume, displays and labs work locally without a key or internet. If voice fails, check the event log or `voice_debug.log`.

## Optional configuration

Files live next to `api_key.txt`. None are required.

| Feature | File / requirement |
|---|---|
| Gmail | `google_client.json` (Desktop-app OAuth client from Google Cloud, Gmail API enabled), then say "connect Gmail" (or "connect Gmail with sending"). No password is ever stored |
| Email contacts | `email_config.json`: `{"contacts": {"ann": "ann@x.com"}}` |
| Calendar | `calendar_config.json`: `{"ics": ["C:/cal.ics", "https://.../basic.ics"]}` (read-only) |
| Phone / WhatsApp | ADB + USB debugging; `contacts.json`, `my_number.txt`, `phone_pin.txt`, `wifi_networks.json` |
| Arduino/ESP32 | `iot_config.json`: `{"mode":"serial","port":"COM5","baud":9600}` or `{"mode":"wifi","url":"http://<ip>/telemetry"}` |
| Presentations | `presentations/<name>.md` or `presentation.md` |
| Code projects / vault | `code_projects.json`, `vault_folders.json` |
| Mic tuning | `mic_index.txt`, `energy_threshold.txt` |
| Face strictness | `face_threshold.txt` (lower = stricter, default 80) |
| Cowork models | `cowork_model.txt`, `safeguard_model.txt` |
| Look | `background.jpg/png`; `fonts/Rajdhani-*.ttf`, `fonts/Orbitron-Bold.ttf` |
| Image voice command | `python jarvis_ui/image_generator.py --install` (one time) |

## Voice commands

Say "Aurora" first. Anything that changes your data asks for a "yes" first.

**Displays**: "show me a carbon atom", "add a proton", "next element", "show the solar system", "show trappist-1", "show Orion", "show a water molecule", "show the Eiffel Tower", "show the human heart", "plot x squared", "show a pendulum simulation", "show my systems", "reset the display".

**Sandbox Labs**: "create a projectile with 20 m/s at 30 degrees on the moon", "drop a ball from 15 meters", "physics world" then "add a 5 kg ball" / "push ball 1 into ball 2 with 100 newtons"; "build a molecule", "add 4 hydrogens", "fill hydrogens"; "circuit sandbox", "build an LED circuit", "add a 220 ohm resistor", "wire it up". Close with "close the circuit sandbox".

**Aurora Plus**: "cowork build a prime number checker", "cowork status/stop", "start network hub", "offline mode on/off", "add folder C:/notes to my vault", "index my files", "find files about budget".

**Productivity**
- Notes: "remember this: ...", "search notes for ...", "delete note 2"
- Skills: "teach a skill called warmup: what time is it then open chrome", "run skill warmup"
- Calendar: "what do I have tomorrow", "add lunch tomorrow at noon to my calendar"
- Email: "connect gmail", "read my email", "draft an email to Ann about the demo", "send the email"
- WhatsApp: "reply on WhatsApp saying I'm on my way", "send Mom a WhatsApp saying ..."
- Files: "find PDFs about physics from this month", then "rename these files properly" or "move these into my school folder", then "undo the move"
- Study: "make 8 flashcards about the water cycle", "quiz me on capitals", "start a pomodoro"
- Translate: "translate good morning to Spanish"

**Creation**: "make a 5 slide presentation about black holes", "create a document about climate change", "build a spreadsheet for a monthly budget", "generate an image of a neon city".

**Modes and monitoring**
- "show my project timeline", "presentation mode", then "next slide", "show speaker notes", "timer for 10 minutes", "blank screen", "end presentation"
- "switch to coding / studying / gaming / standard mode"
- "create a workspace for my Mars project", "add task: ...", "what was the temperature in my last experiment", "close workspace"
- "emergency monitor on", "what happened"
- "simulate organizing my downloads", then "go ahead" to run it for real

**Vision and system**: "what do you see", "read this QR code", "solve this problem", "why isn't this code working", "remember my face as Sam", "weather in Tokyo", "take a screenshot", "lock my computer", "volume to 50", "open notepad".

**Phone and code**: "call mom", "open instagram on my phone", "turn off wifi on my phone"; "write python code called sorter that sorts a list", "edit sorter to add a reverse option", "debug sorter", "test in sandbox: ...".

## Gestures

**One hand**

| Gesture | Action |
|---|---|
| Move hand / twist wrist | Rotate / roll |
| Open palm / held fist | Listening / speaking glow |
| Fist + move closer or away | Zoom |
| Fist, then fling open | Throw flash |
| Point | Select next orbit (draw mode: draw a shape) |
| Pinch + move | Reshape selected orbit, or pan if none selected |
| Peace | Save snapshot to `snapshots/` |
| Thumbs up / down | Green flash / red flash + reset |
| OK sign | Cyan flash + media play/pause |
| Rock sign + move up/down | System volume |
| Swipe left/right / up/down | Cycle theme / brightness |

**Two hands**: spread or pinch together to zoom; rotate both to roll.

## Presentation decks

```
# Slide title
- bullet
Notes: speaker notes
Demo: show me a carbon atom
---
```

## Security notes

- Cowork is layered best-effort protection, not a VM: temp folder, stdlib-only Python, static code scan, a fixed `unittest` command, and a safeguard model that fails closed. Output is a ZIP in `cowork_exports/`.
- Network Hub is LAN-only, token-protected (`hub_token.txt`), rate limited, and blocks lock/unlock/delete/forget commands. Keep the link private.
- Gmail uses OAuth with PKCE; read and send permissions are separate, and sending always needs your "yes".
- File commands only touch your user folder, never overwrite, and never delete.

## Tests

```
python test_aurora.py              logic and routing (no camera/mic/API needed)
python test_aurora.py --hardware   camera, mic, ADB, hand tracker
python test_aurora.py --gui        render every hologram mode
python test_aurora.py --live       real Groq, TTS, weather
python test_aurora.py --all
```

## Troubleshooting

- **Black debug window**: another app is using the camera. Close it.
- **Laggy tracking**: lower the resolution in `jarvis_ui/hand_tracker.py`.
- **Hand not detected**: use decent light (dim rooms are auto-enhanced) and keep your hand centered.
- **Face recognition disabled**: `pip uninstall opencv-python opencv-python-headless opencv-contrib-python -y && pip install opencv-contrib-python`, then say "check face recognition". If someone isn't recognized, raise `face_threshold.txt` slightly.
- **No voice input**: create `mic_index.txt` with the device number from the startup log, or `energy_threshold.txt`.
- **Groq limits**: free tier is rate limited (about 30 requests/minute, daily cap on search). Aurora falls back to Offline Mode.
- **Phone features fail**: enable USB debugging and authorise the PC, or say "connect to my phone over wifi".
- **Temperature or crash alerts missing**: CPU temperature may need admin rights; crash detection reads the Windows Application event log.
- **Image generation 429**: the free service allows about one image per 15 seconds; try again shortly.
- **Document creation fails**: `pip install python-pptx python-docx openpyxl`.

## Project structure

```
main.py                       entry point and main loop
test_aurora.py                self-test suite
requirements.txt
jarvis_ui/
  hologram.py                 OpenGL dashboard and all visual models
  hand_tracker.py             MediaPipe gesture recognition
  voice_assistant.py          wake word, speech-to-text, local command router, TTS
  brain.py                    Groq model, tools, code generation, vision
  face_id.py                  consent-based face enrollment and recognition
  system_control.py           volume, media, apps, phone (ADB), code files, QR, notes, vault
  telemetry.py                Arduino/IoT reader, game companion, experiment recorder
  sandbox_labs.py             physics, molecule builder, circuit simulator
  addons.py                   gesture drawing, spatial panels, heart model, plots, code sandbox
  aurora_plus.py              Cowork, network hub, offline mode, semantic file search
  aurora_utilities.py         skills, calendar, email, files, study, translation, WhatsApp
  gmail_client.py             Gmail OAuth 2.0 read/send
  dashboard_modes.py          project timeline and presentation mode
  adaptive_interface.py       coding / studying / gaming layouts
  project_workspace.py        per-project notes, tasks, data and experiments
  operation_simulator.py      dry-run previews of risky commands
  visual_calculator.py        read and solve math from camera or screen
  document_generator.py       PowerPoint, Word and Excel creation
  image_generator.py          AI image creation
  system_monitor.py           emergency alerts for disk, CPU, temperature, network, crashes
```
