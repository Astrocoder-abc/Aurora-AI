"""
Wiring that used to be "add these 3 snippets yourself" notes, applied at runtime so
hologram.py / hand_tracker.py stay untouched:
  * TipHandTracker  - adds `tip` (index fingertip) to the primary hand for gesture drawing
  * install()       - heart shape, flicker-free streaming info card, draw-mode + spatial-panel overlays
"""
from jarvis_ui.hand_tracker import HandTracker


class TipHandTracker(HandTracker):
    def _classify(self, lm, world, track):
        track.tip = (lm[8].x, lm[8].y)
        return super()._classify(lm, world, track)

    def read(self):
        result, frame = super().read()
        track = self._tracks.get(self._primary_id)
        if result.hands and track is not None:
            result.hands[0].tip = getattr(track, "tip", (0.5, 0.5))
        return result, frame


def install(hologram, drawer=None):
    from jarvis_ui import hologram as hmod

    # 1. human heart model (visuals.py)
    try:
        from jarvis_ui import visuals
        hmod.SHAPES.add("heart")
        orig_shape = hologram._draw_shape
        hologram._draw_shape = lambda name: visuals.draw_heart(hologram) if name == "heart" else orig_shape(name)
    except Exception as e:
        print(f"glue: heart model unavailable ({e})", flush=True)

    # 2. streaming replies re-called show_info_card per token, restarting the materialize
    #    animation every time. Update the text in place instead.
    orig_card = hologram.show_info_card

    def show_info_card(question, answer):
        card = hologram.info_card
        if hologram.mode == "info" and card and card.get("question") == question:
            card["answer"] = answer
        else:
            orig_card(question, answer)

    hologram.show_info_card = show_info_card

    # 3. overlays drawn on top of the dashboard: gesture-draw trails, spatial panels
    orig_dash = hologram._draw_dashboard

    def draw_dashboard(theme):
        orig_dash(theme)
        spatial = getattr(hologram, "spatial", None)
        if drawer is not None or spatial is not None:
            hologram._begin_ortho()
            try:
                if drawer is not None:
                    drawer.render(hologram)
                if spatial is not None:
                    spatial.update()
                    spatial.render(hologram)
            finally:
                hologram._end_ortho()

    hologram._draw_dashboard = draw_dashboard
