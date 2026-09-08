"""
Hand tracking wrapper around MediaPipe's HandLandmarker (Tasks API).

STABILITY FIX (v2): MediaPipe does not guarantee a hand stays at the same
list index between frames — it just returns whatever it detected this
frame, in whatever order. The previous version assumed "hands[0]" was
always the same physical hand, which caused gestures/rotation/pan to
jitter or reset whenever detection order flickered. This version:
  1. Tracks the "primary" hand by nearest-neighbor position to the
     previous frame's primary hand, not by list index.
  2. Keys gesture debouncers by handedness label ("Left"/"Right") instead
     of list index, so confirmation state survives reordering.
  3. Uses a majority-vote debouncer (N of the last M frames) instead of
     strictly consecutive frames — raw per-frame landmark jitter makes
     "5 perfectly consecutive identical frames" rare in practice, which
     is why gestures felt like they weren't registering at all.

Full gesture feature set:
  - Static poses: open_palm, fist, point, peace, thumbs_up, thumbs_down,
    ok_sign, rock_sign (horns), pinch, unknown
  - finger_count: 0-5, always available regardless of named gesture
  - Swipe detection, wrist-twist/two-hand roll, pinch-drag pan,
    two-hand zoom, and grab-and-pull zoom + throw (as before).
"""

import math
import os
import time
import urllib.request
from collections import deque, Counter

import cv2
import mediapipe as mp
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import (
    HandLandmarker,
    HandLandmarkerOptions,
    RunningMode,
)

MODEL_FILENAME = "hand_landmarker.task"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task"
)

# Gestures that fire a one-shot "enter" event (rather than being a
# continuously-held state like open_palm/fist/pinch/point).
ACTIONABLE_GESTURES = {"peace", "thumbs_up", "thumbs_down", "rock_sign", "ok_sign", "point"}

SWIPE_DISPLACEMENT_THRESHOLD = 0.35
SWIPE_CROSS_AXIS_LIMIT = 0.15
SWIPE_WINDOW_FRAMES = 10
SWIPE_COOLDOWN_SEC = 0.6

PULL_ZOOM_GAIN = 6.0
THROW_VELOCITY_THRESHOLD = 0.06


def _ensure_model(model_path):
    if os.path.exists(model_path):
        return
    print(f"Downloading hand landmark model to {model_path} ...")
    urllib.request.urlretrieve(MODEL_URL, model_path)
    print("Download complete.")


class GestureDebouncer:
    """Confirms a gesture once it wins a majority vote over a sliding window.

    Far more forgiving of normal landmark jitter than requiring N perfectly
    consecutive identical frames, which in practice almost never happens.
    """

    def __init__(self, window=6, min_votes=4):
        self.history = deque(maxlen=window)
        self.min_votes = min_votes
        self.confirmed = "unknown"

    def update(self, raw_gesture):
        self.history.append(raw_gesture)
        counts = Counter(self.history)
        best_gesture, best_count = counts.most_common(1)[0]

        changed = False
        if best_count >= self.min_votes and self.confirmed != best_gesture:
            self.confirmed = best_gesture
            changed = True
        return self.confirmed, changed


class HandInfo:
    def __init__(self):
        self.handedness = "Unknown"
        self.yaw_norm = 0.0
        self.pitch_norm = 0.0
        self.pinch_amount = 0.0
        self.gesture = "unknown"
        self.raw_gesture = "unknown"
        self.finger_count = 0
        self.roll_deg = 0.0
        self.palm_size = 0.0


class TrackerResult:
    def __init__(self):
        self.hands = []             # list of HandInfo, primary hand first
        self.zoom_delta = 0.0
        self.roll_delta = 0.0
        self.pan_delta = (0.0, 0.0)
        self.swipe = None
        self.events = []            # list of (hand_index, gesture)

    @property
    def detected(self):
        return len(self.hands) > 0


class HandTracker:
    def __init__(self, camera_index=0, model_path=MODEL_FILENAME, detection_confidence=0.6):
        _ensure_model(model_path)

        options = HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=model_path),
            num_hands=2,
            min_hand_detection_confidence=detection_confidence,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
            running_mode=RunningMode.VIDEO,
        )
        self.landmarker = HandLandmarker.create_from_options(options)

        self.cap = cv2.VideoCapture(camera_index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        self._timestamp_ms = 0
        self._prev_two_hand_dist = None
        self._prev_roll_angle = None
        self._debouncers = {}  # keyed by handedness label, created lazily

        self._motion_history = deque(maxlen=SWIPE_WINDOW_FRAMES)
        self._last_swipe_time = 0.0

        self._pinch_drag_prev = None
        self._pull_zoom_prev_size = None
        self._prev_primary_gesture = None
        self._prev_primary_pos = None  # used both for throw detection AND primary-hand tracking

    # ---- static pose classification --------------------------------------

    def _finger_extended(self, lm, tip_idx, pip_idx):
        return lm[tip_idx].y < lm[pip_idx].y

    def _thumb_extended(self, lm):
        tip_to_pinky_mcp = math.hypot(lm[4].x - lm[17].x, lm[4].y - lm[17].y)
        ip_to_pinky_mcp = math.hypot(lm[3].x - lm[17].x, lm[3].y - lm[17].y)
        # Require a clearer margin than "just barely farther" to cut down
        # on borderline flicker between thumb-extended and not.
        return tip_to_pinky_mcp > ip_to_pinky_mcp * 1.1

    def _palm_size(self, lm):
        return math.hypot(lm[0].x - lm[9].x, lm[0].y - lm[9].y)

    def _pinch_amount(self, lm):
        palm_size = self._palm_size(lm)
        if palm_size < 1e-6:
            return 0.0
        pinch_dist = math.hypot(lm[4].x - lm[8].x, lm[4].y - lm[8].y)
        ratio = pinch_dist / palm_size
        amount = (1.2 - ratio) / (1.2 - 0.15)
        return max(0.0, min(1.0, amount))

    def _classify(self, lm):
        thumb = self._thumb_extended(lm)
        index = self._finger_extended(lm, 8, 6)
        middle = self._finger_extended(lm, 12, 10)
        ring = self._finger_extended(lm, 16, 14)
        pinky = self._finger_extended(lm, 20, 18)
        finger_count = sum([thumb, index, middle, ring, pinky])

        palm_size = self._palm_size(lm)
        pinch_dist = math.hypot(lm[4].x - lm[8].x, lm[4].y - lm[8].y)
        pinch_ratio = pinch_dist / palm_size if palm_size > 1e-6 else 999

        # A natural fist often brings the thumb close to the curled index
        # too, which can look like a loose pinch. Two-tier threshold:
        # a VERY tight pinch (thumb+index deliberately touching) always
        # wins, but a fully-closed hand (0 fingers extended) takes
        # priority over a merely-moderate pinch reading.
        very_tight_pinch = pinch_ratio < 0.22
        loose_pinch = pinch_ratio < 0.35

        thumb_points_up = lm[4].y < lm[0].y
        thumb_points_down = lm[4].y > lm[0].y

        if very_tight_pinch and middle and ring and pinky:
            gesture = "ok_sign"
        elif very_tight_pinch:
            gesture = "pinch"
        elif finger_count == 0:
            gesture = "fist"
        elif finger_count == 5:
            gesture = "open_palm"
        elif loose_pinch and middle and ring and pinky:
            gesture = "ok_sign"
        elif loose_pinch:
            gesture = "pinch"
        elif index and middle and not ring and not pinky:
            gesture = "peace"
        elif index and pinky and not middle and not ring:
            gesture = "rock_sign"
        elif index and not middle and not ring and not pinky:
            gesture = "point"
        elif thumb and not index and not middle and not ring and not pinky and thumb_points_up:
            gesture = "thumbs_up"
        elif thumb and not index and not middle and not ring and not pinky and thumb_points_down:
            gesture = "thumbs_down"
        else:
            gesture = "unknown"

        return gesture, finger_count

    def _roll_angle(self, lm):
        dx = lm[17].x - lm[5].x
        dy = lm[17].y - lm[5].y
        return math.degrees(math.atan2(dy, dx))

    @staticmethod
    def _angle_delta(new, old):
        return (new - old + 180) % 360 - 180

    # ---- swipe detection ---------------------------------------------------

    def _update_swipe(self, primary_pos):
        now = time.time()
        self._motion_history.append(primary_pos)

        if now - self._last_swipe_time < SWIPE_COOLDOWN_SEC:
            return None
        if len(self._motion_history) < SWIPE_WINDOW_FRAMES:
            return None

        x0, y0 = self._motion_history[0]
        x1, y1 = self._motion_history[-1]
        dx, dy = x1 - x0, y1 - y0

        swipe = None
        if abs(dx) > SWIPE_DISPLACEMENT_THRESHOLD and abs(dy) < SWIPE_CROSS_AXIS_LIMIT:
            swipe = "right" if dx > 0 else "left"
        elif abs(dy) > SWIPE_DISPLACEMENT_THRESHOLD and abs(dx) < SWIPE_CROSS_AXIS_LIMIT:
            swipe = "down" if dy > 0 else "up"

        if swipe:
            self._last_swipe_time = now
            self._motion_history.clear()
        return swipe

    # ---- main read loop -----------------------------------------------------

    def read(self):
        result = TrackerResult()
        ok, frame = self.cap.read()
        if not ok:
            return result, None

        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        self._timestamp_ms += 33
        detection = self.landmarker.detect_for_video(mp_image, self._timestamp_ms)

        h, w = frame.shape[:2]
        raw_hands = detection.hand_landmarks or []

        # --- stable primary-hand ordering ---
        # Reorder so index 0 is whichever detected hand is closest to where
        # the primary hand was last frame, instead of trusting MediaPipe's
        # arbitrary per-frame ordering.
        order = list(range(len(raw_hands)))
        if self._prev_primary_pos is not None and len(raw_hands) > 1:
            def dist_to_prev(idx):
                wrist = raw_hands[idx][0]
                return math.hypot(wrist.x - self._prev_primary_pos[0], wrist.y - self._prev_primary_pos[1])
            order.sort(key=dist_to_prev)

        wrist_positions = []

        for slot, idx in enumerate(order):
            lm = raw_hands[idx]
            info = HandInfo()
            if detection.handedness and idx < len(detection.handedness):
                info.handedness = detection.handedness[idx][0].category_name

            wrist = lm[0]
            info.yaw_norm = (wrist.x - 0.5) * 2
            info.pitch_norm = (wrist.y - 0.5) * 2
            info.pinch_amount = self._pinch_amount(lm)
            info.roll_deg = self._roll_angle(lm)
            info.palm_size = self._palm_size(lm)

            raw_gesture, finger_count = self._classify(lm)
            info.raw_gesture = raw_gesture
            info.finger_count = finger_count

            debouncer = self._debouncers.setdefault(info.handedness, GestureDebouncer())
            confirmed, changed = debouncer.update(raw_gesture)
            info.gesture = confirmed
            if changed and confirmed in ACTIONABLE_GESTURES:
                result.events.append((slot, confirmed))

            result.hands.append(info)
            wrist_positions.append((wrist.x, wrist.y))

            for point in lm:
                cx, cy = int(point.x * w), int(point.y * h)
                cv2.circle(frame, (cx, cy), 4, (0, 255, 0), -1)
            label = f"{info.gesture} ({info.finger_count})"
            cv2.putText(
                frame, label, (int(wrist.x * w), int(wrist.y * h) - 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2,
            )

        # two-hand zoom
        if len(wrist_positions) == 2:
            (x1, y1), (x2, y2) = wrist_positions
            dist = math.hypot(x1 - x2, y1 - y2)
            if self._prev_two_hand_dist is not None:
                result.zoom_delta = dist - self._prev_two_hand_dist
            self._prev_two_hand_dist = dist
        else:
            self._prev_two_hand_dist = None

        # roll
        current_roll = None
        if len(wrist_positions) == 2:
            (x1, y1), (x2, y2) = wrist_positions
            current_roll = math.degrees(math.atan2(y2 - y1, x2 - x1))
        elif result.hands:
            current_roll = result.hands[0].roll_deg

        if current_roll is not None:
            if self._prev_roll_angle is not None:
                result.roll_delta = self._angle_delta(current_roll, self._prev_roll_angle)
            self._prev_roll_angle = current_roll
        else:
            self._prev_roll_angle = None

        # pinch-and-drag pan (primary hand only)
        if result.hands and result.hands[0].gesture == "pinch":
            pos = wrist_positions[0]
            if self._pinch_drag_prev is not None:
                dx = pos[0] - self._pinch_drag_prev[0]
                dy = pos[1] - self._pinch_drag_prev[1]
                result.pan_delta = (dx, dy)
            self._pinch_drag_prev = pos
        else:
            self._pinch_drag_prev = None

        # grab-and-pull zoom
        if result.hands and result.hands[0].gesture == "fist":
            size = result.hands[0].palm_size
            if self._pull_zoom_prev_size is not None:
                result.zoom_delta += (size - self._pull_zoom_prev_size) * PULL_ZOOM_GAIN
            self._pull_zoom_prev_size = size
        else:
            self._pull_zoom_prev_size = None

        # throw detection + primary-hand position tracking (used above too)
        if result.hands:
            primary = result.hands[0]
            primary_pos = wrist_positions[0]
            velocity = 0.0
            if self._prev_primary_pos is not None:
                velocity = math.hypot(
                    primary_pos[0] - self._prev_primary_pos[0],
                    primary_pos[1] - self._prev_primary_pos[1],
                )
            if (
                self._prev_primary_gesture == "fist"
                and primary.gesture == "open_palm"
                and velocity > THROW_VELOCITY_THRESHOLD
            ):
                result.events.append((0, "throw"))
            self._prev_primary_gesture = primary.gesture
            self._prev_primary_pos = primary_pos
        else:
            self._prev_primary_gesture = None
            self._prev_primary_pos = None

        # swipe detection
        if result.hands and result.hands[0].gesture in ("open_palm", "unknown"):
            result.swipe = self._update_swipe(wrist_positions[0])
        else:
            self._motion_history.clear()

        return result, frame

    def close(self):
        self.cap.release()
        self.landmarker.close()
