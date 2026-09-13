# Aurora functional validation

## Automated checks

Run from the project root:

```sh
python -m unittest discover -s tests -v
python -m compileall -q main.py jarvis_ui tests tools
```

The suite exercises real Python command, layout, and model logic. Camera,
OpenGL entry points, speech, and external actions are replaced with test doubles;
passing these checks does **not** verify a GPU driver or a working microphone.

| Area | Checked without hardware |
| --- | --- |
| Elements | All 118 names, proton/electron counts, shell totals, selection resets |
| Scene rendering | Core, all supported shapes, atom, solar/known/generated systems, answer overlays (GL calls stubbed) |
| Editing | Numeric quantities above ten, new-element rebuild, bounded counts, actual applied changes |
| Voice commands | Scene changes, theme, guide, diagnostics, motion preference, weather routing |
| Timers | Number words, duration limits, cancellation, independent same-deadline timers |
| Concurrency | Display calls run on UI thread; timed-out queued commands do not run later |
| Speech notices | Greeting/timer notifications queue rather than block rendering |
| Weather | Structured fixtures, city parsing, missing location, invalid values, network/rate-limit failures |
| Overlays | Layout and drawing logic at 640×480, 800×600, 1000×680, 1200×800, 1920×1080 |
| Constellation UI | Space background, subsystem graph labels, top-bar tabs/briefing/stats, command pill, telemetry readout (GL stubbed) |
| Answer navigation | Every page is reachable, final page preserved, scroll bounds |
| Calculator | Basic math, percentage, zero division, exponent/size limits |
| Device fallbacks | Media failure feedback, authorized/offline/multiple ADB device parsing |
| Startup | Dependency-free help, stable paths, optional-voice fallback, MSAA retry logic |

## Target-machine smoke test

Use a terminal so any traceback stays visible. Do not test phone calls, PIN entry,
or app launches unless you intend the corresponding action on your own device.

1. **Graphics only:** `python main.py --no-camera --no-voice`.
   The nebula core should animate and show voice offline. F11 must leave the
   window unchanged in default/windowed mode. Close
   the window; the process should exit without a second cleanup exception.
2. **Voice:** `python main.py --no-camera`. Say “Aurora” alone, then “show me a
   carbon atom” during the listening window. Or say the entire request together.
3. **Editing:** “Aurora, add 12 electrons”, “Aurora, select orbit two”, then
   “Aurora, start a new element”. Expect a hydrogen-like one-electron display.
4. **Scene changes:** Ask for a sphere, solar system, then “show core”. There
   should be no stale answer card or selected orbit from the preceding scene.
5. **Overlays:** Ask a general question, say “read more” and “scroll up”, then
   “close answer”. Ask “show help” and “close help”. These require no mouse clicks.
6. **Weather:** “Aurora, weather in Ghaziabad”. Check the resolved city, units,
   provider time and timezone. Disconnect internet and retry: expect an error
   card, not an old temperature shown as a new success. Reconnect and retry.
7. **Cancellation:** “Aurora, set a timer for ten seconds”, then “Aurora, cancel
   the timer”. It must not announce after the original deadline. Repeat without
   cancelling and check for one announcement.
8. **Camera:** Launch normally, enable `--debug-camera` if needed, and check
   gestures while voice replies. Greeting speech should not freeze the UI.
9. **Stop / quit:** Say “Aurora, stop” during Edge TTS. Close the app while a
   request is in progress; bounded network work may finish on daemon threads,
   but queued display commands must not run against a closed window.

If a check fails, retain the terminal traceback and `voice_debug.log`. Redact
private transcripts/device information before sharing logs. Do not share keys,
PIN files, contacts, or face data.

## Visual previews

`core-motion.gif`, `core-preview.png`, `overlays-preview.png`,
`dashboard-preview.png`, and `dashboard-docked-preview.png` are software design
previews, **not captured OpenGL windows**. Overlay weather values are
explicitly fixture/sample data. The overlay preview invokes the real overlay
layout/text methods through Pillow drawing adapters; the dashboard preview
executes the real HUD draw methods through a small software GL rasterizer
(only the GPU rasterizer is replaced); glow and weather-icon rendering are
approximations.

```sh
pip install Pillow  # optional development-only dependency
python tools/render_core_preview.py --motion
python tools/render_overlays_preview.py
python tools/render_dashboard_preview.py
```

Live speech services, live weather requests, device operations, and real OpenGL
rasterization remain target-machine checks. No blanket claim that every hardware
integration works is made by these automated results.


## Particle-flow build (`amber-flow-v4`)

Added checks cover three-dimensional positions, stream heads matching their
trails, nonzero depth range, outward-moving light packets, bounded vertex arrays,
and seven particle/trail batch submissions. These validate the actual generated
geometry and renderer call structure, not GPU performance. The new vertex-array
renderer requires the same compatibility OpenGL context used by the existing app.

On your laptop, check that small bright heads travel around both sides of the
sphere, trails fade behind them, and short rays travel outward. Test “reduce
motion” and “resume animation”, then show weather and return to the core. Capture
a short screen recording if it still looks static or runs slowly; include the
`python main.py --diagnose` output. Static preview PNGs cannot establish motion.


The v4 tests additionally cover animation-clock frame-rate independence, eased
voice energy, pause/resume pose continuity, quality-specific particle counts,
shared positions across quality switches, and voice quality commands. Test
performance and cinematic modes on the target machine; the CPU/GPU frame rate
is not established by mocked graphics tests or the software preview.
