"""Synthetic geometry fixtures only; no copy of WAPA's PDF is stored."""
from copy import deepcopy
from io import BytesIO
import unittest

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           FloatObject, NameObject, NumberObject)

from outage_backend.rotation_pdf import (_COLUMNS, _STATIC, _font_size,
                                         extracted_windows)


def fixture(date="NOVEMBER 9, 2026"):
    pages, slot = [], 0
    for columns in _COLUMNS:
        rows = list(_STATIC) + [(173.6, 567.2, date)]
        for start, end, heading, groups in columns:
            def clock(minutes):
                hour, minute = divmod(minutes, 60)
                return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"
            rows += [(*start, clock(510 + 120 * slot) + " -"),
                     (*end, clock(630 + 120 * slot)), (*heading, "Outage:")]
            for group_index, (feeder, areas) in enumerate(groups):
                rows.append((*feeder, f"Feeder {slot + 1}{'AB'[group_index]}"))
                if len(areas) == 2:
                    rows.extend([(*areas[0], f"(Area {slot + 1} to"),
                                 (*areas[1], "Example Bay)")])
                elif areas:
                    rows.append((*areas[0], f"(Area {slot + 1})"))
            slot += 1
        pages.append(rows)
    return pages


def pdf_bytes(pages, nested=False):
    """Minimal actual PDFs exercise parsing, parent matrices, and layout checks."""
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    resources = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    for page_number, rows in enumerate(pages):
        page = writer.add_blank_page(width=810, height=810)
        page[NameObject('/MediaBox')] = ArrayObject([FloatObject(n) for n in (0, 7.920007, 810, 817.92)])
        page[NameObject('/Resources')] = resources
        lines = []
        for x, y, text in reversed(rows):  # Deliberately non-reading-order streams.
            size = _font_size(x, y, text, page_number)
            if nested:
                x, y = (x - 20) / 2, (y - 30) / 2
                size /= 2
            escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            lines.append(f'BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET')
        content = DecodedStreamObject()
        content.set_data('\n'.join(lines).encode('ascii'))
        if nested:
            content[NameObject('/Type')] = NameObject('/XObject')
            content[NameObject('/Subtype')] = NameObject('/Form')
            content[NameObject('/BBox')] = ArrayObject([NumberObject(n) for n in (0, 0, 810, 810)])
            content[NameObject('/Resources')] = resources
            content[NameObject('/Matrix')] = ArrayObject([NumberObject(n) for n in (1, 0, 0, 1, 5, 7)])
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject({NameObject('/X1'): writer._add_object(content)})})
            # Form Matrix supplies 10/14 of the final translation.
            content = DecodedStreamObject()
            content.set_data(b'q 2 0 0 2 10 16 cm /X1 Do Q')
        page[NameObject('/Contents')] = writer._add_object(content)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


class RotationPdfTests(unittest.TestCase):
    def test_actual_pdf_and_column_association(self):
        result = extracted_windows(pdf_bytes(fixture()))
        self.assertEqual(result['coverageStart'], '2026-11-09T08:30:00-04:00')
        self.assertEqual(result['coverageEnd'], '2026-11-09T22:30:00-04:00')
        self.assertEqual(len(result['windows']), 7)
        self.assertEqual(result['windows'][0]['feeders'], ['Feeder 1A', 'Feeder 1B'])
        self.assertEqual(result['windows'][3]['feeders'], ['Feeder 4A'])
        self.assertEqual(result['windows'][6]['feeders'], ['Feeder 7A', 'Feeder 7B'])
        self.assertEqual(result['windows'][2]['areas'], ['Area 3 to Example Bay', 'Area 3'])

    def test_nested_form_parent_and_form_transforms(self):
        self.assertEqual(extracted_windows(pdf_bytes(fixture(), nested=True)),
                         extracted_windows(pdf_bytes(fixture())))

    def test_printed_date_changes_automatically(self):
        result = extracted_windows(pdf_bytes(fixture('NOVEMBER 10, 2026')))
        self.assertEqual(result['coverageStart'], '2026-11-10T08:30:00-04:00')
        self.assertEqual(result['windows'][0]['id'], 'wapa-2026-11-10-0830')

    def test_year_and_month_rollover_do_not_use_clock(self):
        for title, expected in [('DECEMBER 31, 2026', '2026-12-31'),
                                ('JANUARY 1, 2027', '2027-01-01'),
                                ('FEBRUARY 29, 2028', '2028-02-29')]:
            with self.subTest(title=title):
                result = extracted_windows(pdf_bytes(fixture(title)))
                self.assertTrue(result['coverageEnd'].startswith(expected))

    def test_noon_boundary_is_pm_not_midnight(self):
        result = extracted_windows(pdf_bytes(fixture()))
        self.assertEqual(result['windows'][1]['endsAt'], '2026-11-09T12:30:00-04:00')
        self.assertEqual(result['windows'][2]['startsAt'], result['windows'][1]['endsAt'])

    def test_missing_malformed_or_partial_date(self):
        for title in ['NOVEMBER 9', 'NOVEMBER 31, 2026', 'FEBRUARY 29, 2027',
                      'NOV 9, 2026', 'NOVEMBER 9, 26', 'NOVEMBER 9-10, 2026']:
            with self.subTest(title=title), self.assertRaises(ValueError):
                extracted_windows(pdf_bytes(fixture(title)))

    def test_page_dates_must_match(self):
        pages = fixture()
        pages[1] = fixture('NOVEMBER 10, 2026')[1]
        with self.assertRaises(ValueError):
            extracted_windows(pdf_bytes(pages))

    def test_duplicate_date_and_extra_headers_are_ambiguous(self):
        for extra in [(173.6, 567.2, 'NOVEMBER 9, 2026'), (300, 550, 'NOVEMBER 10, 2026')]:
            pages = fixture()
            pages[0].append(extra)
            with self.assertRaises(ValueError):
                extracted_windows(pdf_bytes(pages))

    def test_missing_column_or_extra_column_rejected(self):
        pages = fixture()
        for delta in ('remove', 'duplicate'):
            altered = deepcopy(pages)
            row = next(row for row in altered[0] if row[2] == 'Feeder 4A')
            if delta == 'remove':
                altered[0].remove(row)
            else:
                altered[0].append(row)
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                extracted_windows(pdf_bytes(altered))

    def test_moving_feeder_to_another_column_does_not_attach_it(self):
        pages = fixture()
        for i, (x, y, text) in enumerate(pages[0]):
            if text == 'Feeder 1A':
                pages[0][i] = (x + 180, y, text)
        with self.assertRaises(ValueError):
            extracted_windows(pdf_bytes(pages))

    def test_changed_or_malformed_time_rejected(self):
        for time in ['8:60 AM -', '8:30 PM -', '9:30 AM -', '08:30 AM -', '8:30 AM']:
            pages = fixture()
            pages[0] = [(x, y, time if text == '8:30 AM -' else text) for x, y, text in pages[0]]
            with self.subTest(time=time), self.assertRaises(ValueError):
                extracted_windows(pdf_bytes(pages))

    def test_missing_outage_heading_rejected(self):
        pages = fixture()
        pages[0] = [(x, y, 'Restoration:' if text == 'Outage:' else text) for x, y, text in pages[0]]
        with self.assertRaises(ValueError):
            extracted_windows(pdf_bytes(pages))

    def test_unsupported_page_count_and_non_pdf(self):
        for body in [b'not a pdf', pdf_bytes(fixture()[:1]), pdf_bytes(fixture() * 2)]:
            with self.assertRaises(ValueError):
                extracted_windows(body)

    def test_compressed_stream_bomb_is_rejected(self):
        writer = PdfWriter()
        for _ in range(2):
            page = writer.add_blank_page(width=810, height=810)
            page[NameObject('/MediaBox')] = ArrayObject([FloatObject(n) for n in (0, 7.920007, 810, 817.92)])
            page[NameObject('/Resources')] = DictionaryObject()
            stream = DecodedStreamObject()
            stream.set_data(b' ' * 2_100_000)
            page[NameObject('/Contents')] = writer._add_object(stream.flate_encode())
        out = BytesIO()
        writer.write(out)
        self.assertLess(len(out.getvalue()), 20_000)
        with self.assertRaises(ValueError):
            extracted_windows(out.getvalue())

    def test_wrong_page_geometry(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.add_blank_page(width=612, height=792)
        out = BytesIO()
        writer.write(out)
        with self.assertRaises(ValueError):
            extracted_windows(out.getvalue())


if __name__ == '__main__':
    unittest.main()
