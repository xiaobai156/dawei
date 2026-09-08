"""Process-safe, atomic recent-period cache persistence."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dawei.domain.errors import (
    CacheConflictError,
    CacheFormatError,
    CacheLockTimeoutError,
    CacheRollbackError,
    ValidationError,
)
from dawei.domain.models import CacheSite, CacheSnapshot, ScrapeRecord
from dawei.domain.validation import validate_36_numbers


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8-sig") -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=encoding,
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
            newline="",
        ) as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


class ProcessFileLock:
    def __init__(self, path: str | Path, timeout: float = 30.0, poll_interval: float = 0.05):
        self.path = Path(path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._handle = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock()
                return self
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise CacheLockTimeoutError(f"cache lock timeout: {self.path}")
                time.sleep(self.poll_interval)

    def _lock(self) -> None:
        if self._handle is None:
            raise RuntimeError("lock file is not open")
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._handle is None:
            return
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CacheFormatError(f"cache record {field} must be a string")
    return value if value.strip() else None


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CacheFormatError(f"cache {field} must be a non-empty string")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise CacheFormatError(f"cache {field} must be a positive integer")
    return value


def _failure_values(values: Iterable[object]) -> tuple[str, ...]:
    failures = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in failures):
        raise CacheFormatError("cache failures must contain non-empty strings")
    return failures


def _record_from_mapping(
    site_id: str,
    name: str,
    url: str,
    item: object,
    generated_at: str,
) -> ScrapeRecord:
    if not isinstance(item, dict):
        raise CacheFormatError("cache record must be an object")
    issue = item.get("issue")
    values = item.get("numbers")
    if type(issue) is not int or issue <= 0 or not isinstance(values, list):
        raise CacheFormatError("cache record requires positive integer issue and numbers list")
    try:
        normalized = validate_36_numbers(values)
    except ValidationError as exc:
        raise CacheFormatError(f"cache {name} {issue} issue is invalid: {exc}") from exc
    raw_position = item.get("raw_position")
    if raw_position is not None and (type(raw_position) is not int or raw_position < 0):
        raise CacheFormatError("cache record raw_position must be a non-negative integer")
    source_hash = item.get("source_hash", "")
    if not isinstance(source_hash, str):
        raise CacheFormatError("cache record source_hash must be a string")
    if "fetched_at" in item:
        fetched_at = item["fetched_at"]
        if not isinstance(fetched_at, str):
            raise CacheFormatError("cache record fetched_at must be a string")
    else:
        fetched_at = generated_at
    parser_id = _required_string(item.get("parser_id"), "record parser_id")
    record = ScrapeRecord(
        site_id=site_id,
        name=name,
        url=url,
        issue=issue,
        numbers=normalized,
        record_id=_optional_string(item.get("record_id", item.get("article_id")), "record_id"),
        source_path=_optional_string(item.get("source_path"), "source_path"),
        raw_position=raw_position,
        parser_id=parser_id,
        source_hash=source_hash,
        fetched_at=fetched_at,
    )
    if not record.source_hash:
        record = replace(record, source_hash=record.target_fingerprint())
    elif record.source_hash != record.target_fingerprint():
        raise CacheFormatError(f"cache {name} {issue} issue source_hash mismatch")
    return record


def _record_to_mapping(record: ScrapeRecord) -> dict[str, object]:
    data: dict[str, object] = {
        "issue": record.issue,
        "numbers": list(record.numbers),
        "parser_id": record.parser_id,
        "source_hash": record.source_hash,
        "fetched_at": record.fetched_at,
    }
    if record.record_id is not None:
        data["record_id"] = record.record_id
        data["article_id"] = record.record_id
    if record.source_path is not None:
        data["source_path"] = record.source_path
    if record.raw_position is not None:
        data["raw_position"] = record.raw_position
    return data


def _normalize_cache_url_identity(value: str) -> str:
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
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), path, query, parts.fragment)
    )


def _same_site_identity(
    existing: tuple[str, str],
    incoming: tuple[str, str],
) -> bool:
    return existing[0] == incoming[0] and _normalize_cache_url_identity(existing[1]) == _normalize_cache_url_identity(incoming[1])


def _failure_matches_site(failure: str, name: str, url: str) -> bool:
    prefix = f"{name} "
    if not failure.startswith(prefix):
        return False
    parts = failure[len(prefix) :].split(maxsplit=1)
    return bool(parts) and _normalize_cache_url_identity(parts[0]) == _normalize_cache_url_identity(url)


def _failure_identity(failure: str) -> tuple[str, str] | None:
    prefix = failure.split(" [", 1)[0]
    parts = prefix.split()
    for index, part in enumerate(parts):
        if part.casefold().startswith(("http://", "https://")) and index:
            return " ".join(parts[:index]), part
    return None


def _validate_existing_config_identity(
    snapshot: CacheSnapshot,
    expected: Mapping[str, tuple[str, str, str, str | None]],
) -> None:
    """Prove that an unbound cache still belongs to the current config."""
    for site in snapshot.sites:
        identity = expected.get(site.site_id)
        if identity is None:
            raise CacheConflictError(f"cache contains configuration-external site: {site.name}")
        name, url, parser_id, dynamic_record_id = identity
        if site.name != name or _normalize_cache_url_identity(site.url) != _normalize_cache_url_identity(url):
            raise CacheConflictError(f"cache site identity does not match configuration: {site.name}")
        if any(record.parser_id != parser_id for record in site.records):
            raise CacheConflictError(f"cache parser identity does not match configuration: {site.name}")
        if dynamic_record_id is not None and any(
            record.record_id != dynamic_record_id for record in site.records
        ):
            raise CacheConflictError(
                f"cache dynamic record_id does not match configuration: {site.name}"
            )

    expected_pairs = tuple((name, url) for name, url, _, _ in expected.values())
    for failure in snapshot.failures:
        identity = _failure_identity(failure)
        if identity is not None and not any(
            name == identity[0]
            and _normalize_cache_url_identity(url) == _normalize_cache_url_identity(identity[1])
            for name, url in expected_pairs
        ):
            raise CacheConflictError(f"cache failure belongs to configuration-external site: {identity[0]}")


def _validate_incoming_config_identity(
    result: ScrapeRecord,
    expected: Mapping[str, tuple[str, str, str, str | None]],
) -> None:
    """Ensure a direct cache write cannot introduce a configuration-external result."""
    identity = expected.get(result.site_id)
    if identity is None:
        raise CacheConflictError(
            f"cache result belongs to configuration-external site: {result.name}"
        )
    name, url, parser_id, dynamic_record_id = identity
    if result.name != name or _normalize_cache_url_identity(result.url) != _normalize_cache_url_identity(url):
        raise CacheConflictError(f"cache result site identity does not match configuration: {result.name}")
    if result.parser_id != parser_id:
        raise CacheConflictError(f"cache result parser identity does not match configuration: {result.name}")
    if dynamic_record_id is not None and result.record_id != dynamic_record_id:
        raise CacheConflictError(
            f"cache result dynamic record_id does not match configuration: {result.name}"
        )


def _records_conflict(existing: ScrapeRecord, incoming: ScrapeRecord) -> bool:
    if existing.numbers != incoming.numbers:
        return True
    if existing.record_id is not None and incoming.record_id is not None:
        # A matching record ID is the authoritative identity.  A changed
        # path or parser position is refreshed extraction metadata.
        return existing.record_id != incoming.record_id
    # If either side lacks a stable record ID, source_path is the remaining
    # identity evidence.  raw_position is metadata and is allowed to drift.
    return existing.source_path != incoming.source_path


def _normalize_incoming_record(result: ScrapeRecord, fetched_at: str) -> ScrapeRecord:
    issue = _positive_int(result.issue, "result issue")
    site_id = _required_string(result.site_id, "result site_id")
    name = _required_string(result.name, "result name")
    url = _required_string(result.url, "result url")
    parser_id = _required_string(result.parser_id, "result parser_id")
    record_id = _optional_string(result.record_id, "record_id")
    source_path = _optional_string(result.source_path, "source_path")
    if not isinstance(result.fetched_at, str):
        raise CacheFormatError("cache result fetched_at must be a string")
    if result.raw_position is not None and (
        type(result.raw_position) is not int or result.raw_position < 0
    ):
        raise CacheFormatError("cache result raw_position must be a non-negative integer")
    try:
        numbers = validate_36_numbers(result.numbers)
    except ValidationError as exc:
        raise CacheFormatError(
            f"refusing invalid cache result {result.name} {result.issue}: {exc}"
        ) from exc
    if not isinstance(result.source_hash, str):
        raise CacheFormatError("cache result source_hash must be a string")
    normalized = replace(
        result,
        issue=issue,
        name=name,
        url=url,
        numbers=numbers,
        site_id=site_id,
        parser_id=parser_id,
        record_id=record_id,
        source_path=source_path,
        fetched_at=result.fetched_at or fetched_at,
    )
    if not normalized.source_hash:
        return replace(normalized, source_hash=normalized.target_fingerprint())
    if normalized.source_hash != normalized.target_fingerprint():
        raise CacheFormatError(
            f"cache result {normalized.name} {normalized.issue} issue source_hash mismatch"
        )
    return normalized


class CacheRepository:
    def __init__(self, path: str | Path, lock_timeout: float = 30.0):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.lock_timeout = lock_timeout
        self._config_fingerprint: str | None = None

    def load(self) -> CacheSnapshot:
        return self._load_unlocked()

    def load_config_fingerprint(self) -> str | None:
        """Read the cache's configuration binding without exposing raw JSON."""
        self._load_unlocked()
        return self._config_fingerprint

    def archive_sites(self, site_ids: set[str]) -> CacheSnapshot:
        if not site_ids or any(not site_id for site_id in site_ids):
            raise CacheFormatError("archived site_ids must be non-empty")
        with ProcessFileLock(self.lock_path, timeout=self.lock_timeout):
            snapshot = self._load_unlocked()
            archived = tuple(site for site in snapshot.sites if site.site_id in site_ids)
            found = {site.site_id for site in archived}
            missing = site_ids - found
            if missing:
                raise CacheFormatError(f"cache site_id not found: {', '.join(sorted(missing))}")
            failures = tuple(
                failure
                for failure in snapshot.failures
                if not any(
                    _failure_matches_site(failure, site.name, site.url)
                    for site in archived
                )
            )
            updated = replace(
                snapshot,
                sites=tuple(site for site in snapshot.sites if site.site_id not in site_ids),
                failures=failures,
                incomplete=bool(failures),
                version=2,
            )
            self._config_fingerprint = None
            self._write_unlocked(updated)
            return updated

    def _load_unlocked(self) -> CacheSnapshot:
        if not self.path.exists():
            self._config_fingerprint = None
            return CacheSnapshot.empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheFormatError(f"failed to load cache {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise CacheFormatError("cache root must be an object")
        period = payload.get("period")
        periods = payload.get("periods", 10)
        sites = payload.get("sites")
        failures = payload.get("failures", [])
        period = _positive_int(period, "period")
        periods = _positive_int(periods, "periods")
        if not isinstance(sites, list) or not isinstance(failures, list):
            raise CacheFormatError("cache sites and failures must be lists")
        failure_values = _failure_values(failures)
        if "generated_at" in payload and not isinstance(payload["generated_at"], str):
            raise CacheFormatError("cache generated_at must be a string")
        generated_at = payload.get("generated_at", "")
        config_fingerprint = payload.get("config_fingerprint")
        if config_fingerprint is not None and (
            not isinstance(config_fingerprint, str) or not config_fingerprint.strip()
        ):
            raise CacheFormatError("cache config_fingerprint must be a non-empty string")
        self._config_fingerprint = config_fingerprint
        incomplete = payload.get("incomplete", bool(failure_values))
        if type(incomplete) is not bool:
            raise CacheFormatError("cache incomplete must be a boolean")
        if incomplete != bool(failure_values):
            raise CacheFormatError("cache incomplete must match the failures list")
        version = _positive_int(payload.get("version", 1), "version")
        cache_sites: list[CacheSite] = []
        seen_ids: set[str] = set()
        for item in sites:
            if not isinstance(item, dict):
                raise CacheFormatError("cache site must be an object")
            name = item.get("name")
            url = item.get("url")
            records = item.get("records")
            if not isinstance(records, list):
                raise CacheFormatError("cache site requires name, url, and records")
            if not records:
                raise CacheFormatError("cache site must contain at least one record")
            if len(records) > periods:
                raise CacheFormatError("cache site contains more records than periods")
            name = _required_string(name, "site name")
            url = _required_string(url, "site url")
            site_id = _required_string(item.get("site_id"), "site_id")
            if site_id in seen_ids:
                raise CacheFormatError(f"cache contains duplicate site_id: {site_id}")
            seen_ids.add(site_id)
            seen_issues: set[int] = set()
            parsed_records = []
            minimum_issue = period - periods + 1
            for record in records:
                parsed_record = _record_from_mapping(site_id, name, url, record, generated_at)
                if not minimum_issue <= parsed_record.issue <= period:
                    raise CacheFormatError(
                        f"cache {name} {parsed_record.issue} issue is outside the cache window"
                    )
                if parsed_record.issue in seen_issues:
                    raise CacheFormatError(
                        f"cache contains duplicate {name} {parsed_record.issue} issue records"
                    )
                seen_issues.add(parsed_record.issue)
                parsed_records.append(parsed_record)
            parsed = tuple(parsed_records)
            cache_sites.append(CacheSite(site_id, name, url, parsed))
        return CacheSnapshot(
            period=period,
            periods=periods,
            generated_at=generated_at,
            incomplete=incomplete,
            failures=failure_values,
            sites=tuple(cache_sites),
            version=version,
        )

    def update(
        self,
        results: Iterable[ScrapeRecord],
        failures: Iterable[str],
        *,
        fixed_issue: int,
        periods: int = 10,
        preserve_existing_failures: bool = False,
        config_fingerprint: str | None = None,
        expected_site_identities: Mapping[str, tuple[str, str, str, str | None]] | None = None,
        allow_missing_fingerprint_binding: bool = False,
        preserve_site_order: bool = False,
    ) -> CacheSnapshot:
        fixed_issue = _positive_int(fixed_issue, "fixed_issue")
        periods = _positive_int(periods, "periods")
        incoming = tuple(results)
        failure_values = _failure_values(failures)
        if config_fingerprint is not None and (
            not isinstance(config_fingerprint, str) or not config_fingerprint.strip()
        ):
            raise CacheFormatError("cache config_fingerprint must be a non-empty string")
        with ProcessFileLock(self.lock_path, timeout=self.lock_timeout):
            existing = self._load_unlocked()
            stored_fingerprint = self._config_fingerprint
            if stored_fingerprint and config_fingerprint and stored_fingerprint != config_fingerprint:
                raise CacheConflictError(
                    "cache configuration fingerprint differs from the requested configuration"
                )
            if expected_site_identities is not None:
                _validate_existing_config_identity(existing, expected_site_identities)
            if config_fingerprint and not stored_fingerprint and (
                existing.sites or existing.failures
            ):
                if not allow_missing_fingerprint_binding:
                    raise CacheConflictError(
                        "cache lacks a configuration fingerprint; subset updates cannot bind it"
                    )
                if expected_site_identities is None:
                    raise CacheConflictError(
                        "cache lacks a configuration fingerprint; identity proof is required"
                    )
            if existing.period is not None and fixed_issue < existing.period:
                raise CacheRollbackError(
                    f"refusing cache rollback from {existing.period} to {fixed_issue}"
                )
            by_site: dict[str, dict[int, ScrapeRecord]] = {
                site.site_id: {record.issue: record for record in site.records}
                for site in existing.sites
            }
            identities: dict[str, tuple[str, str]] = {
                site.site_id: (site.name, site.url) for site in existing.sites
            }
            minimum_issue = fixed_issue - periods + 1
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            for result in incoming:
                normalized_result = _normalize_incoming_record(result, now)
                if expected_site_identities is not None:
                    _validate_incoming_config_identity(
                        normalized_result,
                        expected_site_identities,
                    )
                if normalized_result.issue < minimum_issue or normalized_result.issue > fixed_issue:
                    raise CacheFormatError(
                        f"cache result {normalized_result.name} {normalized_result.issue} issue "
                        "is outside the target cache window"
                    )
                existing_identity = identities.get(normalized_result.site_id)
                if existing_identity is not None and not _same_site_identity(
                    existing_identity,
                    (normalized_result.name, normalized_result.url),
                ):
                    raise CacheConflictError(
                        f"cache site identity conflict: {normalized_result.site_id}"
                    )
                site_records = by_site.setdefault(normalized_result.site_id, {})
                old = site_records.get(normalized_result.issue)
                if old is not None and _records_conflict(old, normalized_result):
                    raise CacheConflictError(
                        f"cache conflict: {normalized_result.name} {normalized_result.issue} issue"
                    )
                site_records[normalized_result.issue] = normalized_result
                identities[normalized_result.site_id] = (
                    normalized_result.name,
                    normalized_result.url,
                )
            cache_sites: list[CacheSite] = []
            for site_id, site_records in by_site.items():
                retained = tuple(
                    record
                    for issue, record in sorted(site_records.items(), reverse=True)
                    if minimum_issue <= issue <= fixed_issue
                )[:periods]
                if not retained:
                    continue
                name, url = identities[site_id]
                cache_sites.append(CacheSite(site_id, name, url, retained))
            if not preserve_site_order:
                cache_sites.sort(key=lambda site: (site.name, site.url, site.site_id))
            prior_failures = existing.failures if preserve_existing_failures else ()
            if preserve_existing_failures and (incoming or failure_values):
                cleared_identities = [
                    (result.name, result.url)
                    for result in incoming
                ]
                cleared_identities.extend(
                    identity
                    for identity in identities.values()
                    if any(
                        _failure_matches_site(failure, *identity)
                        for failure in failure_values
                    )
                )
                cleared_identities.extend(
                    identity
                    for failure in failure_values
                    if (identity := _failure_identity(failure)) is not None
                )
                prior_failures = tuple(
                    failure
                    for failure in prior_failures
                    if not any(
                        _failure_matches_site(failure, *identity)
                        for identity in cleared_identities
                    )
                )
            merged_failures = tuple(dict.fromkeys(prior_failures + failure_values))
            snapshot = CacheSnapshot(
                period=fixed_issue,
                periods=periods,
                generated_at=now,
                incomplete=bool(merged_failures),
                failures=merged_failures,
                sites=tuple(cache_sites),
                version=2,
            )
            if config_fingerprint is not None:
                self._config_fingerprint = config_fingerprint
            self._write_unlocked(snapshot)
            return snapshot

    def _write_unlocked(self, snapshot: CacheSnapshot) -> None:
        payload = {
            "version": 2,
            "period": snapshot.period,
            "periods": snapshot.periods,
            "generated_at": snapshot.generated_at,
            "incomplete": snapshot.incomplete,
            "failures": list(snapshot.failures),
            "sites": [
                {
                    "site_id": site.site_id,
                    "name": site.name,
                    "url": site.url,
                    "records": [_record_to_mapping(record) for record in site.records],
                }
                for site in snapshot.sites
            ],
        }
        if self._config_fingerprint is not None:
            payload["config_fingerprint"] = self._config_fingerprint
        atomic_write_text(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8-sig",
        )
