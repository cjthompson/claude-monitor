"""Tests for scrollbar.py — custom scrollbar renderers."""

from claude_monitor.widgets.scrollbar import (
    HalfBlockScrollBarRender,
    HorizontalScrollBarRender,
    VerticalScrollBarRender,
)


class TestHalfBlockScrollBarRender:
    def test_vertical_render(self):
        segments = HalfBlockScrollBarRender.render_bar(
            size=10, virtual_size=50, window_size=10, position=0,
            thickness=1, vertical=True,
        )
        assert segments is not None

    def test_vertical_render_middle(self):
        segments = HalfBlockScrollBarRender.render_bar(
            size=10, virtual_size=50, window_size=10, position=20,
            thickness=1, vertical=True,
        )
        assert segments is not None

    def test_horizontal_render(self):
        segments = HalfBlockScrollBarRender.render_bar(
            size=10, virtual_size=50, window_size=10, position=0,
            thickness=1, vertical=False,
        )
        assert segments is not None

    def test_no_scroll_needed(self):
        """When virtual_size == size, scrollbar is blank."""
        segments = HalfBlockScrollBarRender.render_bar(
            size=10, virtual_size=10, window_size=10, position=0,
            thickness=1, vertical=True,
        )
        assert segments is not None

    def test_zero_values(self):
        segments = HalfBlockScrollBarRender.render_bar(
            size=0, virtual_size=0, window_size=0, position=0,
        )
        assert segments is not None

    def test_defaults(self):
        segments = HalfBlockScrollBarRender.render_bar()
        assert segments is not None


class TestHorizontalScrollBarRender:
    def test_renders(self):
        segments = HorizontalScrollBarRender.render_bar(
            size=20, virtual_size=100, window_size=20, position=0,
            thickness=1, vertical=False,
        )
        assert segments is not None

    def test_blank_glyph(self):
        assert HorizontalScrollBarRender.BLANK_GLYPH == "\u2584"


class TestVerticalScrollBarRender:
    def test_renders(self):
        segments = VerticalScrollBarRender.render_bar(
            size=20, virtual_size=100, window_size=20, position=0,
            thickness=1, vertical=True,
        )
        assert segments is not None

    def test_blank_glyph(self):
        assert VerticalScrollBarRender.BLANK_GLYPH == "\u2590"
