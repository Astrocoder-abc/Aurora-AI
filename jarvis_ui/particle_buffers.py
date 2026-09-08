"""Pack particle geometry into bounded vertex arrays for batched OpenGL draws."""
from array import array


def build_buffers(frame, center, radius, color, brightness=1):
    """Returns point-size batches and one line batch. Arrays live for one frame."""
    cx, cy = center
    points = {size: (array('f'), array('f')) for size in (1, 2, 3)}
    lines, line_colors = array('f'), array('f')
    normal_tint = tuple(min(1, max(0, c)) for c in color)
    head_tint = tuple(min(1, max(0, c + .22)) for c in color)
    def rgba(alpha, head=False):
        return (*(head_tint if head else normal_tint), min(1, max(0, alpha * brightness)))
    for x, y, alpha, size in frame.particles:
        vertices, colors = points[size]
        vertices.extend((cx + x * radius, cy + y * radius))
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
