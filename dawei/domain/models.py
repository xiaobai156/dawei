"""Immutable domain models used across V2 layers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import unicodedata


DEFAULT_KEYWORDS = (
    "爆稳36码",
    "精准36码",
    "无错36码",
    "特围36码",
    "围特36码",
    "36码围特",
    "36码特围",
    "36码中特",
    "36码中",
)


def derive_site_id(name: str, url: str) -> str:
    """Create a deterministic identity only for one-time V1 migration."""
    normalized_name = unicodedata.normalize("NFKC", name).strip().casefold()
    normalized_url = unicodedata.normalize("NFKC", url).strip()
    digest = hashlib.sha256(f"{normalized_name}\n{normalized_url}".encode("utf-8")).hexdigest()
    return f"site_{digest[:20]}"


@dataclass(frozen=True)
class SiteConfig:
    name: str
    url: str
    keywords: tuple[str, ...] = DEFAULT_KEYWORDS
    section_keywords: tuple[str, ...] = ()
    search_window: int = 260
    fixed_issue: int | None = None
    api_url: str | None = None
    min_numbers_per_line: int = 2
    image_decoder: str | None = None
    numbers_before_issue: bool = False
    drop_zero_numbers: bool = False
    position: str = "tail"
    region: str | None = None
    render_browser: bool = False
    site_id: str = ""
    source_type: str = "generic_html"
    parser_id: str = "legacy"
    record_id: str | None = None
    render_policy: str = "never"
    navigation_keywords: tuple[str, ...] = ()
    onboarding_exception: str | None = None
    onboarding_valid_issues: tuple[int, ...] = ()
    onboarding_missing_issues: tuple[int, ...] = ()

    @property
    def direction(self) -> str:
        value = self.region or self.position
        if value in {"top", "顶部", "上"}:
            return "top"
        if value in {"bottom", "tail", "尾部", "底部", "下"}:
            return "bottom"
        return value


@dataclass(frozen=True)
class RawAnchor:
    text: str
    href: str


@dataclass(frozen=True)
class RawDocument:
    url: str
    text: str
    links: tuple[RawAnchor, ...]


@dataclass(frozen=True)
class CandidateOrigin:
    raw_issue_line: str
    raw_number_lines: tuple[str, ...]
    anchor_line: str
    document_id: str
    document_url: str
    source_method: str
    block_id: str
    block_start: int
    block_end: int
    page_index: int
    block_index: int
    parser_id: str


@dataclass(frozen=True)
class CandidateEvidence:
    issue: int
    numbers: tuple[str, ...]
    raw_issue_line: str
    raw_number_lines: tuple[str, ...]
    anchor_line: str
    document_id: str
    document_url: str
    source_method: str
    block_id: str
    block_start: int
    block_end: int
    page_index: int
    block_index: int
    parser_id: str
    origins: tuple[CandidateOrigin, ...] = ()


@dataclass(frozen=True)
class ParsedRecord:
    name: str
    url: str
    issue: int
    numbers: tuple[str, ...]
    record_id: str | None = None
    record_path: str | None = None
    raw_position: int | None = None
    evidence: CandidateEvidence | None = None

    @property
    def sorted_numbers(self) -> tuple[str, ...]:
        return tuple(sorted(self.numbers, key=int))

    @property
    def source_path(self) -> str | None:
        return self.record_path


@dataclass(frozen=True)
class ScrapeRecord:
    site_id: str
    name: str
    url: str
    issue: int
    numbers: tuple[str, ...]
    record_id: str | None = None
    source_path: str | None = None
    raw_position: int | None = None
    parser_id: str = "legacy"
    source_hash: str = ""
    fetched_at: str = ""

    @property
    def sorted_numbers(self) -> tuple[str, ...]:
        return tuple(sorted(self.numbers, key=int))

    def target_fingerprint(self) -> str:
        payload = {
            "issue": self.issue,
            "numbers": self.numbers,
            "record_id": self.record_id,
            "source_path": self.source_path,
            "raw_position": self.raw_position,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ArticleRecord:
    record_id: str
    record_path: str
    title: str
    author: str
    body: str
    document: str


@dataclass(frozen=True)
class CacheSite:
    site_id: str
    name: str
    url: str
    records: tuple[ScrapeRecord, ...]


@dataclass(frozen=True)
class CacheSnapshot:
    period: int | None
    periods: int
    generated_at: str
    incomplete: bool
    failures: tuple[str, ...]
    sites: tuple[CacheSite, ...]
    version: int = 2

    @classmethod
    def empty(cls, periods: int = 10) -> "CacheSnapshot":
        return cls(None, periods, "", False, (), ())
