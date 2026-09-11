"""Pack particle geometry into bounded vertex arrays for batched OpenGL draws."""
from array import array

# Multi-hue nebula palette for the voice core: the particle sphere blends
# magenta/violet/cyan/green/white points instead of a single tint,
# matching the supplied constellation-dashboard reference.
NEBULA_PALETTE = (
    (1.0, 0.35, 0.85),  # magenta
    (0.62, 0.38, 1.0),  # violet
    (0.25, 0.75, 1.0),  # cyan
    (0.35, 1.0, 0.70),  # green
    (1.0, 0.72, 0.25),  # gold
    (0.95, 0.95, 1.0),  # white
    (0.62, 0.38, 1.0),  # violet (weighted)
    (1.0, 0.35, 0.85),  # magenta (weighted)
)


def build_buffers(frame, center, radius, color, brightness=1, palette=None):
    """Returns point-size batches and one line batch. Arrays live for one frame.

    ``palette`` optionally supplies per-particle RGB hues (a nebula look):
    each particle deterministically picks one palette entry instead of the
    single theme tint. Trails/rays/links always keep the theme tint.
    """
    cx, cy = center
    points = {size: (array('f'), array('f')) for size in (1, 2, 3)}
    lines, line_colors = array('f'), array('f')
    normal_tint = tuple(min(1, max(0, c)) for c in color)
    head_tint = tuple(min(1, max(0, c + .22)) for c in color)
    palette = tuple(tuple(min(1, max(0, c)) for c in entry) for entry in palette) if palette else None

    def rgba(alpha, head=False, tint=None):
        base = tint if tint is not None else (head_tint if head else normal_tint)
        return (*base, min(1, max(0, alpha * brightness)))

    for index, (x, y, alpha, size) in enumerate(frame.particles):
        vertices, colors = points[size]
        vertices.extend((cx + x * radius, cy + y * radius))
        if palette:
            tint = palette[(index * 31) % len(palette)]
            if size == 3:
                tint = tuple(min(1, c + .3) for c in tint)
            colors.extend(rgba(alpha, tint=tint))
        else:
            colors.extend(rgba(alpha, size == 3))

    def segment(a, b, alpha_a, alpha_b):
        lines.extend((cx + a[0] * radius, cy + a[1] * radius,
                      cx + b[0] * radius, cy + b[1] * radius))
        line_colors.extend(rgba(alpha_a))
        line_colors.extend(rgba(alpha_b))
    for trail in frame.orbits:
        for a, b in zip(trail, trail[1:]):
            segment(a, b, a[2], b[2])
    for x0, y0, x1, y1, alpha in (*frame.rays, *frame.links):
        segment((x0,y0), (x1,y1), alpha * .2, alpha)
    return points, (lines, line_colors)
