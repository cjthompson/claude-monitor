"""Custom half-block scrollbar renderers for claude-monitor TUI."""

from textual.scrollbar import ScrollBarRender


class HalfBlockScrollBarRender(ScrollBarRender):
    """Base renderer that draws the thumb using the half-block glyph in bar color (no reverse)."""

    @classmethod
    def render_bar(cls, size=25, virtual_size=50, window_size=20, position=0,
                   thickness=1, vertical=True,
                   back_color=None, bar_color=None) -> "Segments":
        from rich.color import Color
        from rich.segment import Segment, Segments
        from rich.style import Style
        from math import ceil
        if back_color is None:
            back_color = Color.parse("#000000")
        if bar_color is None:
            bar_color = Color.parse("bright_magenta")
        bars = cls.VERTICAL_BARS if vertical else cls.HORIZONTAL_BARS
        len_bars = len(bars)
        width_thickness = thickness if vertical else 1
        blank = cls.BLANK_GLYPH * width_thickness
        foreground_meta = {"@mouse.down": "grab"}
        if window_size and size and virtual_size and size != virtual_size:
            bar_ratio = virtual_size / size
            thumb_size = max(1, window_size / bar_ratio)
            position_ratio = position / (virtual_size - window_size)
            position = (size - thumb_size) * position_ratio
            start = int(position * len_bars)
            end = start + ceil(thumb_size * len_bars)
            start_index, start_bar = divmod(max(0, start), len_bars)
            end_index, end_bar = divmod(max(0, end), len_bars)
            upper = {"@mouse.down": "scroll_up"}
            lower = {"@mouse.down": "scroll_down"}
            upper_back_segment = Segment(blank, Style(bgcolor=back_color, meta=upper))
            lower_back_segment = Segment(blank, Style(bgcolor=back_color, meta=lower))
            segments = [upper_back_segment] * int(size)
            segments[end_index:] = [lower_back_segment] * (size - end_index)
            # Thumb: use bar_color as foreground, back_color as background (no reverse)
            segments[start_index:end_index] = [
                Segment(blank, Style(color=bar_color, bgcolor=back_color, meta=foreground_meta))
            ] * (end_index - start_index)
            # Fractional end caps
            if start_index < len(segments):
                bar_character = bars[len_bars - 1 - start_bar]
                if bar_character != " ":
                    segments[start_index] = Segment(
                        bar_character * width_thickness,
                        Style(color=bar_color, bgcolor=back_color, meta=foreground_meta),
                    )
            if end_index < len(segments):
                bar_character = bars[len_bars - 1 - end_bar]
                if bar_character != " ":
                    segments[end_index] = Segment(
                        bar_character * width_thickness,
                        Style(color=bar_color, bgcolor=back_color, meta=foreground_meta),
                    )
        else:
            segments = [Segment(blank, Style(bgcolor=back_color))] * int(size)
        if vertical:
            return Segments(segments, new_lines=True)
        else:
            return Segments((segments + [Segment.line()]) * thickness, new_lines=False)


class HorizontalScrollBarRender(HalfBlockScrollBarRender):
    BLANK_GLYPH = "\u2584"
    HORIZONTAL_BARS = [" ", " ", " ", " ", " ", " ", " ", " "]  # disable fractional end caps


class VerticalScrollBarRender(HalfBlockScrollBarRender):
    BLANK_GLYPH = "\u2590"
    VERTICAL_BARS = ["\u2590", "\u2590", "\u2590", "\u2590", "\u2590", "\u2590", "\u2590", " "]  # disable fractional end caps
