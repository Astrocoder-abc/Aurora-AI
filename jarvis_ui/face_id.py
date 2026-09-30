"""
Consent-based face recognition (OpenCV Haar detection + LBPH). Only people who say
"Aurora, remember my face as <name>" are ever recognised; everything stays in face_data/.

Why the old version failed, and what changed:
  * Enrollment grabbed 15 near-identical frames in ~0.5 s, while Aurora was still talking.
    Now: 2.5 s delay, then ~36 captures over several seconds (turn your head a little),
    each stored with 3 brightness variants.
  * No lighting normalisation. Now: CLAHE on a trimmed, resized crop, for enrolment and matching.
  * Model file (.yml) could silently fail to save/load on Windows paths with non-ASCII characters.
    Now: face crops are stored as PNGs and the model is retrained from them at startup.
  * forget() left the person in the model and label ids could be reused, mixing identities.
    Now: forget deletes their samples and retrains.
  * Threshold 75 was too strict for a different-lighting session. Now 80, override with a number
    in face_threshold.txt (lower = stricter). Distances are logged so you can tune it.
"""
import json
import os
import re
import shutil
import time
from collections import Counter, deque

import cv2
import numpy as np

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DATA_DIR = os.path.join(BASE, "face_data")
SAMPLES_DIR = os.path.join(DATA_DIR, "samples")
PEOPLE_PATH = os.path.join(DATA_DIR, "people.json")
THRESHOLD_FILE = os.path.join(BASE, "face_threshold.txt")

FACE_SIZE = 160
DEFAULT_THRESHOLD = 80.0
VOTE_WINDOW, VOTE_MIN_WINS = 4, 2
ENROLL_SAMPLES, ENROLL_DELAY, ENROLL_TIMEOUT = 36, 2.5, 30.0

_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))


def prep(gray, box):
    """Trimmed, resized, contrast-normalised face crop (or None)."""
    x, y, w, h = (int(v) for v in box)
    m = int(0.08 * w)
    crop = gray[max(0, y + m // 2):min(gray.shape[0], y + h - m // 2), max(0, x + m):min(gray.shape[1], x + w - m)]
    if crop.size == 0:
        return None
    return _clahe.apply(cv2.resize(crop, (FACE_SIZE, FACE_SIZE), interpolation=cv2.INTER_AREA))


def _read_gray(path):
    return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_GRAYSCALE)     # unicode-path safe


def _write_png(path, img):
    ok, buf = cv2.imencode(".png", img)
    if ok:
        buf.tofile(path)


class FaceID:
    def __init__(self, on_log=None):
        self._log = on_log or (lambda m: None)
        os.makedirs(SAMPLES_DIR, exist_ok=True)
        self.detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        self.detection_available = not self.detector.empty()
        self.available = hasattr(cv2, "face") and hasattr(cv2.face, "LBPHFaceRecognizer_create")
        if not self.detection_available:
            self._log("FACE: could not load the face detector - OpenCV install conflict. Run: pip uninstall "
                      "opencv-python opencv-python-headless opencv-contrib-python -y && pip install opencv-contrib-python")
        if not self.available:
            self._log("FACE: cv2.face missing - run: pip install opencv-contrib-python (and uninstall plain opencv-python)")

        self.threshold = self._load_threshold()
        self.recognizer, self._labels = None, {}
        self.last_distance = None
        self._votes, self._dists = deque(maxlen=VOTE_WINDOW), deque(maxlen=VOTE_WINDOW)
        self.confirmed_name = None
        self._enroll = None
        self.people = self._load_people()
        if self.available:
            self._retrain()

    # ---- storage --------------------------------------------------------------
    def _load_threshold(self):
        try:
            return float(open(THRESHOLD_FILE).read().strip())
        except Exception:
            return DEFAULT_THRESHOLD

    @staticmethod
    def _dirname(name):
        return re.sub(r"[^\w\-]", "_", name)

    def _dir(self, name):
        return os.path.join(SAMPLES_DIR, self._dirname(name))

    def _load_people(self):
        try:
            meta = json.load(open(PEOPLE_PATH, encoding="utf-8"))
        except Exception:
            meta = {}
        people = {}
        for d in sorted(os.listdir(SAMPLES_DIR)):
            if os.path.isdir(os.path.join(SAMPLES_DIR, d)) and any(f.endswith(".png") for f in os.listdir(os.path.join(SAMPLES_DIR, d))):
                name = next((n for n in meta if self._dirname(n) == d), d)
                people[name] = {"theme_index": meta.get(name, {}).get("theme_index", len(people) % 4)}
        return people

    def _save_people(self):
        with open(PEOPLE_PATH, "w", encoding="utf-8") as f:
            json.dump(self.people, f, indent=2)

    def _retrain(self):
        imgs, labels, self._labels, self.recognizer = [], [], {}, None
        for label, name in enumerate(sorted(self.people)):
            self._labels[label] = name
            d = self._dir(name)
            for fn in os.listdir(d):
                if fn.endswith(".png"):
                    im = _read_gray(os.path.join(d, fn))
                    if im is not None and im.shape == (FACE_SIZE, FACE_SIZE):
                        imgs.append(im)
                        labels.append(label)
        self._reset_votes()
        if not imgs:
            return
        rec = cv2.face.LBPHFaceRecognizer_create()
        rec.train(imgs, np.array(labels, dtype=np.int32))
        self.recognizer = rec
        self._log(f"FACE: trained on {len(imgs)} samples of {len(self.people)} people (threshold {self.threshold:.0f})")

    # ---- detection ----------------------------------------------------------------
    def detect_faces(self, gray):
        """[(x, y, w, h)], largest first."""
        if not self.detection_available:
            return []
        faces = self.detector.detectMultiScale(cv2.equalizeHist(gray), scaleFactor=1.1, minNeighbors=5, minSize=(70, 70))
        return sorted((tuple(int(v) for v in f) for f in faces), key=lambda f: -f[2] * f[3])

    # ---- enrollment (explicit consent) -----------------------------------------------
    @property
    def enrolling(self):
        return self._enroll is not None

    def start_enrollment(self, name):
        if not (self.available and self.detection_available):
            return False
        self._enroll = {"name": name, "t0": time.time(), "last": 0.0, "imgs": []}
        return True

    def feed_enrollment(self, gray, faces):
        """Call every frame while enrolling. None = still collecting, else (ok, name, message)."""
        e, now = self._enroll, time.time()
        if now - e["t0"] > ENROLL_TIMEOUT:
            self._enroll = None
            return False, e["name"], "I couldn't get a clear look at your face. Face the camera in good light and try again."
        if now - e["t0"] < ENROLL_DELAY or not faces or now - e["last"] < 0.15:
            return None
        img = prep(gray, faces[0])
        if img is None:
            return None
        e["last"] = now
        e["imgs"].append(img)
        if len(e["imgs"]) < ENROLL_SAMPLES:
            return None
        self._enroll = None
        try:
            return self._finish_enrollment(e)
        except Exception as ex:
            self._log(f"FACE: enrollment failed ({ex})")
            return False, e["name"], "Something went wrong saving your face. Say 'check face recognition' for details."

    def _finish_enrollment(self, e):
        name, d, stamp = e["name"], self._dir(e["name"]), int(time.time())
        os.makedirs(d, exist_ok=True)
        for i, img in enumerate(e["imgs"]):
            for v, variant in enumerate((img, cv2.convertScaleAbs(img, alpha=0.85, beta=-10),
                                         cv2.convertScaleAbs(img, alpha=1.15, beta=10))):
                _write_png(os.path.join(d, f"{stamp}_{i:02d}_{v}.png"), variant)
        self.people.setdefault(name, {"theme_index": len(self.people) % 4})
        self._save_people()
        self._retrain()
        self._log(f"FACE: enrolled '{name}' ({len(e['imgs'])} captures)")
        return True, name, ""

    def forget(self, name):
        key = next((n for n in self.people if n.lower() == name.lower()), None)
        if not key:
            return False
        shutil.rmtree(self._dir(key), ignore_errors=True)
        del self.people[key]
        self._save_people()
        self._retrain()
        return True

    # ---- recognition ----------------------------------------------------------------
    def _reset_votes(self):
        self._votes.clear()
        self._dists.clear()
        self.confirmed_name = None

    def recognize(self, gray, box):
        """(name, avg_distance). name only once it wins VOTE_MIN_WINS of the last VOTE_WINDOW attempts."""
        if not self.recognizer:
            return None, None
        img = prep(gray, box)
        if img is None:
            return None, None
        try:
            label, dist = self.recognizer.predict(img)
        except cv2.error:
            return None, None
        self.last_distance = dist
        self._votes.append(self._labels.get(label) if dist <= self.threshold else None)
        self._dists.append(dist)
        counts = Counter(v for v in self._votes if v)
        best, wins = counts.most_common(1)[0] if counts else (None, 0)
        avg = sum(self._dists) / len(self._dists)
        if best and wins >= VOTE_MIN_WINS:
            self.confirmed_name = best
            return best, avg
        return None, avg

    def get_theme_index(self, name):
        info = self.people.get(name)
        return info["theme_index"] if info else None
