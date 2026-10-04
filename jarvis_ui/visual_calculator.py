"""
Visual calculator: hold a math problem up to the camera (or have it on screen) and Aurora reads and solves it.

  "Aurora, solve this problem" / "read this equation" / "what's this equation" / "visual calculator"
  "Aurora, solve the equation on my screen"

Online: the vision model transcribes the problem. Offline (or if the AI fails): local OCR via pytesseract
(pip install pytesseract, plus the Tesseract program). Arithmetic and algebra (linear/quadratic in x) are solved
locally with a safe evaluator; anything harder goes to the AI when online, otherwise Aurora just reads it back.
"""
import ast
import math
import operator
import re

from jarvis_ui import system_control as sc

_ASK = re.compile(r"\b(?:solve|read|work out|calculate|check)\s+(?:this|that|the|my)\s+(?:math\s+)?(?:problem|equation|sum|expression)\b"
                  r"|\bvisual calculator\b|\bwhat(?:'s| is) (?:this|that) (?:math )?(?:problem|equation|sum)\b", re.I)
PROMPT = ("The image shows a math problem, printed or handwritten. Transcribe ONLY the expression or equation using plain "
          "characters: digits + - * / ^ ( ) = and x for the unknown. If there are several, give the first. "
          "If no math is visible reply NONE.")
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.Mod: operator.mod}
SPOKEN = (("**", " to the power of "), ("+", " plus "), ("-", " minus "), ("*", " times "), ("/", " divided by "), ("=", " equals "))


def normalize(text):
    t = text.replace("×", "*").replace("÷", "/").replace("−", "-").replace("–", "-").replace("^", "**")
    t = re.sub(r"(?<=\d),(?=\d{3})", "", t)
    t = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "*", t).replace("X", "x")
    t = re.sub(r"[^0-9x+\-*/().= ]", "", t)
    t = re.sub(r"(\d|\))\s*(x|\()", r"\1*\2", t)
    t = re.sub(r"(x|\))\s*(\d|\(|x)", r"\1*\2", t)
    return re.sub(r"\s+", "", t).rstrip("=") if t.strip().endswith("=") else re.sub(r"\s+", "", t)


def evaluate(node, x=None):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id == "x" and x is not None:
        return x
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = evaluate(node.operand, x)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        a, b = evaluate(node.left, x), evaluate(node.right, x)
        if isinstance(node.op, ast.Pow) and abs(b) > 100:
            raise ValueError("exponent too large")
        return OPS[type(node.op)](a, b)
    raise ValueError("unsupported")


def fmt(v):
    return str(int(round(v))) if abs(v - round(v)) < 1e-9 else f"{v:.6g}"


def _tree(expr):
    return ast.parse(expr, mode="eval").body


def solve_for_x(lhs, rhs):
    """Roots of lhs = rhs when it is at most quadratic in x; None if it isn't."""
    L, R = _tree(lhs), _tree(rhs)
    f = lambda v: evaluate(L, v) - evaluate(R, v)
    c, up, down = f(0), f(1), f(-1)
    a, b = (up + down) / 2 - c, (up - down) / 2
    if any(abs(f(v) - (a * v * v + b * v + c)) > 1e-6 * max(1, abs(f(v))) for v in (2, 3, -2)):
        return None
    if abs(a) < 1e-9:
        return ["any"] if abs(b) < 1e-9 and abs(c) < 1e-9 else [] if abs(b) < 1e-9 else [-c / b]
    d = b * b - 4 * a * c
    if d < 0:
        return []
    r = math.sqrt(d)
    return sorted({(-b - r) / (2 * a), (-b + r) / (2 * a)})


def solve_text(problem):
    """Spoken answer string, or None if this needs the AI."""
    expr = normalize(problem)
    if not expr or not re.search(r"\d|x", expr):
        return None
    try:
        lhs, eq, rhs = expr.partition("=")
        if eq and rhs and "x" in expr:
            roots = solve_for_x(lhs, rhs)
            if roots is None:
                return None
            return ("Every number works." if roots == ["any"] else "There is no real solution." if not roots
                    else " or ".join(f"x equals {fmt(r)}" for r in roots) + ".")
        if "x" in expr:
            return None
        if eq and rhs:
            a, b = evaluate(_tree(lhs)), evaluate(_tree(rhs))
            return "That's correct." if abs(a - b) < 1e-9 else f"That's wrong. The left side is {fmt(a)} and the right side is {fmt(b)}."
        return f"The answer is {fmt(evaluate(_tree(lhs)))}."
    except ZeroDivisionError:
        return "That divides by zero."
    except (ValueError, SyntaxError, OverflowError, TypeError):
        return None


def spoken(problem):
    t = normalize(problem)
    for sym, word in SPOKEN:
        t = t.replace(sym, word)
    return re.sub(r"\s+", " ", t).strip()


def ocr_math(frame):
    """Local OCR (no internet). None if pytesseract/Tesseract is missing or nothing was read."""
    try:
        import cv2
        import pytesseract
        g = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        g = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        if g.mean() < 127:
            g = cv2.bitwise_not(g)
        text = pytesseract.image_to_string(g, config="--psm 6 -c tessedit_char_whitelist=0123456789+-*/=()xX^.")
    except Exception:
        return None
    return next((l.strip() for l in text.splitlines() if re.search(r"\d", l)), None)


def handle_command(voice, text):
    """Voice entry point for voice_assistant.py. Returns True if handled."""
    t = text.strip(" .!?,")
    if not _ASK.search(t):
        return False
    speak = voice._speak
    frame = sc.capture_screen() if re.search(r"\bscreen\b", t, re.I) else voice.latest_frame
    if frame is None:
        speak("I can't get an image. Close any other app using the webcam and try again.")
        return True
    plus, client = getattr(voice, "plus", None), voice.client
    online = client is not None and (plus is None or plus.offline.online())
    problem, ai_ok = None, False
    if online:
        try:
            from jarvis_ui import brain
            raw = brain.describe_scene(client, frame, PROMPT, 120, 90).strip().strip("`")
            ai_ok, problem = True, (None if re.fullmatch(r"\W*none\W*", raw, re.I) else raw)
        except Exception as e:
            voice._log(f"VISION: math read failed ({e}), using local OCR")
    if not ai_ok:
        problem = ocr_math(frame)
    if not problem:
        speak("I couldn't read a math problem. Hold it flat, close to the camera, in good light."
              + ("" if ai_ok else " For offline reading, install pytesseract and Tesseract."))
        return True
    answer = solve_text(problem)
    if answer is None and ai_ok:
        try:
            from jarvis_ui import brain
            answer = brain._complete(client, "Solve the math problem. Reply in at most two short spoken sentences with the final "
                                     "answer. Plain text.", problem, 1500, effort="low")
        except Exception:
            pass
    answer = answer or ("I can't solve that without the AI." if not online else "I couldn't solve that one.")
    try:
        voice.hologram.show_info_card("Visual calculator", f"{problem}   ->   {answer}")
    except Exception:
        pass
    speak(f"I read {spoken(problem)}. {answer}")
    return True
