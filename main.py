"""
Aurora - voice + gesture holographic dashboard.  python main.py
F11 fullscreen | ESC or 'q' (debug window) quits.
Say "Aurora ..." (see README / jarvis_ui/voice_assistant.py for the full command list).
Gestures: move hand = rotate, twist = roll, fist+pull = zoom, point = select orbit, pinch = reshape/pan,
peace = snapshot, thumbs down = reset, OK = play/pause, rock sign + up/down = volume,
swipe = theme/brightness, two hands = zoom/roll.
"""
import os
import sys
import time

import cv2

from jarvis_ui.hologram import Hologram
from jarvis_ui.voice_assistant import VoiceAssistant, NullVoice
from jarvis_ui.face_id import FaceID
from jarvis_ui import system_control, addons, dashboard_modes, system_monitor

GESTURE_TO_STATE = {"open_palm": "listening", "fist": "speaking"}
SNAPSHOT_DIR = "snapshots"
DETECT_EVERY = 3            # run face detection every Nth frame (big FPS win, recognition is 2 Hz anyway)
RECOGNITION_INTERVAL = 0.5
FORGET_AFTER = 20.0         # seconds without a match before the same person is greeted again


def save_snapshot(frame):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"jarvis_{int(time.time())}.png")
    cv2.imwrite(path, frame)
    print(f"Snapshot saved: {path}")


def build_voice(hologram, face_id):
    try:
        voice = VoiceAssistant(hologram, face_id, on_log=hologram.log_event)
    except Exception:
        import traceback
        print("VoiceAssistant setup failed, continuing without voice:", flush=True)
        traceback.print_exc()
        return NullVoice()
    try:
        from jarvis_ui import aurora_plus       # cowork, network hub, offline mode, file search
        aurora_plus.install(voice, hologram)
    except Exception:
        import traceback
        print("Aurora Plus unavailable, continuing without it:", flush=True)
        traceback.print_exc()
    voice.start()
    return voice


def main():
    tracker = addons.TipHandTracker(camera_index=0)
    hologram = Hologram()
    face_id = FaceID(on_log=hologram.log_event)
    voice = build_voice(hologram, face_id)
    drawer = addons.GestureDrawer(hologram, speak=voice.speak_now, on_log=hologram.log_event)
    hologram.drawer, voice.drawer = drawer, drawer
    addons.install(hologram, drawer)
    dashboard_modes.install(hologram, voice)
    system_monitor.start(hologram, voice)

    last_debug_frame, prev_time, frame_i = None, time.time(), 0
    volume_prev_pitch = None
    faces, last_check, last_seen, last_dbg, recognized = [], 0.0, 0.0, 0.0, None

    try:
        while True:
            now = time.time()
            dt = max(1e-4, now - prev_time)
            prev_time = now

            hologram.process_events()
            if hologram.should_quit:
                break

            result, debug_frame = tracker.read()
            drawer.update(result.hands[0] if result.detected else None)

            if debug_frame is not None:
                last_debug_frame = debug_frame
                raw = getattr(tracker, "raw_frame", None)
                voice.latest_frame = raw if raw is not None else debug_frame
                frame_i += 1
                gray = cv2.cvtColor(debug_frame, cv2.COLOR_BGR2GRAY)
                if frame_i % DETECT_EVERY == 0 or face_id.enrolling:
                    faces = face_id.detect_faces(gray)

                if hologram.mode == "telemetry":
                    reader = getattr(voice, "active_reader", voice.telemetry)
                    hologram.update_telemetry(reader.latest, reader.connected and not reader.is_stale())
                voice.game_session.set_fps(hologram._fps)
                if voice.experiment.active:
                    voice.experiment.maybe_log_sensor(voice.telemetry.latest)

                # ---- face enrollment (explicit request only) ----
                if voice.pending_enrollment_name and not face_id.enrolling:
                    name, voice.pending_enrollment_name = voice.pending_enrollment_name, None
                    if not face_id.start_enrollment(name):
                        hologram.log_event("FACE: enrollment unavailable")
                        voice.speak_now("Face recognition isn't available on this system. Say 'check face recognition' for details.")
                if face_id.enrolling:
                    res = face_id.feed_enrollment(gray, faces)
                    if res:
                        ok, name, msg = res
                        hologram.log_event(f"FACE: enrollment {'ok' if ok else 'failed'}")
                        voice.speak_now(f"Got it, I'll recognize you as {name} from now on." if ok else msg)
                # ---- recognition (throttled) ----
                elif faces and now - last_check > RECOGNITION_INTERVAL:
                    last_check = now
                    name, dist = face_id.recognize(gray, faces[0])
                    if name:
                        last_seen = now
                        if name != recognized:
                            recognized = name
                            idx = face_id.get_theme_index(name)
                            if idx is not None:
                                hologram.set_theme_index(idx)
                            hologram.log_event(f"FACE: recognized {name}")
                            voice.speak_now(f"Welcome back, {name}.")
                    elif dist is not None and face_id.people and now - last_dbg > 5:
                        last_dbg = now
                        hologram.log_event(f"FACE: no match (distance {dist:.0f}, limit {face_id.threshold:.0f})")
                if recognized and now - last_seen > FORGET_AFTER:
                    recognized = None

            if result.detected:
                p = result.hands[0]
                if drawer.active:       # hold rotation steady while drawing
                    hologram.update(hologram.rotation_y / 90, hologram.rotation_x / 60, 0.0, target_dt=dt)
                else:
                    hologram.update(p.yaw_norm, p.pitch_norm, p.pinch_amount, target_dt=dt)
                gesture_state, pinch_pulse = GESTURE_TO_STATE.get(p.gesture, "idle"), p.pinch_amount

                if p.gesture == "pinch":
                    if hologram.selected_index is not None:
                        hologram.edit_selected_orbit(p.pitch_norm, p.yaw_norm, dt)
                    elif result.pan_delta != (0.0, 0.0):
                        hologram.apply_pan_delta(*result.pan_delta)

                if p.gesture == "rock_sign":       # hand up = louder
                    if volume_prev_pitch is not None:
                        delta = volume_prev_pitch - p.pitch_norm
                        if abs(delta) > 0.003:
                            v = system_control.adjust_volume_percent(round(delta * 150))
                            if v is not None:
                                hologram.log_event(f"VOLUME: {v}%")
                    volume_prev_pitch = p.pitch_norm
                else:
                    volume_prev_pitch = None
            else:
                hologram.update(0, 0, 0.0, target_dt=dt)
                gesture_state, pinch_pulse, volume_prev_pitch = "idle", 0.0, None

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

            for _, gesture in result.events:
                if gesture == "point" and not drawer.active:
                    hologram.select_next_orbit()
                    hologram.trigger_flash("point", 0.6)
                    hologram.log_event(f"SELECT -> {'orbit %d' % (hologram.selected_index + 1) if hologram.selected_index is not None else 'none'}")
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

            sel = (f"Editing orbit {hologram.selected_index + 1}/{len(hologram.orbits)}"
                   if hologram.selected_index is not None else "No orbit selected (point to select)")
            hud = [f"Zoom {hologram.zoom:.2f}  FPS {hologram._fps:.0f}", sel, f"Voice: {'ready' if voice.enabled else 'off'}"]
            hud += [f"H{i + 1} ({h.handedness[:1]}): {h.gesture} x{h.finger_count}" for i, h in enumerate(result.hands)]
            hologram.set_hud_lines(hud)

            hologram.render(pinch_amount=pinch_pulse)
            if debug_frame is not None:
                cv2.imshow("Aurora - hand tracking debug", debug_frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
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
