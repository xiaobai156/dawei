"""Process-safe, atomic recent-period cache persistence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Iterable

from dawei.domain.errors import (
    CacheConflictError,
    CacheFormatError,
    CacheLockTimeoutError,
    CacheRollbackError,
    ValidationError,
)
from dawei.domain.models import CacheSite, CacheSnapshot, ScrapeRecord, derive_site_id
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

    def __enter__(self) -> "ProcessFileLock":
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
    return value


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
    if not isinstance(issue, int) or not isinstance(values, list):
        raise CacheFormatError("cache record requires integer issue and numbers list")
    try:
        normalized = validate_36_numbers(values)
    except ValidationError as exc:
        raise CacheFormatError(f"cache {name} {issue} issue is invalid: {exc}") from exc
    raw_position = item.get("raw_position")
    if raw_position is not None and not isinstance(raw_position, int):
        raise CacheFormatError("cache record raw_position must be an integer")
    record = ScrapeRecord(
        site_id=site_id,
        name=name,
        url=url,
        issue=issue,
        numbers=normalized,
        record_id=_optional_string(item.get("record_id", item.get("article_id")), "record_id"),
        source_path=_optional_string(item.get("source_path"), "source_path"),
        raw_position=raw_position,
        parser_id=str(item.get("parser_id") or "legacy"),
        source_hash=str(item.get("source_hash") or ""),
        fetched_at=str(item.get("fetched_at") or generated_at),
    )
    if not record.source_hash:
        record = replace(record, source_hash=record.target_fingerprint())
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


def _records_conflict(existing: ScrapeRecord, incoming: ScrapeRecord) -> bool:
    if existing.numbers != incoming.numbers:
        return True
    if existing.record_id is not None:
        if incoming.record_id is None or existing.record_id != incoming.record_id:
            return True
    if (
        existing.source_path is not None
        and (
            incoming.source_path is None
            or existing.source_path != incoming.source_path
        )
    ):
        return True
    if existing.raw_position is not None and incoming.raw_position is None:
        return True
    return False


class CacheRepository:
    def __init__(self, path: str | Path, lock_timeout: float = 30.0):
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.lock_timeout = lock_timeout

    def load(self) -> CacheSnapshot:
        return self._load_unlocked()

    def migrate(self) -> CacheSnapshot:
        with ProcessFileLock(self.lock_path, timeout=self.lock_timeout):
            snapshot = self._load_unlocked()
            if snapshot.period is None:
                raise CacheFormatError("cannot migrate an empty cache")
            migrated = replace(snapshot, version=2)
            self._write_unlocked(migrated)
            return migrated

    def remap_parser_ids(self, parser_ids: dict[str, str]) -> CacheSnapshot:
        if any(not site_id or not parser_id for site_id, parser_id in parser_ids.items()):
            raise CacheFormatError("site_id and parser_id mappings must be non-empty")
        with ProcessFileLock(self.lock_path, timeout=self.lock_timeout):
            snapshot = self._load_unlocked()
            sites = tuple(
                replace(
                    site,
                    records=tuple(
                        replace(record, parser_id=parser_ids.get(site.site_id, record.parser_id))
                        for record in site.records
                    ),
                )
                for site in snapshot.sites
            )
            migrated = replace(snapshot, sites=sites, version=2)
            self._write_unlocked(migrated)
            return migrated

    def migrate_site_urls(
        self,
        mappings: dict[str, tuple[str, str]],
    ) -> CacheSnapshot:
        if any(not site_id or not old_url or not new_url for site_id, (old_url, new_url) in mappings.items()):
            raise CacheFormatError("site_id and URL mappings must be non-empty")
        with ProcessFileLock(self.lock_path, timeout=self.lock_timeout):
            snapshot = self._load_unlocked()
            known_ids = {site.site_id for site in snapshot.sites}
            missing = set(mappings) - known_ids
            if missing:
                raise CacheFormatError(f"cache site_id not found: {', '.join(sorted(missing))}")
            sites: list[CacheSite] = []
            for site in snapshot.sites:
                mapping = mappings.get(site.site_id)
                if mapping is None:
                    sites.append(site)
                    continue
                old_url, new_url = mapping
                if site.url != old_url:
                    raise CacheConflictError(f"cache site URL mismatch: {site.site_id}")
                records = []
                for record in site.records:
                    migrated_record = replace(record, url=new_url)
                    records.append(
                        replace(
                            migrated_record,
                            source_hash=migrated_record.target_fingerprint(),
                        )
                    )
                sites.append(replace(site, url=new_url, records=tuple(records)))
            migrated = replace(snapshot, sites=tuple(sites), version=2)
            self._write_unlocked(migrated)
            return migrated

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
            names = {site.name for site in archived}
            urls = {site.url for site in archived}
            failures = tuple(
                failure
                for failure in snapshot.failures
                if not any(failure.startswith(name + " ") for name in names)
                and not any(url in failure for url in urls)
            )
            updated = replace(
                snapshot,
                sites=tuple(site for site in snapshot.sites if site.site_id not in site_ids),
                failures=failures,
                incomplete=bool(failures),
                version=2,
            )
            self._write_unlocked(updated)
            return updated

    def _load_unlocked(self) -> CacheSnapshot:
        if not self.path.exists():
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
        if not isinstance(period, int) or not isinstance(periods, int) or periods <= 0:
            raise CacheFormatError("cache requires positive integer period and periods")
        if not isinstance(sites, list) or not isinstance(failures, list):
            raise CacheFormatError("cache sites and failures must be lists")
        generated_at = str(payload.get("generated_at") or "")
        cache_sites: list[CacheSite] = []
        seen_ids: set[str] = set()
        for item in sites:
            if not isinstance(item, dict):
                raise CacheFormatError("cache site must be an object")
            name = item.get("name")
            url = item.get("url")
            records = item.get("records")
            if not isinstance(name, str) or not isinstance(url, str) or not isinstance(records, list):
                raise CacheFormatError("cache site requires name, url, and records")
            site_id = str(item.get("site_id") or derive_site_id(name, url))
            if site_id in seen_ids:
                raise CacheFormatError(f"cache contains duplicate site_id: {site_id}")
            seen_ids.add(site_id)
            parsed = tuple(
                _record_from_mapping(site_id, name, url, record, generated_at) for record in records
            )
            cache_sites.append(CacheSite(site_id, name, url, parsed))
        return CacheSnapshot(
            period=period,
            periods=periods,
            generated_at=generated_at,
            incomplete=bool(payload.get("incomplete", failures)),
            failures=tuple(str(failure) for failure in failures),
            sites=tuple(cache_sites),
            version=int(payload.get("version", 1)),
        )

    def replace_window(
        self,
        results: Iterable[ScrapeRecord],
        failures: Iterable[str],
        *,
        fixed_issue: int,
        periods: int = 10,
    ) -> CacheSnapshot:
        if fixed_issue <= 0 or periods <= 0:
            raise CacheFormatError("fixed_issue and periods must be positive")
        incoming = tuple(results)
        if not incoming:
            raise CacheFormatError("refusing to replace cache with no valid records")
        failure_values = tuple(str(failure) for failure in failures)
        with ProcessFileLock(self.lock_path, timeout=self.lock_timeout):
            existing = self._load_unlocked()
            if existing.period is not None and fixed_issue < existing.period:
                raise CacheRollbackError(
                    f"refusing cache rollback from {existing.period} to {fixed_issue}"
                )
            existing_records = {
                (site.site_id, record.issue): record
                for site in existing.sites
                for record in site.records
            }
            existing_identities = {
                site.site_id: (site.name, site.url)
                for site in existing.sites
            }
            minimum_issue = fixed_issue - periods + 1
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            by_site: dict[str, dict[int, ScrapeRecord]] = {}
            identities: dict[str, tuple[str, str]] = {}
            for result in incoming:
                if result.issue < minimum_issue or result.issue > fixed_issue:
                    continue
                try:
                    numbers = validate_36_numbers(result.numbers)
                except ValidationError as exc:
                    raise CacheFormatError(
                        f"refusing invalid cache result {result.name} {result.issue}: {exc}"
                    ) from exc
                normalized = replace(
                    result,
                    numbers=numbers,
                    site_id=result.site_id or derive_site_id(result.name, result.url),
                    parser_id=result.parser_id or "legacy",
                    fetched_at=result.fetched_at or now,
                )
                if not normalized.source_hash:
                    normalized = replace(
                        normalized,
                        source_hash=normalized.target_fingerprint(),
                    )
                existing_identity = existing_identities.get(normalized.site_id)
                if existing_identity is not None and existing_identity != (
                    normalized.name,
                    normalized.url,
                ):
                    raise CacheConflictError(
                        f"cache site identity conflict: {normalized.site_id}"
                    )
                old = existing_records.get((normalized.site_id, normalized.issue))
                if old is not None and _records_conflict(old, normalized):
                    raise CacheConflictError(
                        f"cache conflict: {normalized.name} {normalized.issue} issue"
                    )
                identity = identities.setdefault(
                    normalized.site_id,
                    (normalized.name, normalized.url),
                )
                if identity != (normalized.name, normalized.url):
                    raise CacheConflictError(
                        f"cache site identity conflict: {normalized.site_id}"
                    )
                records = by_site.setdefault(normalized.site_id, {})
                prior = records.get(normalized.issue)
                if prior is not None and _records_conflict(prior, normalized):
                    raise CacheConflictError(
                        f"cache conflict: {normalized.name} {normalized.issue} issue"
                    )
                records[normalized.issue] = normalized
            if not by_site:
                raise CacheFormatError("refusing to replace cache with no records in target window")
            sites = tuple(
                CacheSite(
                    site_id,
                    identities[site_id][0],
                    identities[site_id][1],
                    tuple(record for _, record in sorted(records.items(), reverse=True))[:periods],
                )
                for site_id, records in sorted(
                    by_site.items(),
                    key=lambda item: (*identities[item[0]], item[0]),
                )
            )
            snapshot = CacheSnapshot(
                period=fixed_issue,
                periods=periods,
                generated_at=now,
                incomplete=bool(failure_values),
                failures=failure_values,
                sites=sites,
                version=2,
            )
            self._write_unlocked(snapshot)
            return snapshot

    def update(
        self,
        results: Iterable[ScrapeRecord],
        failures: Iterable[str],
        *,
        fixed_issue: int,
        periods: int = 10,
        preserve_existing_failures: bool = False,
    ) -> CacheSnapshot:
        if fixed_issue <= 0 or periods <= 0:
            raise CacheFormatError("fixed_issue and periods must be positive")
        incoming = tuple(results)
        failure_values = tuple(str(failure) for failure in failures)
        with ProcessFileLock(self.lock_path, timeout=self.lock_timeout):
            existing = self._load_unlocked()
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
                try:
                    normalized = validate_36_numbers(result.numbers)
                except ValidationError as exc:
                    raise CacheFormatError(
                        f"refusing invalid cache result {result.name} {result.issue}: {exc}"
                    ) from exc
                if result.issue < minimum_issue or result.issue > fixed_issue:
                    continue
                normalized_result = replace(
                    result,
                    numbers=normalized,
                    site_id=result.site_id or derive_site_id(result.name, result.url),
                    parser_id=result.parser_id or "legacy",
                    fetched_at=result.fetched_at or now,
                )
                if not normalized_result.source_hash:
                    normalized_result = replace(
                        normalized_result,
                        source_hash=normalized_result.target_fingerprint(),
                    )
                existing_identity = identities.get(normalized_result.site_id)
                if existing_identity is not None and existing_identity != (
                    normalized_result.name,
                    normalized_result.url,
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
            cache_sites.sort(key=lambda site: (site.name, site.url, site.site_id))
            prior_failures = existing.failures if preserve_existing_failures else ()
            if preserve_existing_failures and incoming:
                successful_names = {result.name for result in incoming}
                successful_urls = {result.url for result in incoming}
                prior_failures = tuple(
                    failure
                    for failure in prior_failures
                    if not any(failure.startswith(name + " ") for name in successful_names)
                    and not any(url in failure for url in successful_urls)
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
        atomic_write_text(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8-sig",
        )
