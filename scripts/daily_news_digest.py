#!/usr/bin/env python3
"""Daily news digest generator for GitHub Actions.

The script reads a private media watchlist, collects recent RSS/Atom items
from those outlets, asks a configured model provider to write an email-ready
digest, and optionally sends it through Gmail SMTP.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import requests

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "sent_items.json"
OUTPUT_DIR = ROOT / "outputs"
LOCAL_CONFIG_PATH = ROOT / "DIGEST_CONFIG_JSON"
LOCAL_TZ = "America/New_York"

DEFAULT_MODEL_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_DIGEST_LANGUAGE = "Simplified Chinese"
DEFAULT_GOOGLE_NEWS_HL = "en-US"
DEFAULT_GOOGLE_NEWS_GL = "US"
DEFAULT_GOOGLE_NEWS_CEID = "US:en"


def sanitize_log_text(text: str) -> str:
    text = normalize_space(text)
    text = re.sub(r"https?://\S+", "[redacted-url]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[redacted-email]", text)
    text = re.sub(r"\bsite:[^\s,)]+", "site:[redacted]", text)
    return text


def safe_exception_label(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return f"{exc.__class__.__name__} status={status}"
    return exc.__class__.__name__


def safe_runtime_error_message(exc: BaseException) -> str:
    if isinstance(exc, RuntimeError):
        return sanitize_log_text(str(exc))
    return safe_exception_label(exc)


def log_info(message: str) -> None:
    print(sanitize_log_text(message))


def log_warning(message: str) -> None:
    print(sanitize_log_text(message), file=sys.stderr)


@dataclass
class Candidate:
    section: str
    category: str
    outlet: str
    title: str
    authors: str
    date: str
    url: str
    summary: str
    why_candidate: str
    source: str
    feed_url: str

    @property
    def key(self) -> str:
        if self.url:
            return "url:" + self.url.lower().strip()
        return self.title_key

    @property
    def title_key(self) -> str:
        return "title:" + normalize_title(self.title)


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value if value else default


def required_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def bool_env(name: str, default: bool = False) -> bool:
    raw = env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def max_candidates_for_model() -> int:
    return int(env("MAX_CANDIDATES_FOR_MODEL", "120"))


def max_email_candidates() -> int:
    return int(env("MAX_EMAIL_CANDIDATES", "90"))


def default_section_candidate_cap() -> int:
    return int(env("DEFAULT_SECTION_CANDIDATE_CAP", "8"))


def max_feed_items_per_outlet() -> int:
    return int(env("MAX_FEED_ITEMS_PER_OUTLET", "25"))


def max_output_tokens() -> int:
    return int(env("MAX_OUTPUT_TOKENS", "9000"))


def model_provider(config: dict[str, Any]) -> str:
    provider = env("MODEL_PROVIDER", str(config.get("model_provider") or DEFAULT_MODEL_PROVIDER))
    provider = normalize_space(provider).lower()
    return "anthropic" if provider == "claude" else provider


def configured_model_name(config: dict[str, Any]) -> str:
    provider = model_provider(config)
    if provider == "openai":
        return env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    if provider == "anthropic":
        return env("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
    return provider


def digest_subject(start_date: str, end_date: str, generated_date: str, model_name: str) -> str:
    return f"Daily News Digest (coverage {start_date} → {end_date}) — generated {generated_date} by {model_name}"


def include_google_news_fallbacks(config: dict[str, Any]) -> bool:
    if "include_google_news_fallbacks" in config:
        return bool(config.get("include_google_news_fallbacks"))
    return bool_env("INCLUDE_GOOGLE_NEWS_FALLBACKS", True)


def load_digest_config() -> dict[str, Any]:
    raw = env("DIGEST_CONFIG_JSON")
    raw_b64 = env("DIGEST_CONFIG_JSON_B64")
    config_path = env("DIGEST_CONFIG_PATH")

    if raw_b64:
        raw = base64.b64decode(raw_b64).decode("utf-8")
    elif config_path:
        raw = Path(config_path).read_text(encoding="utf-8")
    elif not raw and LOCAL_CONFIG_PATH.exists():
        raw = LOCAL_CONFIG_PATH.read_text(encoding="utf-8")

    if not raw:
        raise RuntimeError(
            "Missing digest configuration. Add DIGEST_CONFIG_JSON as a GitHub Secret, "
            "or keep a local DIGEST_CONFIG_JSON file for testing."
        )

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DIGEST_CONFIG_JSON is not valid JSON: {exc}") from exc

    sections = config.get("sections")
    if not isinstance(sections, list) or not sections:
        raise RuntimeError("Digest config must contain a non-empty 'sections' list.")

    return config


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_title(text: str) -> str:
    text = strip_markup(text).lower()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return normalize_space(text)


def strip_markup(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_space(text)


def compact(text: str, limit: int = 700) -> str:
    text = normalize_space(text)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def direct_children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in list(element) if local_name(child.tag) == name]


def first_child(element: ElementTree.Element, names: list[str]) -> ElementTree.Element | None:
    wanted = set(names)
    for child in list(element):
        if local_name(child.tag) in wanted:
            return child
    return None


def child_text(element: ElementTree.Element, names: list[str]) -> str:
    child = first_child(element, names)
    if child is None:
        return ""
    return normalize_space("".join(child.itertext()))


def atom_link(element: ElementTree.Element) -> str:
    for child in direct_children(element, "link"):
        rel = child.attrib.get("rel", "alternate")
        href = child.attrib.get("href", "")
        if href and rel in {"alternate", ""}:
            return href
    child = first_child(element, ["link"])
    if child is not None:
        return child.attrib.get("href", "") or normalize_space("".join(child.itertext()))
    return ""


def item_url(element: ElementTree.Element) -> str:
    link = atom_link(element)
    if link:
        return link
    guid = child_text(element, ["guid", "id"])
    return guid if guid.startswith("http") else ""


def item_authors(element: ElementTree.Element) -> str:
    creators = [normalize_space("".join(child.itertext())) for child in direct_children(element, "creator")]
    if creators:
        return ", ".join([creator for creator in creators if creator])
    author = first_child(element, ["author"])
    if author is None:
        return ""
    name = child_text(author, ["name"])
    return name or normalize_space("".join(author.itertext()))


def parse_datetime(value: str) -> datetime | None:
    value = normalize_space(value)
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass

    normalized = value.replace("Z", "+00:00")
    for candidate in [normalized, normalized[:19], normalized[:10]]:
        try:
            parsed = datetime.fromisoformat(candidate)
            if isinstance(parsed, datetime):
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
        except Exception:
            continue

    return None


def local_timezone() -> timezone | ZoneInfo:
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(LOCAL_TZ)
    except ZoneInfoNotFoundError:
        return timezone.utc


def date_window(start_date: str, end_date: str) -> tuple[datetime, datetime]:
    tz = local_timezone()
    start_local = datetime.combine(date.fromisoformat(start_date), datetime_time.min, tz)
    end_local = datetime.combine(date.fromisoformat(end_date) + timedelta(days=1), datetime_time.min, tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def local_date_text(value: datetime | None, raw_date: str) -> str:
    if value is None:
        return normalize_space(raw_date)
    return value.astimezone(local_timezone()).date().isoformat()


def item_in_window(
    published: datetime | None,
    start_date: str,
    end_date: str,
    include_undated: bool,
) -> bool:
    if published is None:
        return include_undated
    start, end = date_window(start_date, end_date)
    return start <= published < end


def request_text(url: str, email: str, timeout: int = 30) -> str:
    headers = {
        "User-Agent": f"DailyNewsDigest/1.0 (mailto:{email})",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def google_news_feed_url(outlet: dict[str, Any], lookback_days: int) -> str:
    query = normalize_space(str(outlet.get("google_news_query") or ""))
    if not query:
        domain = normalize_space(str(outlet.get("domain") or ""))
        query = f"site:{domain}" if domain else normalize_space(str(outlet.get("name") or ""))
    query = f"{query} when:{max(lookback_days, 1)}d"
    hl = outlet.get("google_news_hl") or DEFAULT_GOOGLE_NEWS_HL
    gl = outlet.get("google_news_gl") or DEFAULT_GOOGLE_NEWS_GL
    ceid = outlet.get("google_news_ceid") or DEFAULT_GOOGLE_NEWS_CEID
    return f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={ceid}"


def feed_urls_for_outlet(
    outlet: dict[str, Any],
    config: dict[str, Any],
    lookback_days: int,
) -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []
    feeds = outlet.get("feeds")
    if isinstance(feeds, list):
        for feed in feeds:
            if isinstance(feed, str):
                urls.append((feed, "RSS/Atom"))
            elif isinstance(feed, dict) and feed.get("url"):
                urls.append((str(feed["url"]), str(feed.get("label") or "RSS/Atom")))
    elif outlet.get("feed_url"):
        urls.append((str(outlet["feed_url"]), "RSS/Atom"))

    if include_google_news_fallbacks(config) and outlet.get("google_news_query") is not False:
        urls.append((google_news_feed_url(outlet, lookback_days), "Google News RSS fallback"))

    return urls


def clean_title_for_outlet(title: str, outlet_name: str) -> str:
    title = strip_markup(title)
    suffixes = [
        f" - {outlet_name}",
        f" | {outlet_name}",
        f" – {outlet_name}",
        f" — {outlet_name}",
    ]
    for suffix in suffixes:
        if title.lower().endswith(suffix.lower()):
            return title[: -len(suffix)].rstrip()
    return title


def parse_feed_items(
    xml_text: str,
    feed_url: str,
    feed_label: str,
    section: str,
    category: str,
    outlet: dict[str, Any],
    start_date: str,
    end_date: str,
) -> list[Candidate]:
    try:
        root = ElementTree.fromstring(xml_text.lstrip())
    except ElementTree.ParseError as exc:
        log_warning(f"Feed parse failed. error={safe_exception_label(exc)}")
        return []

    outlet_name = normalize_space(str(outlet.get("name") or "Unknown outlet"))
    entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    include_undated = bool(outlet.get("include_undated_items", False))
    section_note = normalize_space(str(outlet.get("why") or outlet.get("note") or "Tracked news outlet."))

    out: list[Candidate] = []
    for entry in entries:
        title = clean_title_for_outlet(child_text(entry, ["title"]), outlet_name)
        if not title:
            continue

        raw_date = child_text(entry, ["pubDate", "published", "updated", "date", "created"])
        published = parse_datetime(raw_date)
        if not item_in_window(published, start_date, end_date, include_undated):
            continue

        summary = child_text(entry, ["description", "summary", "subtitle", "encoded", "content"])
        summary = compact(strip_markup(summary), 700)
        date_text = local_date_text(published, raw_date)
        authors = item_authors(entry)

        out.append(
            Candidate(
                section=section,
                category=category,
                outlet=outlet_name,
                title=title,
                authors=authors or "Not listed in feed metadata",
                date=date_text,
                url=item_url(entry),
                summary=summary,
                why_candidate=section_note,
                source=feed_label,
                feed_url=feed_url,
            )
        )

    return out


def fetch_outlet_candidates(
    outlet: dict[str, Any],
    config: dict[str, Any],
    section: str,
    category: str,
    start_date: str,
    end_date: str,
    email: str,
    lookback_days: int,
) -> list[Candidate]:
    outlet_name = outlet.get("name", "Unknown outlet")
    out: list[Candidate] = []
    for feed_url, feed_label in feed_urls_for_outlet(outlet, config, lookback_days):
        try:
            xml_text = request_text(feed_url, email)
        except Exception as exc:
            log_warning(f"Feed request failed. error={safe_exception_label(exc)}")
            continue
        out.extend(
            parse_feed_items(
                xml_text,
                feed_url,
                feed_label,
                section,
                category,
                outlet,
                start_date,
                end_date,
            )
        )
        time.sleep(0.1)

    return out[: int(outlet.get("candidate_cap", max_feed_items_per_outlet()))]


def load_state() -> tuple[set[str], set[str]]:
    if not STATE_PATH.exists():
        return set(), set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set(), set()

    keys = {str(item.get("key", "")) for item in data.get("items", []) if item.get("key")}
    title_keys = {
        str(item.get("title_key", ""))
        for item in data.get("items", [])
        if item.get("title_key")
    }
    return keys, title_keys


def save_state(sent_keys: set[str], sent_title_keys: set[str], new_items: list[Candidate], now_iso: str) -> None:
    existing: dict[str, dict[str, Any]] = {}
    if STATE_PATH.exists():
        try:
            for item in json.loads(STATE_PATH.read_text(encoding="utf-8")).get("items", []):
                if item.get("key"):
                    existing[item["key"]] = item
        except Exception:
            existing = {}

    for candidate in new_items:
        existing[candidate.key] = {
            "key": candidate.key,
            "title_key": candidate.title_key,
            "title": candidate.title,
            "outlet": candidate.outlet,
            "date": candidate.date,
            "sent_at": now_iso,
        }
        sent_keys.add(candidate.key)
        sent_title_keys.add(candidate.title_key)

    trimmed = sorted(existing.values(), key=lambda x: x.get("sent_at", ""), reverse=True)[:2500]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"items": trimmed}, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def dedupe_candidates(
    candidates: list[Candidate],
    sent_keys: set[str],
    sent_title_keys: set[str],
) -> list[Candidate]:
    seen_keys = set()
    seen_titles = set()
    out = []
    for candidate in candidates:
        if not candidate.title:
            continue
        if candidate.key in seen_keys or candidate.key in sent_keys:
            continue
        if candidate.title_key in seen_titles or candidate.title_key in sent_title_keys:
            continue
        seen_keys.add(candidate.key)
        seen_titles.add(candidate.title_key)
        out.append(candidate)
    return out


def candidate_search_text(candidate: Candidate) -> str:
    return " ".join(
        [
            candidate.title,
            candidate.authors,
            candidate.outlet,
            candidate.summary,
            candidate.why_candidate,
            candidate.source,
        ]
    ).lower()


def candidate_matches_any_term(candidate: Candidate, terms: list[str]) -> bool:
    text = candidate_search_text(candidate)
    return any(normalize_space(term).lower() in text for term in terms if normalize_space(term))


def filter_section_candidates(section_config: dict[str, Any], candidates: list[Candidate]) -> list[Candidate]:
    exclude_terms = section_config.get("exclude_terms") or []
    if not isinstance(exclude_terms, list) or not exclude_terms:
        return candidates

    return [
        candidate
        for candidate in candidates
        if not candidate_matches_any_term(candidate, [str(term) for term in exclude_terms])
    ]


def collect_candidates(
    start_date: str,
    end_date: str,
    email: str,
    config: dict[str, Any],
    lookback_days: int,
) -> list[Candidate]:
    candidates: list[Candidate] = []

    for index, section_config in enumerate(config.get("sections", []), start=1):
        section_title = section_config.get("title") or f"Section {index}"
        section_type = section_config.get("type")
        default_category = section_config.get("category") or section_title

        if section_type != "feed_watchlist":
            log_warning(f"Skipping section with unsupported type. type={section_type}")
            continue

        found: list[Candidate] = []
        for outlet in section_config.get("outlets", []):
            if not isinstance(outlet, dict) or not outlet.get("name"):
                continue
            found.extend(
                fetch_outlet_candidates(
                    outlet,
                    config,
                    section_title,
                    default_category,
                    start_date,
                    end_date,
                    email,
                    lookback_days,
                )
            )

        candidates.extend(filter_section_candidates(section_config, found))

    return sort_candidates(candidates)


def sort_candidates(candidates: list[Candidate]) -> list[Candidate]:
    def sort_key(candidate: Candidate) -> tuple[str, str, str]:
        return (candidate.section, candidate.date or "0000-00-00", candidate.outlet)

    return sorted(candidates, key=sort_key, reverse=True)


def section_cap(section_config: dict[str, Any]) -> int:
    if section_config.get("candidate_cap"):
        return int(section_config["candidate_cap"])
    return default_section_candidate_cap()


def limit_candidates_for_model(candidates: list[Candidate], config: dict[str, Any]) -> list[Candidate]:
    total_cap = min(max_candidates_for_model(), max_email_candidates())
    kept: list[Candidate] = []
    omitted_by_section: dict[str, int] = {}

    for section_config in config.get("sections", []):
        section = section_config.get("title", "")
        section_items = [candidate for candidate in candidates if candidate.section == section]
        cap = section_cap(section_config)
        kept.extend(section_items[:cap])
        if len(section_items) > cap:
            omitted_by_section[section] = len(section_items) - cap

    known_sections = {section.get("title", "") for section in config.get("sections", [])}
    uncategorized = [candidate for candidate in candidates if candidate.section not in known_sections]
    remaining = max(total_cap - len(kept), 0)
    kept.extend(uncategorized[:remaining])
    if len(uncategorized) > remaining:
        omitted_by_section["Other"] = len(uncategorized) - remaining

    if len(kept) > total_cap:
        overflow = len(kept) - total_cap
        kept = kept[:total_cap]
        omitted_by_section["Global cap"] = omitted_by_section.get("Global cap", 0) + overflow

    omitted_total = sum(omitted_by_section.values())
    if omitted_total:
        log_info(f"Omitted {omitted_total} candidate records due to digest length caps.")

    return kept


def candidate_payload(candidates: list[Candidate]) -> str:
    payload = []
    for candidate in candidates:
        item = asdict(candidate)
        item["key"] = candidate.key
        item["title_key"] = candidate.title_key
        item["summary"] = compact(item.get("summary", ""), 900)
        payload.append(item)
    return json.dumps(payload, ensure_ascii=True, indent=2)


def build_digest_prompt(
    candidates: list[Candidate],
    start_date: str,
    end_date: str,
    sender: str,
    recipient: str,
    config: dict[str, Any],
) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    language = config.get("digest_language") or DEFAULT_DIGEST_LANGUAGE
    final_title = config.get("final_section_title", "Top Reads Today")
    final_instruction = config.get(
        "final_section_instruction",
        "End with the most important 8 to 10 items to read first.",
    )
    subject = digest_subject(start_date, end_date, today, configured_model_name(config))
    return f"""
You are preparing an email-ready daily news digest.

Hard requirements:
- Write the digest in {language}. Keep outlet names, article titles, organizations, products, laws, and other proper nouns in their original language when that is clearer.
- Use this exact Subject line and do not rewrite it: {subject}
- Use only the candidate records supplied below. Do not invent facts, links, dates, outlets, or article details.
- Treat feed descriptions as metadata only. Paraphrase; do not copy long source descriptions verbatim.
- Include a concise Subject line, From line, and To line.
- Mention the coverage window: {start_date} to {end_date}, generated on {today}.
- Keep the digest useful for a reader who wants broad situational awareness across politics, business, technology, science, health, climate, law, culture, and sports.
- Preserve the section structure from the private digest configuration.
- Follow any digest-level selection or priority policy in the private digest configuration.
- Within each section, group or label items by outlet when useful.
- Include source outlet, date, and link for each item.
- Avoid duplicates across outlets. If several outlets cover the same story, write one compact synthesis and cite the outlets/links that appear in the supplied candidates.
- Prefer concise analytical summaries over raw headline dumps.
- If a section has no supplied records, say briefly that no fresh items were found for that section.
- The final section must be titled "{final_title}" and follow this instruction: {final_instruction}

From: {sender}
To: {recipient}

Private digest configuration:
{json.dumps(config, ensure_ascii=True, indent=2)}

Candidate records:
{candidate_payload(candidates)}
""".strip()


def compose_with_openai(prompt: str) -> str:
    api_key = env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing. Add it to GitHub repository secrets.")

    model = env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    output_tokens = int(env("OPENAI_MAX_OUTPUT_TOKENS", str(max_output_tokens())))
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": prompt,
            "max_output_tokens": output_tokens,
        },
        timeout=120,
    )
    if response.status_code >= 400:
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        suffix = f" request_id={request_id}" if request_id else ""
        raise RuntimeError(f"OpenAI API error {response.status_code}.{suffix}")
    data = response.json()
    if data.get("output_text"):
        return data["output_text"].strip()

    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    text = "\n".join(chunks).strip()
    if not text:
        raise RuntimeError("OpenAI response did not contain output text.")
    return text


def compose_with_anthropic(prompt: str) -> str:
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is missing. Add it to GitHub repository secrets.")

    model = env("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
    output_tokens = int(env("ANTHROPIC_MAX_OUTPUT_TOKENS", str(max_output_tokens())))
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": env("ANTHROPIC_VERSION", DEFAULT_ANTHROPIC_VERSION),
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": output_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    if response.status_code >= 400:
        request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
        suffix = f" request_id={request_id}" if request_id else ""
        raise RuntimeError(f"Anthropic API error {response.status_code}.{suffix}")

    data = response.json()
    chunks = []
    for content in data.get("content", []):
        if content.get("type") == "text" and content.get("text"):
            chunks.append(content["text"])
    text = "\n".join(chunks).strip()
    if not text:
        raise RuntimeError("Anthropic response did not contain output text.")
    return text


def compose_with_model(
    candidates: list[Candidate],
    start_date: str,
    end_date: str,
    sender: str,
    recipient: str,
    config: dict[str, Any],
) -> str:
    prompt = build_digest_prompt(candidates, start_date, end_date, sender, recipient, config)
    provider = model_provider(config)
    if provider == "openai":
        return compose_with_openai(prompt)
    if provider == "anthropic":
        return compose_with_anthropic(prompt)
    raise RuntimeError("Unsupported MODEL_PROVIDER. Use 'openai' or 'anthropic'.")


def fallback_digest(
    candidates: list[Candidate],
    start_date: str,
    end_date: str,
    sender: str,
    recipient: str,
    config: dict[str, Any],
) -> str:
    generated_date = datetime.now(timezone.utc).date().isoformat()
    subject = digest_subject(start_date, end_date, generated_date, configured_model_name(config))
    no_items = "没有抓到新的候选新闻。"
    fallback_note = "AI 总结暂不可用，请先查看上面的候选新闻。"
    lines = [
        f"Subject: {subject}",
        f"From: {sender}",
        f"To: {recipient}",
        "",
        f"Coverage window: {start_date} to {end_date}.",
    ]
    for section_config in config.get("sections", []):
        section = section_config.get("title", "Section")
        lines.extend(["", section, ""])
        items = [c for c in candidates if c.section == section]
        if not items:
            lines.append(no_items)
            continue
        for c in items[: section_cap(section_config)]:
            note = compact(c.summary or c.why_candidate, 220)
            lines.append(f"- [{c.outlet}] {c.title}. {c.date}. {c.url}. Note: {note}")
    final_title = config.get("final_section_title", "Top Reads Today")
    lines.extend(["", final_title, "", fallback_note])
    return "\n".join(lines)


def extract_subject(body: str, start_date: str, end_date: str) -> str:
    for line in body.splitlines()[:8]:
        if line.lower().startswith("subject:"):
            subject = normalize_space(line.split(":", 1)[1])
            if subject:
                return subject
    return f"Daily News Digest - {start_date} to {end_date}"


def strip_subject_line(body: str) -> str:
    lines = body.splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        return "\n".join(lines[1:]).lstrip()
    return body


def send_email(subject: str, body: str, sender: str, recipient: str, app_password: str) -> None:
    password = app_password.replace(" ", "").strip()
    if not password:
        raise RuntimeError("GMAIL_APP_PASSWORD is missing. Add it to GitHub repository secrets.")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = sender
    msg["To"] = recipient

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(sender, password)
        smtp.sendmail(sender, [recipient], msg.as_string())


def should_run_time_gate() -> bool:
    now = datetime.now(local_timezone())
    target_hour = int(env("TARGET_LOCAL_HOUR", "5"))
    target_minute = int(env("TARGET_LOCAL_MINUTE", "30"))
    event_schedule = normalize_space(env("GITHUB_EVENT_SCHEDULE") or env("GITHUB_SCHEDULE"))

    if event_schedule:
        target_local = datetime.combine(
            now.date(),
            datetime_time(target_hour, target_minute),
            now.tzinfo,
        )
        target_utc = target_local.astimezone(timezone.utc)
        expected_schedule = f"{target_utc.minute} {target_utc.hour} * * *"
        return event_schedule == expected_schedule

    return now.hour == target_hour


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Send the digest by Gmail SMTP.")
    parser.add_argument("--time-gate", action="store_true", help="Only run during the target local hour.")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(env("LOOKBACK_DAYS", "0")),
        help="How many previous local calendar days to include. Use 0 for today only.",
    )
    parser.add_argument("--allow-fallback", action="store_true", help="Send a metadata-only digest if OpenAI is unavailable.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.time_gate and not should_run_time_gate():
        log_info(f"Skipping: current {LOCAL_TZ} time is outside the target local hour.")
        return 0

    sender = required_env("GMAIL_ADDRESS")
    recipient = required_env("DIGEST_RECIPIENT")
    config = load_digest_config()
    now_local = datetime.now(ZoneInfo(LOCAL_TZ)) if ZoneInfo else datetime.now()
    end = now_local.date()
    lookback_days = max(args.lookback_days, 0)
    start = end - timedelta(days=lookback_days)
    start_date = start.isoformat()
    end_date = end.isoformat()

    sent_keys, sent_title_keys = load_state()
    candidates = dedupe_candidates(
        collect_candidates(start_date, end_date, sender, config, max(lookback_days, 1)),
        sent_keys,
        sent_title_keys,
    )
    candidates = limit_candidates_for_model(candidates, config)
    log_info(f"Collected {len(candidates)} unsent candidate records for {start_date} to {end_date}.")

    try:
        body = compose_with_model(candidates, start_date, end_date, sender, recipient, config)
    except Exception as exc:
        if not args.allow_fallback:
            raise
        log_warning(f"AI summarization unavailable, using fallback digest. error={safe_runtime_error_message(exc)}")
        body = fallback_digest(candidates, start_date, end_date, sender, recipient, config)

    generated_date = datetime.now(timezone.utc).date().isoformat()
    subject = digest_subject(start_date, end_date, generated_date, configured_model_name(config))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"daily_news_digest_{end_date}.txt"
    output_path.write_text(f"Subject: {subject}\n{strip_subject_line(body)}\n", encoding="utf-8")
    log_info("Wrote digest output file.")

    if args.send:
        send_email(subject, strip_subject_line(body), sender, recipient, env("GMAIL_APP_PASSWORD"))
        log_info("Sent digest email.")
        save_state(sent_keys, sent_title_keys, candidates, datetime.now(timezone.utc).isoformat())
    else:
        log_info("Dry run only; email not sent.")

    return 0


def cli() -> int:
    try:
        return main()
    except Exception as exc:
        log_warning(f"Run failed. error={safe_runtime_error_message(exc)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
