"""Report possible parcel matches for manual review. Never publishes map pins."""
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .collect import NoRedirect

ESTATES = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/0/query"
PARCELS = "https://services3.arcgis.com/UfiM23HwAqZRk1vw/ArcGIS/rest/services/USVI_PARCELS_GIP/FeatureServer/0/query"
FEED = "https://raw.githubusercontent.com/dezigod/crowdcast-outage-feed/refs/heads/data/outages.json"


def request(url, params=None):
    body = urlencode(params).encode() if params else None
    query = Request(url, data=body, headers={"User-Agent": "CrowdCast parcel review"})
    with build_opener(NoRedirect).open(query, timeout=30) as response:
        payload = response.read(4_000_001)
    if len(payload) > 4_000_000:
        raise ValueError("Parcel review response exceeds size limit")
    result = json.loads(payload)
    if "error" in result or result.get("exceededTransferLimit"):
        raise ValueError("Parcel review source returned an error or incomplete results")
    return result


def inside_ring(x, y, ring):
    crossings = sum(
        (b > y) != (d > y) and x < a + (c - a) * (y - b) / (d - b)
        for (a, b), (c, d) in zip(ring, ring[1:])
    )
    return crossings % 2 == 1


def candidates(geometry, labels, fetch=request):
    polygons = geometry["coordinates"]
    if geometry["type"] == "Polygon":
        polygons = [polygons]
    elif geometry["type"] != "MultiPolygon":
        raise ValueError("Unexpected estate geometry")
    rings = [ring[::-1] for polygon in polygons for ring in polygon]
    params = dict(geometry=json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}}),
                  geometryType="esriGeometryPolygon", spatialRel="esriSpatialRelIntersects",
                  inSR=4326, where="1=1", outFields="PARCEL_NAME,PARCEL_NO,LONGITUDE,LATITUDE",
                  returnGeometry="false", f="json")
    by_name = {}
    for row in fetch(PARCELS, params)["features"]:
        record = row["attributes"]
        name, tax_id = record["PARCEL_NAME"], record["PARCEL_NO"]
        x, y = record["LONGITUDE"], record["LATITUDE"]
        if not name or not tax_id or x is None or y is None:
            continue
        if any(inside_ring(x, y, outer) and not any(inside_ring(x, y, hole) for hole in holes)
               for outer, *holes in polygons):
            by_name.setdefault(name.strip().upper(), set()).add(tax_id)
    return {label: sorted(by_name.get(label.strip().upper(), ())) for label in labels}


def main():
    seed = json.loads(Path(__file__).with_name("parcel-review-seed.json").read_text())
    data = request(ESTATES, dict(where="STATE='78' AND COUNTY='030'", outFields="NAME",
                                outSR=4326, f="geojson", maxAllowableOffset=0.0001))
    estates = {feature["properties"]["NAME"]: feature["geometry"] for feature in data["features"]}
    st_thomas = next(island for island in request(FEED)["islands"] if island["id"] == "stThomas")
    live = {row["name"]: row["locations"] for row in st_thomas["neighborhoods"]}
    lines = ["# St. Thomas parcel review", "",
             "Candidates require human review before a pin can appear in CrowdCast.", ""]
    for name, review in seed.items():
        labels = (set(review["pending"]) | set(live.get(name, []))) - set(review["reviewed"])
        found = candidates(estates[review["estate"]], sorted(labels))
        unique = sum(len(ids) == 1 for ids in found.values())
        ambiguous = sum(len(ids) > 1 for ids in found.values())
        lines += [f"## {name}", "",
                  f"{len(labels)} labels checked · {unique} candidates · {ambiguous} ambiguous", ""]
        for label, ids in found.items():
            if ids:
                safe_label = label.replace("\r", " ").replace("\n", " ").replace("`", "'")
                lines.append(f"- `{safe_label}` · {', '.join(ids)}")
        lines.append("")
    report = "\n".join(lines)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a") as summary:
            summary.write(report)
    else:
        print(report)


if __name__ == "__main__":
    main()
