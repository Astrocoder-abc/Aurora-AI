# Jarvis Project — Laptop-Only Build (Phase 1: Hologram UI)

No external hardware needed — everything runs on your Inspiron 5567 using
its built-in webcam. Your hand's position rotates an on-screen wireframe
"hologram" HUD, pinching makes it glow/pulse, and later phases will hook
voice + the LLM brain into the same UI.

## 1. Install Python dependencies

Open a terminal in this folder and run:

```
python -m venv venv
```

Activate it:
- Windows: `venv\Scripts\activate`
- Mac/Linux: `source venv/bin/activate`

Then install requirements:
```
pip install -r requirements.txt
```

> Note: mediapipe is CPU-friendly and does not need a dedicated GPU — the
> Inspiron 5567's integrated graphics (or the optional Radeon R7 M445 on
> some configs) is more than enough for this.

## 2. Run it

```
python main.py
```

Two windows should open:
1. **Hologram window** — the rotating wireframe HUD
2. **Debug window** — your webcam feed with hand landmarks drawn on it,
   so you can confirm tracking is working

## 3. Try it

- Move your hand left/right and up/down in front of the webcam — the
  hologram should rotate to follow.
- Pinch your thumb and index finger together — the hologram glows
  brighter and pulses faster.
- Open your palm fully — hologram turns cyan ("listening" demo state).
- Make a fist — hologram turns green ("speaking" demo state).
- Press `q` in the debug window to quit.

## Troubleshooting

- **Webcam doesn't open / black debug window:** another app (Zoom, Teams,
  browser tab) may be holding the camera. Close those and rerun.
- **Low frame rate / laggy tracking:** lower the resolution further in
  `hand_tracker.py` (e.g. 480x360), or make sure no other heavy apps are
  running in the background.
- **Hand not detected reliably:** make sure there's decent lighting facing
  your hand, and keep your hand roughly centered in frame while testing.
- **`ImportError` on mediapipe/OpenGL:** double check you activated the
  virtual environment before running `pip install`.

## What's next

This phase proves the gesture-to-visual pipeline works end-to-end. Next
phases will add, without touching this hologram code much:
- Wake word detection + speech-to-text (mic input)
- The LLM brain (Claude API or local model) for actual conversation/tasks
- Text-to-speech output
- Wiring `hologram.set_state(...)` to real conversation events instead of
  the demo palm/fist mapping used here

## Voice AI setup (talk to it, it can search the web and reply out loud) — FREE

Uses Groq's API, which is genuinely free — no credit card required, no
trial period that expires.

1. **Get a free Groq API key**: https://console.groq.com/keys — sign in
   with email or Google, click "Create API Key". No billing info needed.
2. **Install the new dependencies**:
   ```
   python -m pip install --user -r requirements.txt
   ```
   `pyaudio` occasionally fails to install on Windows because it needs a
   prebuilt wheel for your exact Python version. If `pip install pyaudio`
   errors out, try:
   ```
   python -m pip install --user pipwin
   python -m pipwin install pyaudio
   ```
3. **Add your API key**: rename `api_key.txt.example` to `api_key.txt`
   and replace its contents with just your key (no quotes, no extra
   text). Keep this file private — don't share it or commit it anywhere.
4. **Run it**: `python main.py` as usual. If the key and mic are both
   set up correctly, you'll see `VOICE: listening for wake word 'Jarvis'`
   printed and logged to the dashboard's event log panel.
5. **Talk to it**: say "Jarvis" followed by your question, e.g.
   - "Jarvis, what's the weather in Tokyo right now?" (the model has
     built-in web search — groq/compound — and decides on its own when
     to look something up vs. answer from what it already knows)
   - "Jarvis, what time is it?" (answered instantly, no API call)
   - "Jarvis, open youtube" (actually opens it in your browser)
   - "Jarvis, who won the last F1 race?" (web search again)

While it's listening or replying, the hologram's state (and color) will
switch to reflect that — same visual language as the gesture-driven
open-palm/fist states, just driven by voice instead.

**Free tier limits worth knowing**: Groq's free tier is rate-limited
(not a token/dollar cap) — currently around 30 requests/minute and a
per-day cap on the compound model specifically (in the low hundreds of
requests/day, since it's the most capable option). For a personal voice
assistant that's more than enough; if you ever hit the daily cap you'll
see the error message spoken back to you, and it resets the next day.

**If voice doesn't activate:** the dashboard's event log will tell you
why — most commonly a missing/invalid API key, a missing microphone, or
a `pyaudio` install failure. The rest of the app (gestures, hologram)
keeps working fine even if voice can't start.

