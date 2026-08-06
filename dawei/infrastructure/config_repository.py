"""Load, validate, migrate, and atomically save site configuration."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import re
from typing import Any

from dawei.domain.errors import ConfigurationError, ValidationError
from dawei.domain.models import SiteConfig, derive_site_id
from dawei.domain.validation import validate_site_config

from .cache_repository import atomic_write_text


DYNAMIC_RECORD_RE = re.compile(
    r"/article/(?:admin|manager|lottery)/(?P<record_id>[0-9a-z]+)",
    re.IGNORECASE,
)
SPA_USER_RE = re.compile(r"#/users/(?P<record_id>\d+)", re.IGNORECASE)


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
            data[key] = tuple(str(value) for value in data[key])
    for key in ("onboarding_valid_issues", "onboarding_missing_issues"):
        if key in data:
            if not isinstance(data[key], list):
                raise ConfigurationError(f"sites config item #{index} field {key} must be a list")
            try:
                data[key] = tuple(int(value) for value in data[key])
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"sites config item #{index} field {key} must contain integers"
                ) from exc
    name = data.get("name")
    url = data.get("url")
    if not isinstance(name, str) or not isinstance(url, str):
        raise ConfigurationError(f"sites config item #{index} requires string name and url")
    source_type = str(data.get("source_type") or infer_source_type(url))
    data["source_type"] = source_type
    data["site_id"] = str(data.get("site_id") or derive_site_id(name, url))
    data["parser_id"] = str(data.get("parser_id") or "legacy")
    data["record_id"] = data.get("record_id") or infer_record_id(url)
    if not data.get("region"):
        data["region"] = "top" if data.get("position") == "top" else "bottom"
    if not data.get("render_policy"):
        if data.get("render_browser"):
            data["render_policy"] = "always"
        elif source_type == "dynamic_article":
            data["render_policy"] = "fallback"
        else:
            data["render_policy"] = "never"
    try:
        return validate_site_config(SiteConfig(**data))
    except (TypeError, ValidationError) as exc:
        raise ConfigurationError(f"sites config item #{index} is invalid: {exc}") from exc


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
