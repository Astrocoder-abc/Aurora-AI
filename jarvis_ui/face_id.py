"""
Facial recognition for Aurora — consent-based enrollment only.

This recognizes people who have explicitly enrolled their own face (via
"Aurora, remember my face as <name>"). It does NOT identify, track, or
log unknown/random people caught on camera — faces that don't match an
enrolled person are simply ignored. All data (face model + names) stays
local on this machine in face_data/, nothing is uploaded anywhere.

Uses OpenCV's built-in Haar cascade for face detection (ships with
opencv-python, no extra download) and LBPH — Local Binary Patterns
Histograms — for recognition, via opencv-contrib-python. LBPH was chosen
specifically because it installs from a prebuilt Windows wheel with no
compilation step, unlike heavier alternatives like dlib/face_recognition
which have historically been painful to install on Windows.
"""

import json
import os

from .paths import PROJECT_ROOT

import cv2
import numpy as np

DATA_DIR = os.path.join(str(PROJECT_ROOT), "face_data")
MODEL_PATH = os.path.join(DATA_DIR, "lbph_model.yml")
PEOPLE_PATH = os.path.join(DATA_DIR, "people.json")
CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"

# LBPH: LOWER confidence value = more confident match. Above this
# threshold, treat it as "not confidently recognized" rather than
# guessing wrong.
RECOGNITION_CONFIDENCE_THRESHOLD = 75


class FaceID:
    def __init__(self, on_log=None):
        self._on_log = on_log or (lambda msg: None)
        os.makedirs(DATA_DIR, exist_ok=True)

        self.detector = cv2.CascadeClassifier(CASCADE_PATH)
        self._trained = False

        if self.detector.empty():
            self._on_log(f"FACE: could not load face detector from {CASCADE_PATH} — "
                         "face recognition disabled (this usually means opencv-python "
                         "and opencv-contrib-python are both installed and conflicting — "
                         "run: pip uninstall opencv-python opencv-python-headless "
                         "opencv-contrib-python -y, then pip install opencv-contrib-python)")
            self.detection_available = False
        else:
            self.detection_available = True

        try:
            self.recognizer = cv2.face.LBPHFaceRecognizer_create()
            self.available = True
        except AttributeError:
            self._on_log("FACE: opencv-contrib-python not installed — "
                         "face recognition disabled (pip install opencv-contrib-python)")
            self.recognizer = None
            self.available = False

        self.people = self._load_people()
        if self.available:
            self._load_model()

    # ---- persistence -------------------------------------------------------

    def _load_people(self):
        if os.path.exists(PEOPLE_PATH):
            try:
                with open(PEOPLE_PATH, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_people(self):
        with open(PEOPLE_PATH, "w") as f:
            json.dump(self.people, f, indent=2)

    def _load_model(self):
        if os.path.exists(MODEL_PATH):
            try:
                self.recognizer.read(MODEL_PATH)
                self._trained = True
                self._on_log(f"FACE: loaded model with {len(self.people)} enrolled people")
            except Exception as e:
                self._on_log(f"FACE: could not load face model ({e})")

    # ---- detection -----------------------------------------------------------

    def detect_faces(self, gray_frame):
        """Returns a list of (x, y, w, h) boxes for every face found in
        this frame — just detection, not identification."""
        if not self.detection_available:
            return []
        return self.detector.detectMultiScale(
            gray_frame, scaleFactor=1.2, minNeighbors=5, minSize=(80, 80)
        )

    # ---- enrollment (explicit consent) ----------------------------------------

    def enroll(self, gray_face_samples, name):
        """gray_face_samples: list of grayscale face-crop images, all of
        the same person, captured with their knowledge. Adds or updates
        this person in the trained model."""
        if not self.available or not gray_face_samples:
            return False

        if name in self.people:
            label = self.people[name]["label"]
        else:
            # Forgotten labels still exist in LBPH; never assign them to someone else.
            labels = [info["label"] for info in self.people.values()]
            if self._trained:
                labels.extend(int(v) for v in self.recognizer.getLabels().flatten())
            label = max(labels, default=-1) + 1
            self.people[name] = {"label": label, "theme_index": len(self.people) % 4}

        faces = [cv2.resize(f, (200, 200)) for f in gray_face_samples]
        labels = np.array([label] * len(faces), dtype=np.int32)

        if self._trained:
            self.recognizer.update(faces, labels)
        else:
            self.recognizer.train(faces, labels)
            self._trained = True

        self.recognizer.save(MODEL_PATH)
        self._save_people()
        self._on_log(f"FACE: enrolled '{name}' with {len(faces)} samples")
        return True

    def forget(self, name):
        """Removes a person's enrollment. Note: LBPH doesn't support
        removing a single label from an existing model cleanly, so this
        clears them from the name list (they'll no longer be
        recognized/greeted) but a full re-enroll of everyone else would
        be needed to fully purge their data from the underlying model
        file. Good enough for the practical goal: they stop being
        recognized and greeted."""
        if name in self.people:
            del self.people[name]
            self._save_people()
            return True
        return False

    # ---- recognition --------------------------------------------------------

    def recognize(self, gray_frame, face_box):
        """Returns (name, confidence) if confidently matched to an
        enrolled person, or (None, confidence) if a face was detected
        but doesn't match anyone enrolled — this is the deliberate
        design point: unknown faces are never identified as anything,
        just silently ignored."""
        if not self.available or not self.people or not self._trained:
            return None, None

        x, y, w, h = face_box
        face_crop = cv2.resize(gray_frame[y:y + h, x:x + w], (200, 200))
        try:
            label, confidence = self.recognizer.predict(face_crop)
        except cv2.error:
            return None, None

        if confidence > RECOGNITION_CONFIDENCE_THRESHOLD:
            return None, confidence

        name = next((n for n, info in self.people.items() if info["label"] == label), None)
        return name, confidence

    def get_theme_index(self, name):
        info = self.people.get(name)
        return info["theme_index"] if info else None
