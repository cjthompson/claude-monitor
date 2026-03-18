"""Fixed-width sparkline widget for claude-monitor TUI."""

from textual.widgets import Sparkline


class FixedWidthSparkline(Sparkline):
    """Sparkline where each data point = exactly 1 column, right-aligned.

    Pads with zeros on the left so 1 data point = 1 column with no
    horizontal scaling. Data should be pre-normalized to 0.0-1.0.
    """

    def render(self):
        from fractions import Fraction
        from rich.color import Color
        from rich.segment import Segment
        from rich.style import Style
        from rich.color_triplet import ColorTriplet

        width = self.size.width
        height = self.size.height
        data = list(self.data or [])
        if len(data) > width:
            data = data[-width:]
        if len(data) < width:
            data = [0.0] * (width - len(data)) + data
        if len(data) < 2:
            data = [0.0, 0.0]

        _, base = self.background_colors
        min_color = base + (
            self.get_component_styles("sparkline--min-color").color
            if self.min_color is None
            else self.min_color
        )
        max_color = base + (
            self.get_component_styles("sparkline--max-color").color
            if self.max_color is None
            else self.max_color
        )

        # Render directly: data is pre-normalized 0.0-1.0, no re-scaling.
        BARS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
        bar_segs_per_row = len(BARS)
        total_bar_segs = bar_segs_per_row * height - 1

        mc = min_color.rich_color
        xc = max_color.rich_color

        def _blend(ratio: float) -> Style:
            r1, g1, b1 = mc.triplet or ColorTriplet(0, 0, 0)
            r2, g2, b2 = xc.triplet or ColorTriplet(255, 255, 255)
            r = int(r1 + (r2 - r1) * ratio)
            g = int(g1 + (g2 - g1) * ratio)
            b = int(b1 + (b2 - b1) * ratio)
            return Style.from_color(Color.from_rgb(r, g, b))

        lines: list[list[Segment]] = []
        for row in reversed(range(height)):
            row_low = row * bar_segs_per_row
            row_high = (row + 1) * bar_segs_per_row
            segs: list[Segment] = []
            for val in data:
                bar_idx = int(val * total_bar_segs)
                bar_idx = min(bar_idx, total_bar_segs)
                if bar_idx < row_low:
                    segs.append(Segment(" "))
                elif bar_idx >= row_high:
                    segs.append(Segment("\u2588", _blend(val)))
                else:
                    ch = BARS[bar_idx % bar_segs_per_row]
                    segs.append(Segment(ch, _blend(val)))
            lines.append(segs)

        from rich.console import ConsoleOptions, RenderResult as RR, Console
        class _Renderable:
            def __rich_console__(self_r, console: Console, options: ConsoleOptions) -> RR:
                for i, line in enumerate(lines):
                    yield from line
                    if i < len(lines) - 1:
                        yield Segment.line()

        return _Renderable()
