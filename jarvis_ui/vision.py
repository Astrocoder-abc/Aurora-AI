"""Vision helpers: local QR/barcode decoding + Groq scene description."""
import base64

import cv2

VISION_MODEL = "qwen/qwen3.8-27b"


def read_codes(frame):
    """Returns a list of decoded QR/barcode strings found in the frame."""
    found = []
    try:
        ok, decoded, _, _ = cv2.QRCodeDetector().detectAndDecodeMulti(frame)
        if ok:
            found += [d for d in decoded if d]
    except Exception:
        pass
    try:  # barcode module ships with opencv-contrib 4.8+
        det = cv2.barcode.BarcodeDetector()
        ok, decoded, _, _ = det.detectAndDecodeWithType(frame)[:4] if False else (None, None, None, None)
        res = det.detectAndDecode(frame)
        codes = res[1] if len(res) >= 3 else []
        if isinstance(codes, str):
            codes = [codes]
        found += [c for c in (codes or []) if c]
    except Exception:
        pass
    return list(dict.fromkeys(found))


def describe_scene(client, frame):
    """Sends the frame to Groq's vision model, returns a short spoken description."""
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("could not encode camera frame")
    b64 = base64.b64encode(buf.tobytes()).decode()
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        max_tokens=150,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "Describe what you see in 1-2 short plain spoken sentences. No markdown."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
    )
    return (resp.choices[0].message.content or "I couldn't make out anything.").strip()