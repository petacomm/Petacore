"""The shareable project card.

Takes the numbers from `stats` and lays them out as a poster people can post
— the same idea as a Strava activity card. Everything is generated as SVG
text, so no drawing library is required; the app rasterises it to PNG with
whatever the toolkit already provides.

Three shapes are offered:

    story    1080 x 1920   9:16, for stories and reels
    square   1080 x 1080   1:1, for feeds
    wide     1920 x 1080   16:9, for slides and READMEs
"""

import html
import os

from . import stats

SIZES = {
    "story": (1080, 1920),
    "square": (1080, 1080),
    "wide": (1920, 1080),
}

# Brand colours, used by the Overview page inside the app.
LANGUAGE_COLOURS = {
    "Python": "#3572A5", "JavaScript": "#f1e05a", "TypeScript": "#3178c6",
    "HTML": "#e34c26", "CSS": "#563d7c", "C": "#555555", "C++": "#f34b7d",
    "C#": "#178600", "Java": "#b07219", "Rust": "#dea584", "Go": "#00ADD8",
    "PHP": "#4F5D95", "Ruby": "#701516", "Swift": "#F05138",
    "Kotlin": "#A97BFF", "Lua": "#000080", "Shell": "#89e051",
    "SQL": "#e38c00", "JSON": "#a0a0a0", "YAML": "#cb171e",
    "TOML": "#9c4221", "XML": "#0060ac", "Markdown": "#083fa1",
    "Vala": "#a56de2", "Dart": "#00B4AB", "R": "#198CE7", "Perl": "#0298c3",
    "Elixir": "#6e4a7e", "Haskell": "#5e5086", "Scala": "#c22d40",
}
FALLBACK_COLOURS = ["#8ab4f8", "#f28b82", "#fdd663", "#81c995", "#c58af9",
                    "#78d9ec", "#ffa26b"]

# The card itself uses a flat, high-contrast palette instead, so the bar
# reads clearly at story size on a phone screen.
BAR_COLOURS = ["#ffffff", "#5b3f9d", "#9a9a9a", "#3daee9", "#c58af9",
               "#f5a623", "#81c995"]

FONT = "Ubuntu, Ubuntu Sans, sans-serif"

BG = "#1a1a1a"
TEXT = "#ffffff"
DIM = "#b5b5b5"


def colour_for(language, index):
    """Brand colour, for the in-app language list."""
    return LANGUAGE_COLOURS.get(
        language, FALLBACK_COLOURS[index % len(FALLBACK_COLOURS)])


def bar_colour(index):
    """Card palette, in order of language share."""
    return BAR_COLOURS[index % len(BAR_COLOURS)]


def _esc(text):
    return html.escape(str(text), quote=True)


def _inline_icon(path, x, y, size):
    """Drop another SVG file into the card at (x, y).

    The file's own markup is embedded rather than linked, because external
    references are not resolved by every rasteriser — inlining makes the card
    render identically everywhere.
    """
    import re as _re
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return ""
    match = _re.search(r"<svg[^>]*>(.*)</svg>", source, _re.S)
    if not match:
        return ""
    inner = match.group(1)

    box = _re.search(r'viewBox="([\d.\-\s]+)"', source)
    if box:
        numbers = [float(v) for v in box.group(1).split()]
        native = max(numbers[2], numbers[3]) if len(numbers) == 4 else 24.0
    else:
        native = 24.0
    scale = size / native
    return (f'<g transform="translate({x},{y}) scale({scale:.4f})">'
            f'{inner}</g>')


def _icon_path(folder, name):
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(here, folder, name + ".svg")
    return candidate if os.path.isfile(candidate) else None


def build_svg(project_name: str, data: dict, shape: str = "story",
              logo_href: str = None) -> str:
    """Compose the card. `data` is the dict returned by stats.analyse().

    A designer's template in card-templates/ wins over the built-in layout.
    """
    custom = render_template(shape, project_name, data)
    if custom:
        return custom

    width, height = SIZES.get(shape, SIZES["story"])
    scale = width / 1080.0 if height >= width else height / 1080.0

    def s(value):
        return int(value * scale)

    margin = s(110)
    inner = width - margin * 2
    languages = data.get("languages", [])[:4]
    percent_total = sum(item["percent"] for item in languages) or 1

    # vertical rhythm, tuned on the 1080x1920 story and scaled from there
    if height >= width:                       # story / square
        title_y = int(height * 0.415)
        label_y = int(height * 0.508)
        value_y = int(height * 0.552)
        bar_y = int(height * 0.622)
        legend_y = int(height * 0.668)
        caption_y = int(height * 0.684)
        footer_y = int(height * 0.935)
    else:                                     # wide
        title_y = int(height * 0.34)
        label_y = int(height * 0.50)
        value_y = int(height * 0.58)
        bar_y = int(height * 0.68)
        legend_y = int(height * 0.755)
        caption_y = int(height * 0.785)
        footer_y = int(height * 0.92)

    title_size = s(150)
    label_size = s(34)
    value_size = s(78)
    bar_h = s(46)
    legend_icon = s(46)
    legend_size = s(40)
    caption_size = s(18)
    footer_size = s(36)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="{BG}"/>',
    ]

    # -- project name, centred ------------------------------------------------
    # A long name would run past both edges, so the title shrinks until it
    # fits between the margins.
    estimated_title = len(project_name) * title_size * 0.55
    if estimated_title > inner:
        title_size = max(s(46), int(title_size * inner / estimated_title))
    parts.append(
        f'<text x="{width // 2}" y="{title_y}" fill="{TEXT}" '
        f'font-family="{FONT}" font-size="{title_size}" font-weight="300" '
        f'text-anchor="middle">{_esc(project_name)}</text>')

    # -- the three figures ----------------------------------------------------
    figures = [
        ("Lines Of Code", stats.human_count(data.get("lines", 0)), "start",
         margin),
        ("Characters", stats.human_count(data.get("characters", 0)),
         "middle", width // 2),
        ("Files", str(data.get("files", 0)), "end", width - margin),
    ]
    for label, value, anchor, x in figures:
        parts.append(
            f'<text x="{x}" y="{label_y}" fill="{TEXT}" '
            f'font-family="{FONT}" font-size="{label_size}" '
            f'text-anchor="{anchor}">{_esc(label)}</text>')
        parts.append(
            f'<text x="{x}" y="{value_y + value_size // 2}" fill="{TEXT}" '
            f'font-family="{FONT}" font-size="{value_size}" '
            f'font-weight="400" text-anchor="{anchor}">{_esc(value)}</text>')

    # -- language bar ---------------------------------------------------------
    x = margin
    for index, item in enumerate(languages):
        segment = int(inner * item["percent"] / percent_total)
        if index == len(languages) - 1:
            segment = margin + inner - x          # avoid rounding gaps
        if segment <= 0:
            continue
        parts.append(
            f'<rect x="{x}" y="{bar_y}" width="{segment}" height="{bar_h}" '
            f'fill="{bar_colour(index)}"/>')
        x += segment

    # -- legend: language icon, share, and a one-line caption -----------------
    x = margin
    for index, item in enumerate(languages):
        segment = int(inner * item["percent"] / percent_total)
        icon_file = _icon_path("filetype-icons", item["language"].lower()
                               .replace("+", "p").replace("#", "sharp"))
        if icon_file:
            parts.append(_inline_icon(icon_file, x, legend_y - legend_icon,
                                      legend_icon))
        else:
            radius = legend_icon // 2
            parts.append(
                f'<circle cx="{x + radius}" cy="{legend_y - radius}" '
                f'r="{radius}" fill="{bar_colour(index)}"/>')
        parts.append(
            f'<text x="{x + legend_icon + s(16)}" y="{legend_y - s(6)}" '
            f'fill="{TEXT}" font-family="{FONT}" font-size="{legend_size}">'
            f'{item["percent"]:.0f}%</text>')
        parts.append(
            f'<text x="{x}" y="{caption_y + caption_size}" fill="{TEXT}" '
            f'font-family="{FONT}" font-size="{caption_size}">'
            f'Contains {item["percent"]:.0f}% {_esc(item["language"])}</text>')
        x += segment

    # -- footer ---------------------------------------------------------------
    mark = _icon_path("brand-icons", "petacomm")
    text_x = margin
    if mark:
        mark_w = s(76)
        parts.append(_inline_icon(mark, margin, footer_y - s(28), mark_w))
        text_x = margin + mark_w + s(20)

    footer_text = (f"Statics by Petacore, {project_name} app developed "
                   f"with Petacore")
    # Long project names would otherwise run off the edge, so the footer is
    # shrunk to whatever still fits between the mark and the right margin.
    available = (width - margin) - text_x
    estimated = len(footer_text) * footer_size * 0.47
    if estimated > available:
        footer_size = max(s(20), int(footer_size * available / estimated))
    parts.append(
        f'<text x="{text_x}" y="{footer_y}" fill="{TEXT}" '
        f'font-family="{FONT}" font-size="{footer_size}">'
        f'{_esc(footer_text)}</text>')

    parts.append('</svg>')
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Custom templates
#
# A designer can drop their own SVG into card-templates/ and Petacore fills
# it in, instead of drawing the built-in layout. See the README in that
# folder: text placeholders like {{LINES}}, bar segments with id="bar1",
# and icon slots with id="icon1".
# --------------------------------------------------------------------------- #
TEMPLATE_DIR = "card-templates"


def template_path(shape: str):
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(here, TEMPLATE_DIR, f"{shape}.svg")
    return candidate if os.path.isfile(candidate) else None


def _placeholders(project_name: str, data: dict):
    languages = data.get("languages", [])
    values = {
        "PROJECT": project_name,
        "LINES": stats.human_count(data.get("lines", 0)),
        "CHARS": stats.human_count(data.get("characters", 0)),
        "FILES": str(data.get("files", 0)),
        "PLATFORM": data.get("platform", ""),
    }
    for index in range(1, 5):
        item = languages[index - 1] if index <= len(languages) else None
        values[f"LANG{index}"] = item["language"] if item else ""
        values[f"PCT{index}"] = f'{item["percent"]:.0f}%' if item else ""
    return values


def _fill_text(svg: str, values: dict) -> str:
    """Replace {{TOKEN}} placeholders, tolerating the way editors split a
    string across several <tspan> runs."""
    import re as _re
    for token, value in values.items():
        svg = svg.replace("{{%s}}" % token, _esc(value))
    # a placeholder broken up by markup, e.g. {{LI<tspan>NES}}
    def repair(match):
        raw = _re.sub(r"<[^>]+>", "", match.group(0))
        token = raw.strip("{} ")
        return _esc(values.get(token, ""))
    return _re.sub(r"\{\{[^{}]*?\}\}", repair, svg, flags=_re.S)


def _fill_bars(svg: str, data: dict) -> str:
    """Re-proportion elements named bar1..bar4 to the language shares,
    keeping the total width the designer chose."""
    import re as _re
    languages = data.get("languages", [])[:4]
    total_percent = sum(item["percent"] for item in languages) or 1

    bars = []
    for index in range(1, 5):
        match = _re.search(
            r'<rect[^>]*id="bar%d"[^>]*/?>' % index, svg)
        if not match:
            continue
        tag = match.group(0)
        x = _re.search(r'\bx="([-\d.]+)"', tag)
        w = _re.search(r'\bwidth="([-\d.]+)"', tag)
        if not (x and w):
            continue
        bars.append((index, tag, float(x.group(1)), float(w.group(1))))
    if not bars:
        return svg

    start = min(item[2] for item in bars)
    span = max(item[2] + item[3] for item in bars) - start

    cursor = start
    for position, (index, tag, _x, _w) in enumerate(bars):
        if index <= len(languages):
            share = languages[index - 1]["percent"] / total_percent
            width = span * share
            if index == len(languages):        # last one closes the bar
                width = start + span - cursor
        else:
            width = 0.0
        new_tag = _re.sub(r'\bx="[-\d.]+"', 'x="%.2f"' % cursor, tag)
        new_tag = _re.sub(r'\bwidth="[-\d.]+"',
                          'width="%.2f"' % max(0.0, width), new_tag)
        svg = svg.replace(tag, new_tag, 1)
        cursor += width
    return svg


def _fill_icons(svg: str, data: dict) -> str:
    """Drop the language logo into any placeholder named icon1..icon4."""
    import re as _re
    languages = data.get("languages", [])[:4]
    for index in range(1, 5):
        match = _re.search(r'<rect[^>]*id="icon%d"[^>]*/?>' % index, svg)
        if not match:
            continue
        tag = match.group(0)
        if index > len(languages):
            svg = svg.replace(tag, "", 1)
            continue
        x = _re.search(r'\bx="([-\d.]+)"', tag)
        y = _re.search(r'\by="([-\d.]+)"', tag)
        w = _re.search(r'\bwidth="([-\d.]+)"', tag)
        h = _re.search(r'\bheight="([-\d.]+)"', tag)
        if not (x and y and w):
            continue
        size = min(float(w.group(1)),
                   float(h.group(1)) if h else float(w.group(1)))
        name = languages[index - 1]["language"].lower()
        name = name.replace("+", "p").replace("#", "sharp")
        icon = _icon_path("filetype-icons", name)
        replacement = (_inline_icon(icon, float(x.group(1)),
                                    float(y.group(1)), size)
                       if icon else "")
        svg = svg.replace(tag, replacement, 1)
    return svg


def render_template(shape: str, project_name: str, data: dict):
    """Fill the designer's template, or None when there isn't one."""
    path = template_path(shape)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            svg = f.read()
    except OSError:
        return None
    svg = _fill_text(svg, _placeholders(project_name, data))
    svg = _fill_bars(svg, data)
    svg = _fill_icons(svg, data)
    return svg


def write_svg(path: str, project_name: str, data: dict,
              shape: str = "story", logo_href: str = None) -> str:
    svg = build_svg(project_name, data, shape, logo_href)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return path


# --------------------------------------------------------------------------- #
# PNG output
# --------------------------------------------------------------------------- #
def _render_with_qt(svg: str, path: str, width: int, height: int) -> bool:
    try:
        from PySide6.QtCore import QByteArray
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
    except Exception:
        return False
    try:
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        if not renderer.isValid():
            return False
        image = QImage(width, height, QImage.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        return bool(image.save(path, "PNG"))
    except Exception:
        return False


def _render_with_gdkpixbuf(svg: str, path: str, width: int,
                           height: int) -> bool:
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, Gio
    except Exception:
        return False
    try:
        stream = Gio.MemoryInputStream.new_from_bytes(
            GLibBytes(svg.encode("utf-8")))
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(
            stream, width, height, True, None)
        return bool(pixbuf.savev(path, "png", [], []))
    except Exception:
        return False


def GLibBytes(data):
    from gi.repository import GLib
    return GLib.Bytes.new(data)


def _render_with_tool(svg_path: str, path: str, width: int,
                      height: int) -> bool:
    """rsvg-convert or Inkscape, if either happens to be installed."""
    import shutil
    import subprocess
    if shutil.which("rsvg-convert"):
        cmd = ["rsvg-convert", "-w", str(width), "-h", str(height),
               "-o", path, svg_path]
    elif shutil.which("inkscape"):
        cmd = ["inkscape", svg_path, "--export-type=png",
               f"--export-filename={path}", f"--export-width={width}"]
    else:
        return False
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        return proc.returncode == 0 and os.path.isfile(path)
    except Exception:
        return False


def _render_with_cairosvg(svg: str, path: str, width: int,
                          height: int) -> bool:
    try:
        import cairosvg
        cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=path,
                         output_width=width, output_height=height)
        return os.path.isfile(path)
    except Exception:
        return False


def write_png(path: str, project_name: str, data: dict,
              shape: str = "story", logo_href: str = None) -> str:
    """Render the card straight to PNG.

    Several rasterisers are tried in turn so this works on a plain GNOME
    box, a plain KDE box, or anything in between. The SVG is kept next to
    the PNG only if nothing could rasterise it, so the user is never left
    empty-handed.
    """
    import tempfile

    width, height = SIZES.get(shape, SIZES["story"])
    svg = build_svg(project_name, data, shape, logo_href)

    if _render_with_qt(svg, path, width, height):
        return path
    if _render_with_gdkpixbuf(svg, path, width, height):
        return path
    if _render_with_cairosvg(svg, path, width, height):
        return path

    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False,
                                     encoding="utf-8") as f:
        f.write(svg)
        svg_path = f.name
    try:
        if _render_with_tool(svg_path, path, width, height):
            return path
    finally:
        try:
            os.remove(svg_path)
        except OSError:
            pass

    # Nothing available: hand back an SVG rather than failing outright.
    fallback = os.path.splitext(path)[0] + ".svg"
    with open(fallback, "w", encoding="utf-8") as f:
        f.write(svg)
    return fallback
