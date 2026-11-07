import unittest

from outage_backend.parcel_review import candidates


class ParcelReviewTests(unittest.TestCase):
    def test_reports_ambiguity_and_avoids_similar_labels_or_outside_estates(self):
        geometry = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}

        def row(name, parcel, x=0.5):
            return {"attributes": {"PARCEL_NAME": name, "PARCEL_NO": parcel,
                                   "LONGITUDE": x, "LATITUDE": 0.5}}

        def fetch(url, params):
            return {"features": [row("5B", "1"), row("5B", "1"), row("B-12", "2"),
                                 row("B-12", "3"), row("4-2", "4"), row("5F", "5", x=2)]}

        self.assertEqual(candidates(geometry, ["5B", "B-12", "42", "5F"], fetch),
                         {"5B": ["1"], "B-12": ["2", "3"], "42": [], "5F": []})
