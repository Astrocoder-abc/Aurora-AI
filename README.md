# Aurora — Voice-Controlled Holographic AI Dashboard

A local, laptop-only AI assistant with a hand-gesture-controlled 3D
holographic UI. Runs on your machine's webcam and mic — no external
hardware required (Arduino/ESP32 and an Android phone are optional extras).

## Features

- **Hologram display** (pygame + OpenGL): atoms (all 118 elements), the solar
  system, real star systems, constellations, molecules, math graphs, physics
  sims, satellites, wireframe shapes, an Eiffel Tower / skyscraper / DNA /
  human-heart model, and a live constellation view of Aurora's own subsystems.
- **Hand tracking** (MediaPipe): gestures rotate, zoom, pan, and edit the
  display; a full static-pose gesture set (fist, open palm, pinch, peace,
  thumbs up/down, OK, rock sign, point). **Draw a shape in the air** and
  Aurora turns it into a hologram.
- **Voice assistant** (Groq API, `gpt-oss-120b`): wake word "Aurora",
  speech-to-text, streamed spoken replies, tool calling (the AI can drive
  every feature below), and built-in web search for time-sensitive questions.
- **Vision**: ask what the webcam sees, or have it read QR codes and barcodes.
- **Face recognition** (OpenCV/LBPH): consent-based enrollment only —
  nobody is identified unless they explicitly ask to be remembered.
- **Sandbox Labs**: Physics sandbox, Molecule builder (3D, valence-aware),
  and a real Circuit simulator (nodal analysis, LEDs burn out without a resistor).
- **Aurora Plus**: Cowork (sandboxed autonomous coding agent), Network Hub
  (control Aurora from your phone over LAN), Offline Mode, and semantic file search.
- **Productivity**: skills (voice macros), calendar, email, notifications,
  file utilities, quick notes, study mode (flashcards, quizzes, pomodoro), translation.
- **Dashboard modes**: Project Timeline and Presentation Mode (slides from Markdown).
- **Emergency System Monitor**: explains sudden disk loss, overheating,
  network drops, app crashes, and unusual CPU use.
- **Maker tools**: Arduino/ESP32 telemetry, Game Companion overlay,
  Experiment Recorder with report, voice-driven code writing/editing, code sandbox.
- **System control**: volume, media keys, launching apps, timers, quick
  math, notes, screenshots, screen recording, locking the PC.
- **Phone control** (ADB, optional): calls, WhatsApp calls, opening apps,
  wifi, search, and notifications on a connected Android phone.
- **Weather** (Open-Meteo, free/keyless): current conditions by city or
  auto-detected location.
- **Gmail (OAuth)**: read unread mail and send drafts without Aurora ever
  seeing your password. Read and send are separate permissions, and sending
  always needs your "yes".
- **WhatsApp replies** (ADB): read incoming WhatsApp notifications, have the
  AI write a reply, and send it after you confirm.
- **File assistant**: find files by type, topic or date, then batch rename or
  move them (confirmed first, with undo).
- **Screen understanding**: ask why your code fails or what an error means and
  Aurora reads your screen.
- **Visual calculator**: hold a math problem to the camera (or have it on
  screen) and Aurora reads and solves it, online or offline (local OCR).
- **Document generator**: AI-written PowerPoint, Word and Excel files by voice.
- **Image generator**: AI images by voice (free Pollinations endpoint).
- **Adaptive interface**: switch the dashboard to coding, studying or gaming
  layouts by voice.
- **Project workspaces**: one folder and overlay per project (notes, tasks,
  links, files, data, experiments, project chat) that you can search and ask
  questions about.
- **Simulation Room**: preview what a disruptive command would do, without
  touching anything, then say "go ahead" to run it for real.
- **Code debugging loop**: AI writes code, the sandbox runs it, and the AI
  fixes it from the traceback.

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

> MediaPipe is CPU-friendly — no dedicated GPU needed. Aurora targets
> Windows; volume, lock, crash detection and app launching are Windows-only.

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

## 3. Voice setup

Voice is Aurora's main interface. It needs a working microphone and `pyaudio`,
plus a Groq API key for the AI brain (free, no credit card required).

1. Get a free key at https://console.groq.com/keys.
2. Rename `api_key.txt.example` to `api_key.txt` and paste your key in
   (no quotes, no extra text). Keep this file private.
3. Run `python main.py` as usual. You should see
   `VOICE: listening for wake word 'Aurora'` in the log.
4. Say **"Aurora"** followed by your request.

The key powers open-ended chat, tool calling, web search, vision descriptions,
code generation, translation and flashcards. Built-in commands (timers, notes,
math, apps, volume, displays, labs) run locally and keep working if the key
or internet is missing.

If voice doesn't activate, check the dashboard's event log or
`voice_debug.log` — the usual causes are a missing/invalid API key, no
microphone, or a failed `pyaudio` install.

## 4. Optional setup

Config files live next to `api_key.txt`. None are required.

| Feature | Requirement |
|---|---|
| Phone control & notifications | [ADB](https://developer.android.com/tools/releases/platform-tools), USB debugging on; `contacts.json`, `my_number.txt`, `phone_pin.txt`, `wifi_networks.json` |
| Arduino/ESP32 telemetry | `iot_config.json` — `{"mode":"serial","port":"COM5","baud":9600}` or `{"mode":"wifi","url":"http://<ip>/telemetry"}` |
| Calendar | `calendar_config.json` — `{"ics": ["C:/path/cal.ics", "https://.../basic.ics"]}` (read-only) |
| Email | `email_config.json` (`imap_host`, `smtp_host`, `username`, `contacts`) + env `AURORA_EMAIL_PASSWORD` or `email_password.txt` (use an app password) |
| Screen recording | `ffmpeg` on PATH |
| Offline speech recognition | `pip install pocketsphinx` |
| Presentations | `presentations/<name>.md` or `presentation.md` (see below) |
| Code projects / file vault | `code_projects.json`, `vault_folders.json` |
| Custom background | drop `background.jpg`/`.png` in the project root |
| Custom fonts | drop `Rajdhani-*.ttf` / `Orbitron-Bold.ttf` in a `fonts/` folder |
| Mic / sensitivity | `mic_index.txt`, `energy_threshold.txt` |
| Face match strictness | `face_threshold.txt` (lower = stricter, default 80) |
| Cowork models | `cowork_model.txt`, `safeguard_model.txt` |
| Gmail (OAuth) | `google_client.json` (Desktop-app OAuth client from Google Cloud with the Gmail API enabled), then say "connect gmail" (see Gmail setup below). `email_config.json` now only needs `contacts` |
| Offline math reading | `pip install pytesseract` plus the Tesseract program |
| Offline speech (better) | `pip install faster-whisper` (Whisper small, used before PocketSphinx) |
| Documents | `pip install python-pptx python-docx openpyxl`; files land in `documents/` |
| Image generator voice command | one-time: `python jarvis_ui/image_generator.py --install`; files land in `generated_images/` |
| Project workspaces | created on demand in `workspaces/<project>/` |

## Voice commands

Say "Aurora" first, then the command. `/` separates alternate phrasings.

### Displays & lab
| Say | Action |
|---|---|
| "show me a carbon atom" | Bohr-model atom (any of 118 elements) |
| "add a proton" / "remove 2 electrons" / "add 3 neutrons" | Edit the current atom |
| "start a new element" / "next element" | Custom atom / step through the table |
| "show me the solar system" | Sun + planets |
| "show trappist-1" / "next system" | Real star systems (unknown names are generated) |
| "show Orion" / "show the big dipper" | Constellation star maps |
| "show a water molecule" / "show methane" | Ball-and-stick molecule |
| "show me a sphere" | Also: cube, torus, pyramid, cylinder |
| "show the Eiffel Tower" / "show a skyscraper" / "show a double helix" | Landmark and DNA models |
| "show the human heart" | Beating 3D heart |
| "plot x squared" / "graph sine of x" | Math graph |
| "show a pendulum simulation" / "show a projectile" | Quick physics sims |
| "show the ISS satellite" | Body orbiting Earth |
| "show my systems" | Aurora's own subsystems as a network |
| "select orbit one" / "deselect orbit" / "reset the display" | Orbit editing and reset |

### Sandbox Labs
Say "close the physics / molecule / circuit sandbox" to exit.

| Say | Action |
|---|---|
| "create a projectile with 20 m/s velocity at 30 degrees on the moon" | Projectile with range, peak, flight time |
| "drop a ball from 15 meters" / "create a pendulum with length 2 meters" | Free fall / pendulum |
| "create a spring with constant 40" / "clear physics" | Mass-spring / reset |
| "build a molecule" / "add carbon" / "add 4 hydrogens" / "fill hydrogens" | Build molecules atom by atom |
| "double bond between 1 and 2" / "select atom 2" / "remove atom 3" | Edit bonds and atoms |
| "build a water molecule" / "stop spinning" / "molecule info" | Presets, spin control, formula |
| "circuit sandbox" / "add a 220 ohm resistor" / "add a green LED" | Open circuit lab, add parts |
| "add a battery" / "add an arduino pin" / "add a switch" / "wire it up" | More parts, auto-wire in series |
| "build an LED circuit" / "build a blink circuit" / "build an RC circuit" | Preset circuits |
| "toggle the switch" / "measure the circuit" / "undo" / "delete selected" | Operate and edit |

Mouse: drag rotates molecules. In circuits, click a dot then another dot to wire, click a switch/Arduino to toggle, right-click cancels.

### Aurora Plus
| Say | Action |
|---|---|
| "cowork build a prime number checker" | Sandboxed coding agent builds, tests, and zips it |
| "cowork status" / "cowork stop" | Check or cancel Cowork |
| "start network hub" / "stop network hub" / "hub address" | Control Aurora from a phone on your LAN |
| "offline mode on" / "offline mode off" / "offline mode status" | Force or check offline mode |
| "add folder C:/Users/you/notes to my vault" / "index my files" | Add and index folders for search |
| "find files about budget" / "what did I write about arduino" | Semantic file search |

### Productivity
Anything that changes data asks for a "yes" first.

| Say | Action |
|---|---|
| "remember this: pick up the parcel" | Save a quick note |
| "search notes for parcel" / "read note 2" / "delete note 2" / "delete all notes" | Search, read, delete notes |
| "teach a skill called warmup: what time is it then open chrome" | Create a voice macro |
| "run skill warmup" / "list my skills" / "delete skill warmup" | Use and manage skills |
| "what do I have tomorrow" / "what's next on my calendar" | Read calendar |
| "add lunch with Sam tomorrow at noon to my calendar" | Add local event (confirmed) |
| "read my email" / "summarize email one" | Unread mail and summaries |
| "draft an email to Ann about the demo" / "draft a reply to the latest email saying ..." | Write drafts |
| "read my draft" / "discard the draft" / "send the email" | Review, discard, send (confirmed) |
| "show my notifications" / "clear notifications" | Phone, email, and calendar alerts |
| "rename report.pdf in downloads to summary.pdf" | Rename (confirmed) |
| "move summary.pdf in downloads to documents" | Move (confirmed) |
| "zip todo.txt in documents" / "extract archive.zip" | Compress / extract |
| "organize my downloads" | Sort loose files into folders (confirmed) |
| "make 8 flashcards about the water cycle" | AI-generated deck |
| "quiz me on capitals" / "review flashcards" | Quiz or review; then "flip", "next", "the answer is ..." |
| "start a pomodoro" / "study timer for 30 minutes" / "how long have I studied" | Focus timers and log |
| "translate good morning to Spanish" / "how do you say thank you in Japanese" | Translate and speak |

### Modes & monitoring
| Say | Action |
|---|---|
| "show my project timeline" / "... today" / "... this week" | Git, file, and Aurora activity timeline |
| "older" / "newer" / "close timeline" | Page through / close |
| "presentation mode" / "start presentation called robotics" | Slides from Markdown |
| "next slide" / "previous slide" / "slide 3" / "first slide" / "last slide" | Navigate |
| "show speaker notes" / "read the notes" / "hide speaker notes" | Notes |
| "timer for 10 minutes" / "how much time is left" | Presentation timer |
| "blank screen" / "unblank" / "run the demo" / "end presentation" | Screen and demo control |
| "emergency monitor on" / "off" / "status" | System watcher |
| "what happened" / "system alerts" / "explain the last alert" | Alert history and explanation |
| "draw mode" / "stop drawing" | Gesture drawing |
| "move system panel behind me" / "bring notes panel forward" | Spatial panels |
| "show my desktop" / "reset layout" | Panel summary / reset |

### Maker tools
| Say | Action |
|---|---|
| "connect to my arduino" / "show my Mars station telemetry" / "close telemetry" | Live Arduino/ESP32 data |
| "game companion" / "start recording" / "stop recording" | Game stats overlay, screen recording |
| "start experiment called volcano" / "stop experiment" | Experiment recorder |
| "log observation: ..." / "take an experiment screenshot" | Add to the log |
| "generate my experiment report" | Compile `report.txt` |
| "write python code called sorter that sorts a list" | AI writes code, opens it in VS Code |
| "edit sorter to add a reverse option" | AI edits the file (with backup) |
| "new arduino sketch blinky" / "new python script called demo" | Scaffold files |
| "open sorter in VS Code" / "open blinky in arduino" | Open in editor |
| "test in sandbox: print the first 10 primes" | Run AI-written code in the sandbox |

### General
| Say | Action |
|---|---|
| "remember my face as Sam" / "forget Sam's face" / "who do you know" | Face enrollment |
| "what do you see" / "read this QR code" | Webcam description / code reading |
| "what's the weather right now?" / "weather in Tokyo" / "close the weather" | Weather panel |
| "what time is it?" / "what's 47 times 12" / "15 percent of 200" | Time and math |
| "set a timer for 5 minutes" / "how much time is left" / "cancel timer" | Timers |
| "open notepad" | Also: calc, spotify, chrome, explorer... |
| "volume up/down" / "volume to 50" / "mute" | Volume |
| "play music" / "pause" / "next song" / "previous song" | Media keys |
| "take a screenshot" / "system status" / "lock my computer" | PC utilities |
| "take a note: ..." / "read my notes" | Notes |
| "call mom" / "call me" / "call mom on WhatsApp" | Phone calls (ADB) |
| "open instagram on my phone" / "turn off wifi on my phone" | Phone control (ADB) |
| "repeat that" / "stop" | Repeat reply / interrupt speech |
| "hello" | General chat, shown as a response card |

### Gmail, WhatsApp & files
| Say | Action |
|---|---|
| "connect gmail" / "connect gmail with sending" / "disconnect gmail" | Google sign-in in your browser (read-only, or read + send) |
| "reply on WhatsApp saying I'm on my way" / "reply to the latest WhatsApp from Mom" | Reply to a WhatsApp notification (AI-written unless you say "saying ...", confirmed) |
| "send Mom a WhatsApp saying I'll be late" / "send Sam a WhatsApp about the demo" | New WhatsApp message (confirmed, needs a number in `contacts.json` or your phone contacts) |
| "find PDFs about physics from this month" / "find images in downloads from last week" | Search by type, topic, date and folder |
| "rename these files properly" / "move these into my school folder" | Batch rename or move the last search results (confirmed) |
| "undo the move" / "undo the rename" | Reverse the last batch (confirmed) |

### Screen, camera & calculator
| Say | Action |
|---|---|
| "why isn't this code working" / "what's on my screen" / "what does this error mean" | Aurora reads your screen and explains it |
| "solve this problem" / "read this equation" / "visual calculator" | Read a math problem from the camera and solve it |
| "solve the equation on my screen" | Same, from a screenshot |

### Creation
| Say | Action |
|---|---|
| "make a 5 slide presentation about black holes" | Designed PowerPoint in `documents/`: themed layouts, facts and figures, speaker notes, pictures |
| "make a presentation about volcanoes in the sunset theme" | Themes: aurora, ocean, sunset, forest, minimal, paper |
| "make a presentation about Rome without images" | Text-only deck (images are AI-generated online, drawn locally offline) |
| "create a document about climate change" / "write a report on ..." | Word file |
| "build a spreadsheet for a monthly budget" / "10 row spreadsheet for ..." | Excel file |
| "generate an image of a neon city at night" | AI image in `generated_images/` ("wallpaper" = wide, "portrait" = tall) |

Offline, documents are created as a blank outline instead of AI-written content.

### Interface, workspaces & dry runs
| Say | Action |
|---|---|
| "switch to coding / studying / gaming / standard mode" | Change the dashboard layout |
| "create a workspace for my Mars project" / "open my Mars workspace" / "list my workspaces" | Project workspaces |
| "show notes" (also files, data, links, experiments, tasks, chat) | Switch workspace tab |
| "add note: ..." / "add task: ..." / "complete task 2" / "add link: nasa.gov" / "add file C:/data/run1.csv" | Fill the workspace |
| "what was the temperature in my last experiment" / "search project for batteries" | Ask your project |
| "close workspace" | Close it |
| "simulate organizing my downloads" / "preview moving these into my school folder" / "dry run zipping todo.txt in documents" | Preview without touching anything |
| "simulate deleting all notes" / "simulate volume to 90" / "simulate sending the email" | Preview data and system actions |
| "go ahead" | Run the last simulated command for real (it still asks for "yes") |
| "write and debug python code called calc that adds two numbers" / "debug sorter" | AI writes, sandbox runs, AI fixes from the error |

## Presentation decks

Create `presentations/<name>.md`; a built-in sample deck is used otherwise.

```
# Slide title
- bullet
Notes: speaker notes
Demo: show me a carbon atom        <- any voice command, runs when the slide appears
---                                <- next slide
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
| Point (draw mode) | Draw a circle, square, triangle, line, wave or spiral in the air |
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

## Network Hub

Say "start network hub", then open the address shown on screen from a phone
on the same wifi. It is LAN-only, token-protected (`hub_token.txt`), rate
limited, and blocks sensitive commands (unlock, lock, delete, forget).
Keep the link private.

## Cowork safety

Cowork is layered, best-effort protection — **not a VM**: fresh temp folder,
flat layout, stdlib-only Python, secret/destructive-intent scan, static code
scan, a fixed `unittest` command as the only thing it can run, and every
step validated by a safeguard model that fails closed. Output is a ZIP in
`cowork_exports/` for you to review.

## Gmail setup

Aurora signs in with Google OAuth 2.0 (PKCE), so your Google password never
reaches Aurora. The token is stored locally in `gmail_token.json`.

1. In Google Cloud Console create a project and enable the Gmail API.
2. Configure the OAuth consent screen, then create an **OAuth client ID** of
   type **Desktop app** and download the JSON as `google_client.json` (next to
   `api_key.txt`).
3. Say **"Aurora, connect gmail"** (read unread mail only) or
   **"connect gmail with sending"**. Approve access in the browser tab.
4. Say **"disconnect gmail"** any time to revoke the token and delete it.

Drafts stay local in `email_drafts.json`, and nothing is sent without your "yes".

## Self-test

```
python test_aurora.py              logic + routing tests (no camera/mic/API needed)
python test_aurora.py --hardware   also: camera, mic, ADB, hand tracker
python test_aurora.py --gui        also: render every hologram mode
python test_aurora.py --live       also: real Groq, TTS, weather
python test_aurora.py --all
```

## Troubleshooting

- **Webcam doesn't open / black debug window** — another app (Zoom,
  Teams, a browser tab) may be holding the camera. Close it and rerun.
- **Low frame rate / laggy tracking** — lower the resolution in
  `jarvis_ui/hand_tracker.py`, or close other heavy background apps.
- **Hand not detected reliably** — use decent lighting (dim rooms are
  auto-enhanced) and keep your hand roughly centered in frame.
- **`ImportError` on mediapipe/OpenGL** — confirm the virtual
  environment is activated before `pip install`.
- **Face recognition disabled** — usually an OpenCV install conflict.
  Run: `pip uninstall opencv-python opencv-python-headless
  opencv-contrib-python -y && pip install opencv-contrib-python`. Say
  "Aurora, check face recognition" for a live diagnosis. If someone isn't
  recognized, raise `face_threshold.txt` slightly (distances are logged).
- **Voice never picks up audio** — create `mic_index.txt` with the
  device number shown in the log at startup, or `energy_threshold.txt`.
- **Groq free tier limit** — rate-limited (not a token/dollar cap),
  roughly 30 requests/minute with a per-day cap on the search-capable
  model. Resets the next day. Aurora falls back to Offline Mode when
  the API or internet is unavailable.
- **Phone features say "can't see your phone"** — enable USB debugging,
  authorise the PC, or say "connect to my phone over wifi".
- **Temperature/crash alerts missing** — CPU temperature may need admin
  rights; crash detection reads the Windows Application event log.
- **Screen recording fails** — install `ffmpeg` and add it to PATH.
- **Gmail says "isn't set up" or "isn't connected"** — put the Desktop-app
  OAuth file next to `api_key.txt` as `google_client.json`, then say
  "connect gmail". If sign-in expired, connect again.
- **Math reading offline fails** — install `pytesseract` and the Tesseract
  program; online, the vision model reads the problem instead.
- **Document creation says "Missing library"** — run
  `pip install python-pptx python-docx openpyxl`.
- **Image generation fails with a 429** — the free service allows about one
  image per 15 seconds. Wait a moment and try again.
- **"Generate an image" isn't recognised** — run
  `python jarvis_ui/image_generator.py --install` once.
- **WhatsApp reply can't find the contact** — add them to `contacts.json`
  with the country code, or save them in your phone contacts.

## Project structure

```
main.py                     entry point, main loop
test_aurora.py              self-test suite
jarvis_ui/
  hologram.py                OpenGL dashboard + all visual models
  hand_tracker.py             MediaPipe gesture recognition
  voice_assistant.py          wake word, STT, local commands, TTS
  brain.py                    Groq model, tools, code generation, vision
  face_id.py                  consent-based face enrollment/recognition
  system_control.py           volume, media, apps, phone (ADB), code files, QR, notes, vault
  telemetry.py                Arduino/IoT reader, game companion, experiment recorder
  sandbox_labs.py             physics, molecule builder, circuit simulator
  addons.py                   gesture drawing, spatial panels, heart model, plots, code sandbox
  aurora_plus.py              Cowork agent, network hub, offline mode, semantic file search
  aurora_utilities.py         skills, calendar, email, notifications, files, study, translation
  dashboard_modes.py          project timeline, presentation mode
  system_monitor.py           emergency alerts for disk, CPU, temperature, network, crashes
  gmail_client.py             Gmail OAuth 2.0 sign-in, read unread mail, send
  adaptive_interface.py       coding / studying / gaming dashboard layouts
  project_workspace.py        per-project notes, tasks, links, files, data, experiments, chat
  operation_simulator.py      Simulation Room: dry-run previews of disruptive commands
  visual_calculator.py        read and solve math from the camera or screen
  document_generator.py       Word and Excel creation, routes presentations to the builder below
  presentation_builder.py     designed PowerPoint decks: themes, layouts, images, speaker notes
  image_generator.py          AI image creation
requirements.txt
api_key.txt                  your Groq key (create from .example)
```
