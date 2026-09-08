from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.services.source_probe import SafeHttpClient

ASSET_DIR = Path(__file__).resolve().parents[1] / "static" / "reader" / "assets"
LAYER_SPECS = {
    "ADM2": {
        "url": (
            "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/"
            "releaseData/gbOpen/COD/ADM2/geoBoundaries-COD-ADM2_simplified.geojson"
        ),
        "filename": "cod-adm2.geojson",
        "expected_count": 189,
    },
    "ADM3": {
        "url": (
            "https://github.com/wmgeolab/geoBoundaries/raw/"
            "7278f6e1f5b609a1bab4f959015f040b809a5ac5/releaseData/gbOpen/COD/ADM3/"
            "geoBoundaries-COD-ADM3_simplified.geojson"
        ),
        "filename": "cod-adm3.geojson",
        "expected_count": 188,
    },
}


def _coordinate_count(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
        longitude, latitude = float(value[0]), float(value[1])
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError("coordinate falls outside longitude/latitude bounds")
        return 1
    return sum(_coordinate_count(item) for item in value)


def _validated_geojson(body: bytes, *, level: str, expected_count: int) -> bytes:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{level} download is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ValueError(f"{level} download is not a GeoJSON FeatureCollection")
    features = payload.get("features")
    if not isinstance(features, list) or len(features) != expected_count:
        raise ValueError(
            f"{level} expected {expected_count} units, received "
            f"{len(features) if isinstance(features, list) else 'invalid'}"
        )
    coordinate_count = 0
    for index, feature in enumerate(features, start=1):
        if not isinstance(feature, dict):
            raise ValueError(f"{level} feature {index} is invalid")
        geometry = feature.get("geometry")
        properties = feature.get("properties")
        if not isinstance(geometry, dict) or geometry.get("type") not in {
            "Polygon",
            "MultiPolygon",
        }:
            raise ValueError(f"{level} feature {index} has an unsupported geometry")
        if not isinstance(properties, dict) or not str(properties.get("shapeName") or "").strip():
            raise ValueError(f"{level} feature {index} has no shapeName")
        coordinate_count += _coordinate_count(geometry.get("coordinates"))
    if coordinate_count < expected_count * 4:
        raise ValueError(f"{level} geometry contains too few coordinates")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    client = SafeHttpClient()
    for level, spec in LAYER_SPECS.items():
        resource = client.fetch_geojson(spec["url"])
        body = _validated_geojson(
            resource.body,
            level=level,
            expected_count=spec["expected_count"],
        )
        destination = ASSET_DIR / spec["filename"]
        _atomic_write(destination, body)
        digest = hashlib.sha256(body).hexdigest()
        print(
            f"{level} {len(json.loads(body)['features'])} units {len(body)} bytes "
            f"upstream_sha256={resource.sha256} sha256={digest}"
        )


if __name__ == "__main__":
    main()
