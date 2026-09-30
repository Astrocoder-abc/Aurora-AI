"""
Hand tracking wrapper around MediaPipe's HandLandmarker (Tasks API).

v4 ("smarter"):
  - Soft finger scoring (joint angle + reach ratio, 3D world landmarks) with
    per-finger hysteresis, instead of hard thresholds.
  - Intent gating: one-shot gesture events only fire when the hand is fairly
    still, and each gesture has a cooldown, so swipes/moves don't misfire.
  - Dropout bridging: if detection blinks out for a few frames, the last known
    hand is held instead of snapping the hologram back to idle.
  - Two-hand zoom only once both hands have been stable for a few frames.
  - Auto low-light enhancement (CLAHE) when the camera image is dark.
  - One Euro smoothing, persistent hand tracks, pinch hysteresis,
    time-based swipe/throw, low-latency camera setup.

Public interface unchanged: HandTracker.read() -> (TrackerResult, frame).
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

ACTIONABLE_GESTURES = {"peace", "thumbs_up", "thumbs_down", "rock_sign", "ok_sign", "point"}

# --- tunables ---------------------------------------------------------------
ANGLE_LO, ANGLE_HI = 100.0, 160.0   # PIP joint angle range mapped to 0..1 "straightness"
REACH_LO, REACH_HI = 1.0, 1.3       # tip-to-wrist / pip-to-wrist ratio mapped to 0..1
EXT_ENTER, EXT_EXIT = 0.6, 0.4      # per-finger hysteresis on the combined score

PINCH_ENTER = 0.28                  # thumb-index gap / palm size to START a pinch
PINCH_EXIT = 0.45                   # ...and to END it

MATCH_MAX_DIST = 0.30               # max wrist jump (normalized) to keep the same hand ID
MAX_MISSED_FRAMES = 8               # frames before a lost hand's state is dropped
HOLD_FRAMES = 4                     # frames a lost hand is "held" in the output
STABLE_AGE = 5                      # frames a hand must exist before two-hand zoom counts it

EVENT_MAX_SPEED = 0.9               # wrist speed (frame-widths/s) above which events wait
EVENT_PENDING_SEC = 0.5             # how long a gesture may wait for the hand to settle
EVENT_COOLDOWN = {"point": 0.5}     # per-gesture cooldown, default below
EVENT_COOLDOWN_DEFAULT = 0.8

SWIPE_WINDOW_SEC = 0.4
SWIPE_DISPLACEMENT_THRESHOLD = 0.35
SWIPE_CROSS_AXIS_LIMIT = 0.15
SWIPE_COOLDOWN_SEC = 0.6

PULL_ZOOM_GAIN = 6.0
THROW_SPEED_THRESHOLD = 1.2
ROLL_DEADZONE_DEG = 0.3

LOW_LIGHT_MEAN = 80                 # mean pixel value below which CLAHE is applied

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
    (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
]

FINGERS = {"index": (5, 6, 8), "middle": (9, 10, 12), "ring": (13, 14, 16), "pinky": (17, 18, 20)}


def _ensure_model(model_path):
    if os.path.exists(model_path):
        return
    print(f"Downloading hand landmark model to {model_path} ...")
    urllib.request.urlretrieve(MODEL_URL, model_path)
    print("Download complete.")


def _clamp01(x):
    return max(0.0, min(1.0, x))


class OneEuro:
    """One Euro filter: heavy smoothing when slow, light smoothing when fast."""

    def __init__(self, min_cutoff=1.5, beta=10.0, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = self.dx = self.t = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, t):
        if self.t is None:
            self.x, self.dx, self.t = x, 0.0, t
            return x
        dt = max(1e-3, t - self.t)
        self.t = t
        self.dx += self._alpha(self.d_cutoff, dt) * ((x - self.x) / dt - self.dx)
        cutoff = self.min_cutoff + self.beta * abs(self.dx)
        self.x += self._alpha(cutoff, dt) * (x - self.x)
        return self.x


class GestureDebouncer:
    """Confirms a gesture once it wins a majority vote over a sliding window."""

    def __init__(self, window=6, min_votes=4):
        self.history = deque(maxlen=window)
        self.min_votes = min_votes
        self.confirmed = "unknown"

    def update(self, raw_gesture):
        self.history.append(raw_gesture)
        best, count = Counter(self.history).most_common(1)[0]
        changed = False
        if count >= self.min_votes and self.confirmed != best:
            self.confirmed = best
            changed = True
        return self.confirmed, changed


class _Track:
    """Persistent per-hand state, matched across frames by position."""

    def __init__(self, tid):
        self.id = tid
        self.fx = OneEuro()
        self.fy = OneEuro()
        self.fpinch = OneEuro(min_cutoff=2.0, beta=2.0)
        self.debounce = GestureDebouncer()
        self.ext = {"thumb": False, "index": False, "middle": False, "ring": False, "pinky": False}
        self.pinching = False
        self.roll = None
        self.palm = None
        self.raw = (0.0, 0.0)
        self.missed = 0
        self.age = 0
        self.pending = None
        self.pending_t = 0.0
        self.last_fire = {}
        self.last_info = None
        self.last_pos = (0.5, 0.5)

    @property
    def speed(self):
        dx, dy = self.fx.dx or 0.0, self.fy.dx or 0.0
        return math.hypot(dx, dy)


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
        self.hands = []             # HandInfo list, primary hand first
        self.zoom_delta = 0.0
        self.roll_delta = 0.0
        self.pan_delta = (0.0, 0.0)
        self.swipe = None
        self.events = []            # (hand_index, gesture)

    @property
    def detected(self):
        return len(self.hands) > 0


def _pt(l):
    return (l.x, l.y, l.z)


def _angle(a, b, c):
    """Angle in degrees at point b between segments b->a and b->c."""
    v1 = [a[i] - b[i] for i in range(3)]
    v2 = [c[i] - b[i] for i in range(3)]
    n = math.sqrt(sum(x * x for x in v1)) * math.sqrt(sum(x * x for x in v2))
    if n < 1e-9:
        return 180.0
    cos = sum(v1[i] * v2[i] for i in range(3)) / n
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


class HandTracker:
    def __init__(self, camera_index=0, model_path=MODEL_FILENAME, detection_confidence=0.5):
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

        self.cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW) if os.name == "nt" \
            else cv2.VideoCapture(camera_index)
        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(camera_index)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self._clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))

        self._ts = 0
        self._tracks = {}
        self._next_id = 0
        self._primary_id = None

        self._prev_two_hand_dist = None
        self._prev_roll_angle = None
        self._roll_mode = 0
        self._motion = deque(maxlen=60)
        self._last_swipe_time = 0.0
        self._pinch_drag_prev = None
        self._pull_zoom_prev_size = None
        self._prev_primary_gesture = None
        self._prev_primary_pos = None
        self._prev_time = None
        self._peak_speed = 0.0

    # ---- image prep ------------------------------------------------------

    def _enhance(self, rgb):
        """Boost contrast in dim rooms so the landmark model keeps working."""
        if rgb[::8, ::8].mean() >= LOW_LIGHT_MEAN:
            return rgb
        l, a, b = cv2.split(cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB))
        l = self._clahe.apply(l)
        return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2RGB)

    # ---- track management ------------------------------------------------

    def _assign(self, wrists):
        """Match each detection to an existing track by nearest wrist; create
        new tracks for unmatched ones. Returns {detection_index: track}."""
        pairs = sorted(
            (math.hypot(w[0] - t.raw[0], w[1] - t.raw[1]), di, t.id)
            for di, w in enumerate(wrists) for t in self._tracks.values()
        )
        assigned, used = {}, set()
        for dist, di, tid in pairs:
            if dist > MATCH_MAX_DIST:
                break
            if di in assigned or tid in used:
                continue
            assigned[di] = self._tracks[tid]
            used.add(tid)
        for di in range(len(wrists)):
            if di not in assigned:
                t = _Track(self._next_id)
                self._next_id += 1
                self._tracks[t.id] = t
                assigned[di] = t
                used.add(t.id)
        for tid in list(self._tracks):
            if tid in used:
                self._tracks[tid].missed = 0
            else:
                self._tracks[tid].missed += 1
                if self._tracks[tid].missed > MAX_MISSED_FRAMES:
                    del self._tracks[tid]
        for di, w in enumerate(wrists):
            assigned[di].raw = w
        return assigned

    # ---- pose classification ------------------------------------------------

    def _classify(self, lm, world, track):
        """Returns (gesture, finger_count, pinch_ratio). Uses 3D world
        landmarks (metric, rotation-invariant) when available."""
        p = [_pt(l) for l in (world if world else lm)]
        palm = max((math.dist(p[0], p[9]) + math.dist(p[5], p[17])) / 2, 1e-6)

        scores = {}
        for name, (m, pip, tip) in FINGERS.items():
            a = _clamp01((_angle(p[m], p[pip], p[tip]) - ANGLE_LO) / (ANGLE_HI - ANGLE_LO))
            reach = math.dist(p[tip], p[0]) / max(math.dist(p[pip], p[0]), 1e-6)
            r = _clamp01((reach - REACH_LO) / (REACH_HI - REACH_LO))
            scores[name] = 0.6 * a + 0.4 * r

        t_ratio = math.dist(p[4], p[17]) / max(math.dist(p[3], p[17]), 1e-6)
        t_out = math.dist(p[4], p[5]) / palm
        scores["thumb"] = min(_clamp01((t_ratio - 1.0) / 0.2), _clamp01((t_out - 0.3) / 0.3))

        for k, s in scores.items():
            track.ext[k] = s > (EXT_EXIT if track.ext[k] else EXT_ENTER)

        ext = track.ext
        finger_count = sum(ext.values())

        ratio = math.dist(p[4], p[8]) / palm
        track.pinching = ratio < (PINCH_EXIT if track.pinching else PINCH_ENTER)

        idx, mid, ring, pinky, thumb = ext["index"], ext["middle"], ext["ring"], ext["pinky"], ext["thumb"]

        if track.pinching:
            gesture = "ok_sign" if (mid and ring and pinky) else "pinch"
        elif not (idx or mid or ring or pinky):
            gesture = "fist"
            if thumb:
                vx, vy = lm[4].x - lm[2].x, lm[4].y - lm[2].y
                n = math.hypot(vx, vy)
                if n > 1e-6 and abs(vy) > 0.6 * n:
                    gesture = "thumbs_up" if vy < 0 else "thumbs_down"
        elif idx and mid and ring and pinky:
            gesture = "open_palm"
        elif idx and mid and not ring and not pinky:
            gesture = "peace"
        elif idx and pinky and not mid and not ring:
            gesture = "rock_sign"
        elif idx and not mid and not ring and not pinky:
            gesture = "point"
        else:
            gesture = "unknown"

        return gesture, finger_count, ratio

    @staticmethod
    def _angle_delta(new, old):
        return (new - old + 180) % 360 - 180

    # ---- intent gating for one-shot events ---------------------------------

    def _gate_event(self, track, confirmed, changed, now):
        """Fire an actionable gesture once, only when the hand is settled and
        the gesture isn't on cooldown. Returns True if it should fire."""
        if changed:
            track.pending = confirmed if confirmed in ACTIONABLE_GESTURES else None
            track.pending_t = now
        if track.pending != confirmed:
            track.pending = None
            return False
        if now - track.pending_t > EVENT_PENDING_SEC:
            track.pending = None
            return False
        cooldown = EVENT_COOLDOWN.get(confirmed, EVENT_COOLDOWN_DEFAULT)
        if track.speed < EVENT_MAX_SPEED and now - track.last_fire.get(confirmed, -99.0) > cooldown:
            track.last_fire[confirmed] = now
            track.pending = None
            return True
        return False

    # ---- swipe detection ---------------------------------------------------

    def _update_swipe(self, pos, now):
        self._motion.append((now, pos[0], pos[1]))
        while self._motion and now - self._motion[0][0] > SWIPE_WINDOW_SEC:
            self._motion.popleft()
        if now - self._last_swipe_time < SWIPE_COOLDOWN_SEC or len(self._motion) < 4:
            return None

        _, x0, y0 = self._motion[0]
        dx, dy = pos[0] - x0, pos[1] - y0
        swipe = None
        if abs(dx) > SWIPE_DISPLACEMENT_THRESHOLD and abs(dy) < SWIPE_CROSS_AXIS_LIMIT:
            swipe = "right" if dx > 0 else "left"
        elif abs(dy) > SWIPE_DISPLACEMENT_THRESHOLD and abs(dx) < SWIPE_CROSS_AXIS_LIMIT:
            swipe = "down" if dy > 0 else "up"

        if swipe:
            self._last_swipe_time = now
            self._motion.clear()
        return swipe

    # ---- main read loop -----------------------------------------------------

    def read(self):
        result = TrackerResult()
        ok, frame = self.cap.read()
        if not ok:
            return result, None

        frame = cv2.flip(frame, 1)
        rgb = self._enhance(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        now = time.monotonic()
        self._ts = max(self._ts + 1, int(now * 1000))
        detection = self.landmarker.detect_for_video(mp_image, self._ts)

        h, w = frame.shape[:2]
        raw_hands = detection.hand_landmarks or []
        world_hands = detection.hand_world_landmarks or []
        handedness = detection.handedness or []

        wrists_raw = [(lm[0].x, lm[0].y) for lm in raw_hands]
        assigned = self._assign(wrists_raw)

        if assigned:
            ids = {t.id for t in assigned.values()}
            if self._primary_id not in ids:
                self._primary_id = min(ids)
        order = sorted(assigned, key=lambda di: (assigned[di].id != self._primary_id, assigned[di].id))

        wrist_positions = []
        ordered_tracks = []

        for slot, di in enumerate(order):
            lm = raw_hands[di]
            world = world_hands[di] if di < len(world_hands) else None
            track = assigned[di]
            track.age += 1

            info = HandInfo()
            if di < len(handedness) and handedness[di]:
                info.handedness = handedness[di][0].category_name

            fx = track.fx(lm[0].x, now)
            fy = track.fy(lm[0].y, now)
            track.last_pos = (fx, fy)
            info.yaw_norm = (fx - 0.5) * 2
            info.pitch_norm = (fy - 0.5) * 2

            raw_gesture, finger_count, ratio = self._classify(lm, world, track)
            info.raw_gesture = raw_gesture
            info.finger_count = finger_count

            amount = _clamp01((1.0 - ratio) / 0.85)
            info.pinch_amount = _clamp01(track.fpinch(amount, now))

            angle = math.degrees(math.atan2(lm[17].y - lm[5].y, lm[17].x - lm[5].x))
            if track.roll is None:
                track.roll = angle
            else:
                track.roll += 0.5 * self._angle_delta(angle, track.roll)
            info.roll_deg = track.roll

            ps = math.hypot(lm[0].x - lm[9].x, lm[0].y - lm[9].y)
            track.palm = ps if track.palm is None else track.palm + 0.4 * (ps - track.palm)
            info.palm_size = track.palm

            confirmed, changed = track.debounce.update(raw_gesture)
            info.gesture = confirmed
            if self._gate_event(track, confirmed, changed, now):
                result.events.append((slot, confirmed))

            track.last_info = info
            result.hands.append(info)
            wrist_positions.append((fx, fy))
            ordered_tracks.append(track)

            # debug overlay
            pts = [(int(l.x * w), int(l.y * h)) for l in lm]
            for a, b in HAND_CONNECTIONS:
                cv2.line(frame, pts[a], pts[b], (0, 160, 0), 1)
            for c in pts:
                cv2.circle(frame, c, 3, (0, 255, 0), -1)
            cv2.putText(frame, f"{info.gesture} ({info.finger_count})",
                        (pts[0][0], pts[0][1] - 20), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 255, 255), 2)

        # ---- no hands: bridge short dropouts, otherwise reset ----
        if not result.hands:
            held = self._tracks.get(self._primary_id)
            if held is not None and held.last_info is not None and held.missed <= HOLD_FRAMES:
                result.hands = [held.last_info]
                return result, frame

            self._prev_two_hand_dist = None
            self._prev_roll_angle = None
            self._roll_mode = 0
            self._pinch_drag_prev = None
            self._pull_zoom_prev_size = None
            self._prev_primary_gesture = None
            self._prev_primary_pos = None
            self._prev_time = now
            self._peak_speed = 0.0
            self._motion.clear()
            return result, frame

        dt = max(1e-3, now - self._prev_time) if self._prev_time is not None else 1 / 30
        self._prev_time = now

        # two-hand zoom (only once both hands are stable, to ignore flicker detections)
        two_stable = len(wrist_positions) == 2 and all(t.age >= STABLE_AGE for t in ordered_tracks)
        if two_stable:
            (x1, y1), (x2, y2) = wrist_positions
            dist = math.hypot(x1 - x2, y1 - y2)
            if self._prev_two_hand_dist is not None:
                result.zoom_delta = dist - self._prev_two_hand_dist
            self._prev_two_hand_dist = dist
        else:
            self._prev_two_hand_dist = None

        # roll (reference resets when switching between 1- and 2-hand mode)
        mode = 2 if two_stable else 1
        if mode != self._roll_mode:
            self._prev_roll_angle = None
            self._roll_mode = mode
        if two_stable:
            (x1, y1), (x2, y2) = wrist_positions
            current_roll = math.degrees(math.atan2(y2 - y1, x2 - x1))
        else:
            current_roll = result.hands[0].roll_deg
        if self._prev_roll_angle is not None:
            delta = self._angle_delta(current_roll, self._prev_roll_angle)
            result.roll_delta = 0.0 if abs(delta) < ROLL_DEADZONE_DEG else delta
        self._prev_roll_angle = current_roll

        primary = result.hands[0]
        primary_pos = wrist_positions[0]

        # pinch-and-drag pan
        if primary.gesture == "pinch":
            if self._pinch_drag_prev is not None:
                result.pan_delta = (primary_pos[0] - self._pinch_drag_prev[0],
                                    primary_pos[1] - self._pinch_drag_prev[1])
            self._pinch_drag_prev = primary_pos
        else:
            self._pinch_drag_prev = None

        # grab-and-pull zoom
        if primary.gesture == "fist":
            if self._pull_zoom_prev_size is not None:
                result.zoom_delta += (primary.palm_size - self._pull_zoom_prev_size) * PULL_ZOOM_GAIN
            self._pull_zoom_prev_size = primary.palm_size
        else:
            self._pull_zoom_prev_size = None

        # throw: fist -> open palm right after a fast wrist motion
        speed = 0.0
        if self._prev_primary_pos is not None:
            speed = math.hypot(primary_pos[0] - self._prev_primary_pos[0],
                               primary_pos[1] - self._prev_primary_pos[1]) / dt
        self._peak_speed = max(speed, self._peak_speed * math.exp(-dt / 0.25))
        if (self._prev_primary_gesture == "fist" and primary.gesture == "open_palm"
                and self._peak_speed > THROW_SPEED_THRESHOLD):
            result.events.append((0, "throw"))
            self._peak_speed = 0.0
        self._prev_primary_gesture = primary.gesture
        self._prev_primary_pos = primary_pos

        # swipe
        if primary.gesture in ("open_palm", "unknown"):
            result.swipe = self._update_swipe(primary_pos, now)
        else:
            self._motion.clear()

        return result, frame

    def close(self):
        self.cap.release()
        self.landmarker.close()
