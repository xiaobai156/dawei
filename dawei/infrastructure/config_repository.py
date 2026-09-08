"""Load, validate, migrate, and atomically save site configuration."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dawei.domain.errors import ConfigurationError, ValidationError
from dawei.domain.models import SiteConfig, derive_site_id
from dawei.domain.validation import validate_site_config

from .cache_repository import atomic_write_text

DYNAMIC_RECORD_RE = re.compile(
    r"/article/(?:admin|manager|lottery)/(?P<record_id>[0-9a-z]+)(?:[/?#]|$)",
    re.IGNORECASE,
)
SPA_USER_RE = re.compile(r"#/users/(?P<record_id>\d+)", re.IGNORECASE)
API_RECORD_RE = re.compile(
    r"/(?:(?:admin|manager)-articles?|articles?|article)/(?P<record_id>[0-9a-z]+)$",
    re.IGNORECASE,
)
ALLOWED_SOURCE_TYPES = frozenset(
    {
        "generic_html",
        "topic_page",
        "bbs_topic",
        "forum_thread",
        "dynamic_article",
        "dynamic_collection",
        "paginated_article_list",
    }
)


def normalize_url_identity(value: str) -> str:
    """Canonicalize only URL spelling, never the URL's target identity."""
    text = unicodedata.normalize("NFKC", value).strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.casefold()
    if not parts.scheme or not parts.netloc:
        return text.casefold()
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = urlencode(sorted(query_pairs))
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, query, parts.fragment)
    )


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def config_fingerprint(sites: tuple[SiteConfig, ...] | list[SiteConfig]) -> str:
    """Return a stable fingerprint for the ordered, effective site contract."""
    canonical: list[dict[str, object]] = []
    for site in sites:
        item = asdict(site)
        item["name"] = _normalize_text(site.name)
        item["url"] = normalize_url_identity(site.url)
        item["api_url"] = normalize_url_identity(site.api_url) if site.api_url else None
        item["direction"] = site.direction
        for key in ("keywords", "section_keywords", "navigation_keywords"):
            item[key] = [_normalize_text(str(value)) for value in getattr(site, key)]
        canonical.append(item)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_identity_collisions(sites: tuple[SiteConfig, ...]) -> None:
    names: dict[str, SiteConfig] = {}
    targets: dict[tuple[object, ...], SiteConfig] = {}
    apis: dict[tuple[str, str], SiteConfig] = {}
    record_ids: dict[str, SiteConfig] = {}
    for site in sites:
        name_key = _normalize_text(site.name)
        previous = names.get(name_key)
        if previous is not None:
            raise ConfigurationError(
                f"duplicate site name identity: {previous.name} / {site.name}"
            )
        names[name_key] = site
        url_record_id = infer_record_id(site.url)
        if site.source_type == "dynamic_article":
            if not url_record_id or site.record_id != url_record_id:
                raise ConfigurationError(
                    f"dynamic article record_id does not match URL: {site.name}"
                )
            if not site.api_url:
                raise ConfigurationError(f"dynamic article requires api_url: {site.name}")
            api_match = API_RECORD_RE.search(urlsplit(site.api_url).path.rstrip("/"))
            if api_match is None or api_match.group("record_id") != site.record_id:
                raise ConfigurationError(
                    f"dynamic article API record_id does not match URL: {site.name}"
                )
        if site.record_id:
            previous = record_ids.get(site.record_id)
            if previous is not None:
                raise ConfigurationError(
                    f"duplicate dynamic record_id identity: {previous.name} / {site.name}"
                )
            record_ids[site.record_id] = site
        target_key = (
            normalize_url_identity(site.url),
            site.record_id or "",
            normalize_url_identity(site.api_url) if site.api_url else "",
            tuple(_normalize_text(value) for value in site.section_keywords),
            tuple(_normalize_text(value) for value in site.keywords),
            site.parser_id,
            site.source_type,
            site.direction,
        )
        previous = targets.get(target_key)
        if previous is not None:
            raise ConfigurationError(
                f"duplicate site target identity: {previous.name} / {site.name}"
            )
        targets[target_key] = site
        if site.api_url and site.record_id:
            api_key = (normalize_url_identity(site.api_url), site.record_id)
            previous = apis.get(api_key)
            if previous is not None:
                raise ConfigurationError(
                    f"duplicate dynamic API identity: {previous.name} / {site.name}"
                )
            apis[api_key] = site


def _validate_site_safety(site: SiteConfig, index: int) -> None:
    if site.source_type not in ALLOWED_SOURCE_TYPES:
        raise ConfigurationError(
            f"sites config item #{index} has unsupported source_type: {site.source_type}"
        )
    if site.record_id is not None and not isinstance(site.record_id, str):
        raise ConfigurationError(f"sites config item #{index} record_id must be a string")
    if site.source_type == "dynamic_collection":
        if not site.api_url:
            raise ConfigurationError(
                f"sites config item #{index} dynamic_collection requires api_url"
            )
        if site.parser_id == "legacy":
            raise ConfigurationError(
                f"sites config item #{index} dynamic_collection requires explicit parser_id"
            )
    for field in ("url", "api_url"):
        value = getattr(site, field)
        if value is None:
            continue
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ConfigurationError(
                f"sites config item #{index} field {field} is not a valid URL"
            ) from exc
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError(
                f"sites config item #{index} field {field} must be an http/https URL"
            )
    if site.fixed_issue is not None and (
        isinstance(site.fixed_issue, bool)
        or not isinstance(site.fixed_issue, int)
        or site.fixed_issue <= 0
    ):
        raise ConfigurationError(
            f"sites config item #{index} fixed_issue must be a positive integer"
        )


def infer_source_type(url: str) -> str:
    if DYNAMIC_RECORD_RE.search(url):
        return "dynamic_article"
    if SPA_USER_RE.search(url):
        return "dynamic_collection"
    if "/bbs/topic.php" in url:
        return "bbs_topic"
    if "/topic/" in url:
        return "topic_page"
    if "read.php" in url:
        return "forum_thread"
    return "generic_html"


def infer_record_id(url: str) -> str | None:
    match = DYNAMIC_RECORD_RE.search(url) or SPA_USER_RE.search(url)
    return match.group("record_id") if match else None


def migrate_site_mapping(item: object, index: int) -> SiteConfig:
    if not isinstance(item, dict):
        raise ConfigurationError(f"sites config item #{index} is not an object")
    allowed = set(SiteConfig.__dataclass_fields__)
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise ConfigurationError(
            f"sites config item #{index} has unknown fields: {', '.join(unknown)}"
        )
    data: dict[str, Any] = dict(item)
    for key in ("keywords", "section_keywords", "navigation_keywords"):
        if key in data:
            if not isinstance(data[key], list):
                raise ConfigurationError(f"sites config item #{index} field {key} must be a list")
            if any(type(value) is not str for value in data[key]):
                raise ConfigurationError(
                    f"sites config item #{index} field {key} must contain strings"
                )
            data[key] = tuple(data[key])
    for key in ("onboarding_valid_issues", "onboarding_missing_issues"):
        if key in data:
            if not isinstance(data[key], list):
                raise ConfigurationError(f"sites config item #{index} field {key} must be a list")
            if any(type(value) is not int for value in data[key]):
                raise ConfigurationError(
                    f"sites config item #{index} field {key} must contain integers"
                )
            data[key] = tuple(data[key])
    name = data.get("name")
    url = data.get("url")
    if not isinstance(name, str) or not isinstance(url, str):
        raise ConfigurationError(f"sites config item #{index} requires string name and url")
    if "source_type" in data:
        if type(data["source_type"]) is not str:
            raise ConfigurationError(
                f"sites config item #{index} field source_type must be a string"
            )
        source_type = data["source_type"]
    else:
        source_type = infer_source_type(url)
    data["source_type"] = source_type
    if "site_id" in data:
        if type(data["site_id"]) is not str:
            raise ConfigurationError(
                f"sites config item #{index} field site_id must be a string"
            )
    else:
        data["site_id"] = derive_site_id(name, url)
    if "parser_id" in data:
        if type(data["parser_id"]) is not str:
            raise ConfigurationError(
                f"sites config item #{index} field parser_id must be a string"
            )
    else:
        data["parser_id"] = "legacy"
    if "record_id" in data:
        if data["record_id"] is not None and type(data["record_id"]) is not str:
            raise ConfigurationError(
                f"sites config item #{index} field record_id must be a string or null"
            )
    else:
        data["record_id"] = infer_record_id(url)
    if "region" in data:
        if type(data["region"]) is not str or data["region"] not in {"top", "bottom"}:
            raise ConfigurationError(
                f"sites config item #{index} field region must be exactly top or bottom"
            )
    else:
        is_tuku2135 = (
            data.get("image_decoder") == "tuku2135_ocr"
            or data.get("parser_id") == "image_tuku2135"
        )
        if is_tuku2135 and data.get("position") == "none":
            data["region"] = None
        else:
            data["region"] = "top" if data.get("position") == "top" else "bottom"
    if "render_policy" in data:
        if type(data["render_policy"]) is not str or not data["render_policy"].strip():
            raise ConfigurationError(
                f"sites config item #{index} field render_policy must be a non-empty string"
            )
    else:
        if data.get("render_browser"):
            data["render_policy"] = "always"
        elif source_type == "dynamic_article":
            data["render_policy"] = "fallback"
        else:
            data["render_policy"] = "never"
    try:
        site = validate_site_config(SiteConfig(**data))
    except (TypeError, ValidationError) as exc:
        raise ConfigurationError(f"sites config item #{index} is invalid: {exc}") from exc
    _validate_site_safety(site, index)
    return site


class ConfigRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> tuple[SiteConfig, ...]:
        if not self.path.exists():
            raise ConfigurationError(f"sites config not found: {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"failed to load sites config {self.path}: {exc}") from exc
        if not isinstance(payload, list):
            raise ConfigurationError(f"sites config {self.path} must contain a list")
        sites = tuple(migrate_site_mapping(item, index + 1) for index, item in enumerate(payload))
        if not sites:
            raise ConfigurationError(f"sites config {self.path} contains no sites")
        duplicate_ids = sorted(
            site_id for site_id in {site.site_id for site in sites} if sum(s.site_id == site_id for s in sites) > 1
        )
        if duplicate_ids:
            raise ConfigurationError("duplicate site_id: " + ", ".join(duplicate_ids))
        _validate_identity_collisions(sites)
        return sites

    def save(self, sites: tuple[SiteConfig, ...]) -> None:
        if not sites:
            raise ConfigurationError("refusing to save an empty sites config")
        validated = tuple(validate_site_config(site) for site in sites)
        duplicate_ids = sorted(
            site_id
            for site_id in {site.site_id for site in validated}
            if sum(site.site_id == site_id for site in validated) > 1
        )
        if duplicate_ids:
            raise ConfigurationError("duplicate site_id: " + ", ".join(duplicate_ids))
        for index, site in enumerate(validated, start=1):
            _validate_site_safety(site, index)
        _validate_identity_collisions(validated)
        payload = []
        for site in validated:
            item = asdict(site)
            item["keywords"] = list(site.keywords)
            item["section_keywords"] = list(site.section_keywords)
            payload.append(item)
        atomic_write_text(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8-sig",
        )

    def migrate(self) -> tuple[SiteConfig, ...]:
        sites = self.load()
        self.save(sites)
        return sites

    def replace(self, site_id: str, **changes: object) -> SiteConfig:
        sites = list(self.load())
        matches = [index for index, site in enumerate(sites) if site.site_id == site_id]
        if len(matches) != 1:
            raise ConfigurationError(f"site_id must match exactly one site: {site_id}")
        updated = replace(sites[matches[0]], **changes)
        sites[matches[0]] = validate_site_config(updated)
        self.save(tuple(sites))
        return updated
