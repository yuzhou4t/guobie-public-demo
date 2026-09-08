from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from app.services.source_probe import ProbeError, SafeHttpClient, normalize_http_url

ROBOTS_USER_AGENT = "GuobieSourceProbe"


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    state: str
    allowed: bool
    code: str
    robots_url: str


def check_robots(client: SafeHttpClient, target_url: str) -> RobotsDecision:
    """Fetch and evaluate robots.txt through the same SSRF-safe HTTP layer."""

    normalized_target = normalize_http_url(target_url)
    parsed = urlsplit(normalized_target)
    robots_url = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    try:
        resource = client.fetch(robots_url)
    except ProbeError as exc:
        if exc.code in {"http_404", "http_410"}:
            return RobotsDecision(
                state="allowed",
                allowed=True,
                code="robots_absent",
                robots_url=robots_url,
            )
        return RobotsDecision(
            state="review_required",
            allowed=False,
            code=f"robots_{exc.code}",
            robots_url=robots_url,
        )

    text = resource.text()
    if "<html" in text[:4096].casefold():
        return RobotsDecision(
            state="review_required",
            allowed=False,
            code="robots_html_response",
            robots_url=robots_url,
        )

    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(text.splitlines())
    allowed = parser.can_fetch(ROBOTS_USER_AGENT, normalized_target)
    return RobotsDecision(
        state="allowed" if allowed else "disallowed",
        allowed=allowed,
        code="robots_allowed" if allowed else "robots_disallowed",
        robots_url=robots_url,
    )
