
import base64
from typing import Optional


# Helper function to parse tus Upload-Metadata header

def parse_tus_metadata(metadata_header: Optional[str]) -> dict:
    if not metadata_header:
        return {}
    metadata = {}
    for pair in metadata_header.split(","):
        parts = pair.strip().split(" ")
        key = parts[0]
        if len(parts) > 1:
            try:
                value = base64.b64decode(parts[1]).decode("utf-8")
            except Exception:
                value = ""
        else:
            value = ""
        metadata[key] = value
    return metadata
