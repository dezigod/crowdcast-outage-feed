"""Fail-closed extraction of the verified WAPA two-page daily layout.

This is not a general PDF/table parser. Text anchors, complete column shapes,
headings, and all seven time boundaries must match the inspected daily layout.
The date is read exclusively from both printed titles. A layout change requires
human inspection and a new validator, never a best-effort interpretation.
"""
from datetime import datetime, timedelta
from io import BytesIO
import re
from zoneinfo import ZoneInfo

from pypdf import PdfReader, apply_configuration
from pypdf.generic import ContentStream, DecodedStreamObject, NameObject

_IDENTITY = [1, 0, 0, 1, 0, 0]
_TZ = ZoneInfo("America/St_Thomas")
_MONTHS = "JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER OCTOBER NOVEMBER DECEMBER".split()


def _multiply(a, b):
    return [a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3],
            a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3],
            a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5]]


def _font_size(x, y, text, page_number):
    if y > 600:
        return 37.12
    if y > 540:
        return 42.72
    if y > 440:
        return 26.32
    if y > 380:
        return 31.25
    if y > 200:
        if text.startswith("Feeder "):
            return 22.99
        return 19.0 if page_number == 0 else (17.61 if x < 300 else 18.0)
    if y > 65:
        return 16.99
    return 13.33 if y > 48 else 8.0


def _positioned_text(page, page_number):
    """Resolve Form XObjects explicitly: stock visitor matrices are form-local.

    Strip Do from each local text pass, then recurse once with the complete
    parent transform. This also prevents duplicate or wrongly positioned text.
    """
    rows = []

    def walk(obj, parent, resources, ancestors=()):
        if len(ancestors) > 12 or id(obj) in ancestors:
            raise ValueError("Unsupported recursive PDF form")
        stream = ContentStream(obj.get_contents() if "/Contents" in obj else obj, page.pdf)
        if len(stream.operations) > 10000:
            raise ValueError("Oversized PDF content stream")
        clean = ContentStream(None, page.pdf)
        clean.operations = [(args, op) for args, op in stream.operations if op != b"Do"]
        local = DecodedStreamObject()
        local.set_data(clean.get_data())
        local[NameObject("/Resources")] = resources

        def visit(text, cm, tm, font, size):
            if not text.strip():
                return
            matrix = _multiply(_multiply(tm, cm), parent)
            if (abs(matrix[1]) > .001 or abs(matrix[2]) > .001
                    or matrix[0] <= 0 or matrix[3] <= 0):
                raise ValueError("Rotated or reflected schedule text")
            # One visitor record must correspond to one printed line.
            lines = text.strip().splitlines()
            if len(lines) != 1 or len(text) > 250:
                raise ValueError("Unsupported PDF text grouping")
            x, y = matrix[4:6]
            # Validate visual text scale as well as baselines. A tiny, giant,
            # stretched, or invisible text layer is not this verified template.
            expected_size = _font_size(x, y, lines[0], page_number)
            if (abs(matrix[0] * size - expected_size) > .15
                    or abs(matrix[3] * size - expected_size) > .15):
                raise ValueError("Unsupported schedule font geometry")
            rows.append((x, y, " ".join(lines[0].split())))
            if len(rows) > 200:
                raise ValueError("Too much schedule text")

        page.extract_xform_text(local, orientations=(0, 90, 180, 270), visitor_text=visit)
        matrix, stack = _IDENTITY, []
        for args, op in stream.operations:
            if op == b"Tr" and args != [0]:
                raise ValueError("Unsupported schedule text rendering mode")
            if op == b"q":
                stack.append(matrix)
            elif op == b"Q":
                if not stack:
                    raise ValueError("Unbalanced PDF graphics state")
                matrix = stack.pop()
            elif op == b"cm":
                matrix = _multiply([float(v) for v in args], matrix)
            elif op == b"Do":
                form = resources["/XObject"][args[0]]
                if form["/Subtype"] == "/Form":
                    transform = _multiply(_multiply(form.get("/Matrix", _IDENTITY), matrix), parent)
                    walk(form, transform, form.get("/Resources", resources), ancestors + (id(obj),))
                elif form["/Subtype"] != "/Image":
                    raise ValueError("Unsupported PDF object")
        if stack:
            raise ValueError("Unbalanced PDF graphics state")

    walk(page, _IDENTITY, page["/Resources"])
    return rows


# Baseline coordinates in PDF points, not text-stream order. Each column lists
# its two time lines, Outage heading, and feeder/area groups. Tolerance is only
# rounding/export drift (3 points), not enough to cross a column boundary.
_COLUMNS = (
    (
        ((90.3, 494.6), (89.9, 457.8), (79.6, 399.5),
         (((87.1, 361.7), ((100, 336.2), (84.9, 318.2))),
          ((87.4, 280.7), ((91.3, 254.4), (89.3, 234.9))))),
        ((270.5, 494.6), (276.3, 457.8), (269.9, 398.7),
         (((268.7, 360.7), ((254.9, 335.5), (247.1, 316.7))),)),
        ((459.9, 494.6), (474.6, 457.8), (458.6, 396.1),
         (((464, 361.7), ((466.6, 338.8), (474.4, 322.3))),
          ((464, 294.2), ((448.6, 271.7),)))),
        ((658.3, 494.6), (664.4, 457.8), (642.9, 394),
         (((650.2, 360.7), ()),)),
    ),
    (
        ((136, 499.9), (146.4, 463.2), (127.6, 402.7),
         (((133.6, 359.2), ((117.4, 338.1), (107.8, 313.8))),)),
        ((370.7, 503), (376.8, 466.2), (357.1, 402.7),
         (((361.2, 359.2), ((327.9, 337.2), (354.8, 322.2))),)),
        ((600.3, 499.6), (599.9, 462.8), (585.6, 401.9),
         (((590, 359.2), ((583.9, 338.7), (599.6, 323.7))),
          ((590.6, 282.4), ((547.8, 264.7),)))),
    ),
)
_STATIC = (
    (254.8, 742.3, "STT/STJDISTRICT"),
    (215.3, 690.5, "ROTATIONALOUTAGE"),
    (349.8, 638.8, "SCHEDULE"),
    (54.1, 164.8, "This schedule will only be activated if needed based on electricity demand. Not all feeders"),
    (54.1, 141.5, "listed may be affected. Restoration may be delayed by up to 15 minutes, and the schedule may"),
    (54.1, 118.3, "change based on demand."),
    (54.1, 95, "WAPA urges residents and businesses across St. Thomas and St. John to conserve electricity"),
    (54.1, 71.8, "over the next two weeks as we work through current generation challenges."),
    (643.2, 56.6, "Stay Connected"),
    (492.4, 40, "WWW.VIWAPA.VI"),
    (626.3, 40.7, "@USVIWAPA"),
    (701.6, 40.7, "@VIWAPA"),
)


def _take(rows, point):
    matches = [i for i, (x, y, _) in enumerate(rows)
               if abs(x - point[0]) <= 3 and abs(y - point[1]) <= 3]
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous schedule text anchor")
    return rows.pop(matches[0])[2]


def _printed_date(rows):
    matches = [i for i, (x, y, _) in enumerate(rows) if 90 <= x <= 300 and abs(y - 567.2) <= 3]
    if len(matches) != 1:
        raise ValueError("Missing or ambiguous printed schedule date")
    text = rows.pop(matches[0])[2].replace(" ", "")
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]?),([0-9]{4})", text)
    if not match or match[1] not in _MONTHS:
        raise ValueError("Schedule title requires a full dated heading")
    return datetime(int(match[3]), _MONTHS.index(match[1]) + 1, int(match[2]), tzinfo=_TZ)


def _clock(text):
    match = re.fullmatch(r"(1[0-2]|[1-9]):\s*([0-5][0-9])\s*(AM|PM)", text)
    if not match:
        raise ValueError("Malformed printed schedule time")
    return (int(match[1]) % 12 + (12 if match[3] == "PM" else 0)) * 60 + int(match[2])


def _validate_layout(pages):
    windows, day = [], None
    for page_number, source_rows in enumerate(pages):
        rows = list(source_rows)
        printed = _printed_date(rows)
        if day is not None and printed != day:
            raise ValueError("Schedule page dates disagree")
        day = printed
        for x, y, expected in _STATIC:
            actual = _take(rows, (x, y))
            if actual.replace(" ", "") != expected.replace(" ", ""):
                raise ValueError("Unsupported schedule heading or disclaimer")
        for start_point, end_point, heading, groups in _COLUMNS[page_number]:
            start_text = _take(rows, start_point)
            if not start_text.endswith(" -"):
                raise ValueError("Missing time range separator")
            start_minute = _clock(start_text[:-2])
            end_minute = _clock(_take(rows, end_point))
            # This verified daily template has exactly seven contiguous 2h slots.
            expected_start = 8 * 60 + 30 + 120 * len(windows)
            if start_minute != expected_start or end_minute != expected_start + 120:
                raise ValueError("Unsupported or discontinuous daily time boundaries")
            if _take(rows, heading) != "Outage:":
                raise ValueError("Missing outage column heading")
            feeders, areas = [], []
            for feeder_point, area_points in groups:
                feeder = _take(rows, feeder_point)
                match = re.fullmatch(r"Feeder ([1-9][0-9]?[A-Z])", feeder)
                if not match or feeder in feeders:
                    raise ValueError("Malformed or duplicate feeder")
                feeders.append(feeder)
                if area_points:
                    area = " ".join(_take(rows, point) for point in area_points)
                    # The inspected template omits the closing parenthesis for
                    # this one literal area; do not generalize that typo.
                    if area == "(Paul M. Pearson Garden to Bolongo":
                        area += ")"
                    if (not re.fullmatch(r"\([A-Za-z][A-Za-z0-9 .'’/,&-]{1,150}\)", area)
                            or re.search(r"\b(?:Feeder|Outage|AM|PM)\b", area)):
                        raise ValueError("Malformed schedule area")
                    areas.append(area[1:-1])
            start, end = day + timedelta(minutes=start_minute), day + timedelta(minutes=end_minute)
            windows.append({"id": f"wapa-{day:%Y-%m-%d}-{start:%H%M}",
                            "startsAt": start.isoformat(), "endsAt": end.isoformat(),
                            "feeders": feeders, "areas": areas})
        if rows:
            raise ValueError("Unrecognized or extra schedule text")
    if len(windows) != 7:
        raise ValueError("Incomplete daily schedule")
    return {"extractionStatus": "reviewed", "coverageStart": windows[0]["startsAt"],
            "coverageEnd": windows[-1]["endsAt"], "windows": windows}


def _extract_windows(body: bytes) -> dict:
    """Return validated daily windows, or reject unsupported/ambiguous content.

    `reviewed` is the existing schema's validation state, not a claim that this
    revision was manually reviewed. The caller records validated-layout as its
    extraction method. No machine clock, HTTP metadata, or filename dates apply.
    """
    try:
        if not isinstance(body, bytes) or not body.startswith(b"%PDF-") or len(body) > 15_000_000:
            raise ValueError("Invalid schedule PDF")
        reader = PdfReader(BytesIO(body), strict=True)
        if reader.is_encrypted or len(reader.pages) != 2:
            raise ValueError("Only the verified two-page daily layout is supported")
        for page in reader.pages:
            if (list(map(float, page.mediabox)) != [0.0, 7.920007, 810.0, 817.92]
                    or list(page.cropbox) != list(page.mediabox)
                    or page.get("/UserUnit", 1) != 1
                    or page.rotation != 0 or page.get("/Annots")):
                raise ValueError("Unsupported schedule page geometry or annotations")
        return _validate_layout([_positioned_text(page, i) for i, page in enumerate(reader.pages)])
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Unsupported or unreadable schedule PDF") from exc


def extracted_windows(body: bytes) -> dict:
    """Extract under bounded, context-local pypdf resource limits.

    Raster images are never decoded. These limits constrain text/form/font
    stream decompression and PDF parsing before our geometry checks run.
    """
    with apply_configuration(
        maximum_declared_stream_length=8_000_000,
        array_based_stream_maximum_output_length=2_000_000,
        zlib_maximum_output_length=2_000_000,
        lzw_maximum_output_length=2_000_000,
        run_length_maximum_output_length=2_000_000,
        brotli_maximum_output_length=2_000_000,
        stream_decoding_work_maximum_length=8_000_000,
        stream_parsing_work_maximum_count=50_000,
        array_maximum_element_count=10_000,
        dictionary_maximum_element_count=10_000,
        page_tree_maximum_entries=20,
        page_tree_maximum_depth=5,
        page_tree_maximum_page_entries=2_000,
        stream_filters_maximum_length=4,
    ):
        return _extract_windows(body)
