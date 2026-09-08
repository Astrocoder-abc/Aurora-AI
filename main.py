"""
Aurora Project — Editable Holographic Element Display

Launches FULLSCREEN by default. Press F11 to toggle windowed mode, ESC to
quit (or close the debug window / press 'q' there).

Two windows open:
  1. The hologram dashboard — nucleus with orbiting "element" nodes, a
     glowing projector base plate, scanline flicker, corner-bracket HUD
     frame, and system/event-log side panels.
  2. Webcam debug view with hand landmarks + live gesture/finger labels.

VOICE (say "Aurora" + your request):
  "Aurora, show me a carbon atom"      -> real Bohr-model diagram
  "Aurora, add a proton"               -> changes the element, keeps it neutral
  "Aurora, remove 2 electrons"         -> turns it into an ion
  "Aurora, add 3 neutrons"             -> changes the isotope
  "Aurora, start a new element"        -> resets to 1 proton, build from there
  "Aurora, show me the solar system"   -> sun + 8 planets
  "Aurora, show me a sphere"           -> wireframe globe (also: cube,
                                          torus, pyramid, cylinder)
  "Aurora, show the Eiffel Tower"      -> tapering lattice tower model
  "Aurora, show a skyscraper"          -> generic tower model (also
                                          matches Burj Khalifa, Empire
                                          State Building, etc.)
  "Aurora, show me a double helix"     -> DNA strand model
  "Aurora, select orbit one"           -> selects that orbit, confirms aloud
  "Aurora, deselect orbit"             -> clears the selection
  "Aurora, reset the display"          -> back to the demo hologram
  "Aurora, what's the weather right now?" -> docks the hologram left,
                                             shows an animated weather
                                             panel (icon, temp, humidity,
                                             wind), and speaks a summary
  "Aurora, weather in Tokyo"           -> same, for any location
  "Aurora, close the weather"          -> undocks, hologram returns to center
  "Aurora, what time is it?"           -> instant, no API call
  "Aurora, hello"                      -> just chats, but still shows a
                                          response card with what you
                                          asked and the answer, not just
                                          speech (any general question
                                          gets this — specific categories
                                          like atoms/shapes/weather still
                                          get their own dedicated visual)
  "Aurora, remember my face as Sam"    -> enrolls your face (look at the
                                          camera, hold still ~1 second) —
                                          only people who explicitly
                                          enroll get recognized, ever
  "Aurora, forget Sam's face"          -> removes that enrollment
  "Aurora, set a timer for 5 minutes"  -> counts down, speaks when done
  "Aurora, how much time is left"      -> checks active timers
  "Aurora, what's 47 times 12"         -> instant local calculation
  "Aurora, 15 percent of 200"          -> instant local calculation
  "Aurora, open notepad"               -> launches the app (also: calc,
                                          spotify, chrome, explorer, etc.)
  "Aurora, volume up" / "volume down" / "volume to 50" / "mute"
  "Aurora, play music" / "pause" / "next song" / "previous song"
  "Aurora, call mom"                   -> requires phone connected via
                                          ADB and contacts.json set up
  "Aurora, unlock my phone"            -> requires ADB + phone_pin.txt
                                          (see phone_control.py for setup)
  "Aurora, stop"                        -> interrupts it mid-sentence

GESTURE GUIDE
--------------
One hand:
  Move hand                -> rotate the hologram (yaw/pitch)
  Wrist twist               -> roll the hologram
  Open palm                 -> "listening" glow (cyan)
  Fist (held)                -> "speaking" glow (green)
  Fist + move closer/away    -> grab-and-pull zoom
  Fist -> fling open fast     -> "throw" flash
  Point                       -> select the next orbit/shell to edit
  Pinch (orbit selected) + move -> reshape it: vertical = radius,
                                    horizontal = spin speed
  Pinch (nothing selected) + move -> pan/drag the whole hologram instead
  Peace sign                  -> saves a snapshot to snapshots/
  Thumbs up                   -> green confirm flash
  Thumbs down                  -> red flash AND resets display to demo
  OK sign                      -> cyan flash + media play/pause
  Rock sign (horns), held + move up/down -> system volume
  Swipe left/right (open palm) -> cycle color theme
  Swipe up/down (open palm)     -> adjust brightness

Two hands:
  Spread apart / bring together -> zoom in/out
  Both hands rotate together     -> roll the hologram
"""

import os
import sys
import time

import cv2

from jarvis_ui.hand_tracker import HandTracker
from jarvis_ui.hologram import Hologram
from jarvis_ui.voice_assistant import VoiceAssistant
from jarvis_ui.face_id import FaceID
from jarvis_ui import system_control

GESTURE_TO_STATE = {
    "open_palm": "listening",
    "fist": "speaking",
}

SNAPSHOT_DIR = "snapshots"


def save_snapshot(frame):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    filename = os.path.join(SNAPSHOT_DIR, f"jarvis_{int(time.time())}.png")
    cv2.imwrite(filename, frame)
    print(f"Snapshot saved: {filename}")


def main():
    tracker = HandTracker(camera_index=0)
    hologram = Hologram()
    face_id = FaceID(on_log=hologram.log_event)

    print("DEBUG: about to construct VoiceAssistant...", flush=True)
    try:
        voice = VoiceAssistant(hologram, face_id, on_log=hologram.log_event)
        print(f"DEBUG: VoiceAssistant constructed OK, client={voice.client is not None}, "
              f"mic={voice.microphone is not None}", flush=True)
        voice.start()
        print(f"DEBUG: voice.start() returned, enabled={voice.enabled}", flush=True)
    except Exception as e:
        import traceback
        print("DEBUG: VoiceAssistant setup raised an exception:", flush=True)
        traceback.print_exc()
        voice = VoiceAssistant.__new__(VoiceAssistant)
        voice.state = "idle"
        voice.enabled = False
        voice.pending_enrollment_name = None

    print(__doc__)

    last_debug_frame = None
    prev_time = time.time()
    volume_drag_prev_pitch = None

    enrollment_samples = []
    ENROLLMENT_TARGET_SAMPLES = 15

    last_recognition_check = 0.0
    RECOGNITION_INTERVAL = 1.0  # seconds between recognition attempts - no need to run every frame
    recognized_person = None
    person_absent_frames = 0

    try:
        while True:
            now = time.time()
            dt = max(1e-4, now - prev_time)
            prev_time = now

            hologram.process_events()
            if hologram.should_quit:
                break

            result, debug_frame = tracker.read()
            if debug_frame is not None:
                last_debug_frame = debug_frame

                gray = cv2.cvtColor(debug_frame, cv2.COLOR_BGR2GRAY)
                faces = face_id.detect_faces(gray)

                # ---- enrollment: collect samples while a request is pending ----
                if voice.pending_enrollment_name and len(faces) == 1:
                    x, y, w, h = faces[0]
                    enrollment_samples.append(gray[y:y + h, x:x + w])
                    if len(enrollment_samples) >= ENROLLMENT_TARGET_SAMPLES:
                        name = voice.pending_enrollment_name
                        face_id.enroll(enrollment_samples, name)
                        enrollment_samples = []
                        voice.pending_enrollment_name = None
                        voice.speak_now(f"Got it, I'll recognize you as {name} from now on.")

                # ---- recognition: throttled, greets + personalizes on a new match ----
                elif not voice.pending_enrollment_name and len(faces) >= 1 and \
                        time.time() - last_recognition_check > RECOGNITION_INTERVAL:
                    last_recognition_check = time.time()
                    name, confidence = face_id.recognize(gray, faces[0])
                    if name:
                        person_absent_frames = 0
                        if name != recognized_person:
                            recognized_person = name
                            theme_idx = face_id.get_theme_index(name)
                            if theme_idx is not None:
                                hologram.set_theme_index(theme_idx)
                            hologram.log_event(f"FACE: recognized {name}")
                            voice.speak_now(f"Welcome back, {name}.")
                    elif recognized_person:
                        person_absent_frames += 1
                        if person_absent_frames > 5:  # a few missed ticks before resetting, avoids flicker
                            recognized_person = None

            if result.detected:
                primary = result.hands[0]
                hologram.update(primary.yaw_norm, primary.pitch_norm, primary.pinch_amount, target_dt=dt)
                gesture_state = GESTURE_TO_STATE.get(primary.gesture, "idle")
                pinch_for_pulse = primary.pinch_amount

                if primary.gesture == "pinch":
                    if hologram.selected_index is not None:
                        hologram.edit_selected_orbit(primary.pitch_norm, primary.yaw_norm, dt)
                    elif result.pan_delta != (0.0, 0.0):
                        hologram.apply_pan_delta(*result.pan_delta)

                # Gesture volume control: hold "rock sign" (horns) and move
                # your hand up/down to adjust system volume. Chosen because
                # rock_sign has no other continuous-control use, so there's
                # no conflict with existing gestures.
                if primary.gesture == "rock_sign":
                    if volume_drag_prev_pitch is not None:
                        delta = volume_drag_prev_pitch - primary.pitch_norm  # hand up = positive = louder
                        if abs(delta) > 0.003:
                            new_vol = system_control.adjust_volume_percent(round(delta * 150))
                            if new_vol is not None:
                                hologram.log_event(f"VOLUME: {new_vol}%")
                    volume_drag_prev_pitch = primary.pitch_norm
                else:
                    volume_drag_prev_pitch = None
            else:
                hologram.update(0, 0, 0.0, target_dt=dt)
                gesture_state = "idle"
                pinch_for_pulse = 0.0
                volume_drag_prev_pitch = None

            # Voice state takes priority over gesture state when active —
            # you don't want the hologram flipping to "idle" mid-reply just
            # because your hand dropped out of frame.
            hologram.set_state(voice.state if voice.state != "idle" else gesture_state)

            if result.zoom_delta:
                hologram.apply_zoom_delta(result.zoom_delta)
            if result.roll_delta:
                hologram.apply_roll_delta(result.roll_delta)

            if result.swipe == "left":
                hologram.cycle_theme(-1)
            elif result.swipe == "right":
                hologram.cycle_theme(1)
            elif result.swipe == "up":
                hologram.adjust_brightness(0.15)
            elif result.swipe == "down":
                hologram.adjust_brightness(-0.15)

            for hand_index, gesture in result.events:
                if gesture == "point":
                    hologram.select_next_orbit()
                    hologram.trigger_flash("point", intensity=0.6)
                    label = f"orbit {hologram.selected_index+1}" if hologram.selected_index is not None else "none"
                    hologram.log_event(f"SELECT -> {label}")
                elif gesture == "peace" and last_debug_frame is not None:
                    save_snapshot(last_debug_frame)
                    hologram.trigger_flash("peace")
                    hologram.log_event("SNAPSHOT captured")
                elif gesture == "thumbs_down":
                    hologram.trigger_flash("thumbs_down")
                    hologram.reset_orbits()
                    hologram.log_event("RESET orbits")
                elif gesture == "ok_sign":
                    system_control.media_play_pause()
                    hologram.trigger_flash("ok_sign")
                    hologram.log_event("MEDIA: play/pause (gesture)")
                elif gesture in ("thumbs_up", "rock_sign", "throw"):
                    hologram.trigger_flash(gesture)
                    hologram.log_event(f"{gesture.upper()} detected")

            selection_label = (
                f"Editing orbit {hologram.selected_index + 1}/{len(hologram.orbits)}"
                if hologram.selected_index is not None else "No orbit selected (point to select)"
            )
            hud_lines = [
                f"Zoom {hologram.zoom:.2f}  FPS {hologram._fps:.0f}",
                selection_label,
                f"Voice: {'ready' if voice.enabled else 'off'}",
            ]
            for i, hand in enumerate(result.hands):
                hud_lines.append(f"H{i+1} ({hand.handedness[:1]}): {hand.gesture} x{hand.finger_count}")
            hologram.set_hud_lines(hud_lines)

            hologram.render(pinch_amount=pinch_for_pulse)

            if debug_frame is not None:
                cv2.imshow("Aurora - hand tracking debug", debug_frame)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        print("MAIN LOOP CRASHED:", flush=True)
        traceback.print_exc()
        input("Press Enter to close...")
    finally:
        voice.stop()
        tracker.close()
        hologram.close()
        cv2.destroyAllWindows()
        sys.exit(0)


if __name__ == "__main__":
    main()
