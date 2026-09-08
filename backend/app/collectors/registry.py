from __future__ import annotations

from app.collectors.base import Collector
from app.collectors.html import HtmlCollector
from app.collectors.json_api import JsonApiCollector
from app.collectors.pdf import PdfCollector
from app.collectors.rss import RssCollector

_COLLECTORS: dict[str, Collector] = {
    "rss": RssCollector(),
    "api": JsonApiCollector(),
    "html": HtmlCollector(),
    "pdf": PdfCollector(),
}


def get_collector(collector_type: str) -> Collector:
    try:
        return _COLLECTORS[collector_type]
    except KeyError as exc:
        raise ValueError(f"unsupported collector type: {collector_type}") from exc
