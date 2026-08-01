"""Histo Maker - privacy-conscious KDE and descriptive-statistics service."""

from __future__ import annotations

import base64
import codecs
import copy
import csv
import hashlib
import io
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from itertools import islice
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from charset_normalizer import from_bytes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import Flask, Response, jsonify, make_response, render_template, request
from scipy.signal import find_peaks
from scipy.stats import gaussian_kde, t as student_t

BASE_DIR = Path(__file__).resolve().parent
VERSION_FILE = BASE_DIR / "VERSION"
APP_VERSION = os.getenv("APP_VERSION") or VERSION_FILE.read_text(encoding="utf-8").strip()
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024
MIN_SHARED_GROUP_SIZE = 5
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
RATE_LIMIT_INSPECT = int(os.getenv("RATE_LIMIT_INSPECT_PER_WINDOW", "30"))
RATE_LIMIT_ANALYZE = int(os.getenv("RATE_LIMIT_ANALYZE_PER_WINDOW", "10"))
RATE_LIMIT_ESTIMATE = int(os.getenv("RATE_LIMIT_ESTIMATE_PER_WINDOW", "30"))
RATE_LIMIT_MAX_CLIENTS = int(os.getenv("RATE_LIMIT_MAX_CLIENTS", "10000"))
MAX_CONCURRENT_ANALYSES = int(os.getenv("MAX_CONCURRENT_ANALYSES_PER_WORKER", "2"))
MAX_CONCURRENT_INSPECTIONS = int(
    os.getenv("MAX_CONCURRENT_INSPECTIONS_PER_WORKER", "1")
)
TRUST_CF_CONNECTING_IP = os.getenv("TRUST_CF_CONNECTING_IP", "0").lower() in {
    "1",
    "true",
    "yes",
}
UPLOAD_CACHE_TTL_SECONDS = int(os.getenv("UPLOAD_CACHE_TTL_SECONDS", "600"))
UPLOAD_CACHE_MAX_BYTES = int(os.getenv("UPLOAD_CACHE_MAX_MB", "256")) * 1024 * 1024
UPLOAD_CACHE_MAX_ITEMS = int(os.getenv("UPLOAD_CACHE_MAX_ITEMS", "100"))
KDE_MAX_SAMPLE_SIZE = int(os.getenv("KDE_MAX_SAMPLE_SIZE", "20000"))
RUG_MAX_POINTS = int(os.getenv("RUG_MAX_POINTS", "300"))
BROWSER_MAX_SHARED_JSON_BYTES = 2_000_000
MAX_SHARED_JSON_BYTES = min(
    int(os.getenv("MAX_SHARED_JSON_BYTES", str(BROWSER_MAX_SHARED_JSON_BYTES))),
    BROWSER_MAX_SHARED_JSON_BYTES,
)
MAX_CURVES = 80
MAX_FORM_JSON_CHARS = int(os.getenv("MAX_FORM_JSON_CHARS", "100000"))
MAX_COLUMN_CONFIG_ITEMS = int(os.getenv("MAX_COLUMN_CONFIG_ITEMS", "500"))
APP_STARTED_MONOTONIC = time.monotonic()

if min(
    MAX_UPLOAD_BYTES,
    RATE_LIMIT_WINDOW_SECONDS,
    RATE_LIMIT_INSPECT,
    RATE_LIMIT_ANALYZE,
    RATE_LIMIT_ESTIMATE,
    RATE_LIMIT_MAX_CLIENTS,
    MAX_CONCURRENT_ANALYSES,
    MAX_CONCURRENT_INSPECTIONS,
    UPLOAD_CACHE_TTL_SECONDS,
    UPLOAD_CACHE_MAX_BYTES,
    UPLOAD_CACHE_MAX_ITEMS,
    KDE_MAX_SAMPLE_SIZE,
    RUG_MAX_POINTS,
    MAX_SHARED_JSON_BYTES,
    MAX_FORM_JSON_CHARS,
    MAX_COLUMN_CONFIG_ITEMS,
) < 1:
    raise RuntimeError("Konfigurationswerte müssen größer als null sein.")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["JSON_SORT_KEYS"] = False
app.logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))


def base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def load_share_signing_key() -> Ed25519PrivateKey:
    """Load a persistent Ed25519 seed, creating one for local development."""
    configured = os.getenv("SHARE_SIGNING_PRIVATE_KEY", "").strip()
    key_file = Path(os.getenv("SHARE_SIGNING_KEY_FILE", BASE_DIR / ".share-signing-key"))
    if configured:
        try:
            raw = base64url_decode(configured)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Der konfigurierte Ed25519-Schlüssel ist ungültig.") from exc
    elif key_file.exists():
        raw = base64url_decode(key_file.read_text(encoding="ascii").strip())
    else:
        generated = Ed25519PrivateKey.generate()
        raw = generated.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        key_file.write_text(base64url_encode(raw), encoding="ascii")
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
    if len(raw) != 32:
        raise RuntimeError("Der Ed25519-Signierschlüssel muss genau 32 Bytes lang sein.")
    return Ed25519PrivateKey.from_private_bytes(raw)


SHARE_SIGNING_KEY = load_share_signing_key()
SHARE_PUBLIC_KEY = SHARE_SIGNING_KEY.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)
SHARE_KEY_ID = hashlib.sha256(SHARE_PUBLIC_KEY).hexdigest()[:16]


def load_share_public_keyring() -> OrderedDict[str, bytes]:
    """Load historical verification keys from a JSON environment setting.

    SHARE_PUBLIC_KEYRING accepts either ``{"key-id": "base64url"}`` or a list
    of ``{"key_id": "...", "public_key": "..."}`` objects. The current key
    is always included and wins on an ID collision.
    """
    configured = (
        os.getenv("SHARE_PUBLIC_KEYRING", "").strip()
        or os.getenv("SHARE_HISTORICAL_PUBLIC_KEYS", "").strip()
        or os.getenv("SHARE_VERIFICATION_PUBLIC_KEYS", "").strip()
        or os.getenv("SHARE_PUBLIC_KEYS", "").strip()
    )
    entries: list[tuple[str, str]] = []
    if configured:
        try:
            decoded = json.loads(configured)
        except json.JSONDecodeError as exc:
            raise RuntimeError("SHARE_PUBLIC_KEYRING muss gültiges JSON enthalten.") from exc
        if isinstance(decoded, dict):
            entries = [(str(key), str(value)) for key, value in decoded.items()]
        elif isinstance(decoded, list):
            for item in decoded:
                if not isinstance(item, dict):
                    raise RuntimeError("Ein Keyring-Eintrag ist ungültig.")
                entries.append((str(item.get("key_id", "")), str(item.get("public_key", ""))))
        else:
            raise RuntimeError("SHARE_PUBLIC_KEYRING muss ein Objekt oder eine Liste sein.")

    result: OrderedDict[str, bytes] = OrderedDict()
    for key_id, encoded in entries:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", key_id):
            raise RuntimeError("Ein Keyring-Key-ID ist ungültig.")
        try:
            raw = base64url_decode(encoded)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"Der öffentliche Schlüssel '{key_id}' ist ungültig.") from exc
        if len(raw) != 32:
            raise RuntimeError(f"Der öffentliche Schlüssel '{key_id}' muss 32 Bytes lang sein.")
        result[key_id] = raw
    result[SHARE_KEY_ID] = SHARE_PUBLIC_KEY
    return result


SHARE_PUBLIC_KEYRING = load_share_public_keyring()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sign_text(value: str) -> str:
    return base64url_encode(SHARE_SIGNING_KEY.sign(value.encode("utf-8")))


@app.after_request
def security_headers(response: Response) -> Response:
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; worker-src 'self'; manifest-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
    )
    return response


class MemoryRateLimiter:
    """Thread-safe, process-local sliding-window limiter with bounded memory."""

    def __init__(self, window_seconds: int, max_clients: int) -> None:
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self.lock = threading.Lock()
        self.buckets: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self.checks = 0

    def check(self, scope: str, identity: str, limit: int) -> tuple[bool, int, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        key = (scope, identity)
        with self.lock:
            bucket = self.buckets.get(key)
            if bucket is None:
                bucket = deque()
                self.buckets[key] = bucket
            else:
                self.buckets.move_to_end(key)
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, math.ceil(bucket[0] + self.window_seconds - now))
                return False, 0, retry_after
            bucket.append(now)
            remaining = max(0, limit - len(bucket))
            self.checks += 1
            if self.checks % 200 == 0 or len(self.buckets) > self.max_clients:
                self._purge(cutoff)
            return True, remaining, 0

    def _purge(self, cutoff: float) -> None:
        stale = []
        for key, bucket in self.buckets.items():
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                stale.append(key)
        for key in stale:
            self.buckets.pop(key, None)
        while len(self.buckets) > self.max_clients:
            self.buckets.popitem(last=False)


RATE_LIMITER = MemoryRateLimiter(RATE_LIMIT_WINDOW_SECONDS, RATE_LIMIT_MAX_CLIENTS)
ANALYZE_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_ANALYSES)
INSPECT_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_INSPECTIONS)


def client_identity() -> str:
    candidate = request.remote_addr or "unknown"
    if TRUST_CF_CONNECTING_IP:
        forwarded = request.headers.get("CF-Connecting-IP", "").strip()
        if forwarded and "," not in forwarded:
            candidate = forwarded
    try:
        candidate = str(ipaddress.ip_address(candidate))
    except ValueError:
        candidate = "unknown"
    return hashlib.sha256(candidate.encode("ascii", "replace")).hexdigest()[:32]


def rate_limited(scope: str, limit: int):
    def decorator(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            allowed, remaining, retry_after = RATE_LIMITER.check(scope, client_identity(), limit)
            if not allowed:
                response = make_response(
                    jsonify(error="Zu viele Anfragen. Bitte kurz warten und erneut versuchen."),
                    429,
                )
                response.headers["Retry-After"] = str(retry_after)
            else:
                response = make_response(function(*args, **kwargs))
            response.headers["RateLimit-Limit"] = str(limit)
            response.headers["RateLimit-Remaining"] = str(remaining)
            response.headers["RateLimit-Reset"] = str(
                retry_after if not allowed else RATE_LIMIT_WINDOW_SECONDS
            )
            return response

        return wrapper

    return decorator


def concurrency_limited(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not ANALYZE_SEMAPHORE.acquire(blocking=False):
            response = make_response(
                jsonify(
                    error="Der Server verarbeitet bereits die maximal erlaubte Anzahl an Analysen. Bitte kurz warten."
                ),
                503,
            )
            response.headers["Retry-After"] = "5"
            return response
        try:
            return function(*args, **kwargs)
        finally:
            ANALYZE_SEMAPHORE.release()

    return wrapper


def inspect_concurrency_limited(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not INSPECT_SEMAPHORE.acquire(blocking=False):
            response = make_response(
                jsonify(
                    error="Der Server liest bereits die maximal erlaubte Anzahl an Dateien ein. Bitte kurz warten."
                ),
                503,
            )
            response.headers["Retry-After"] = "5"
            return response
        try:
            return function(*args, **kwargs)
        finally:
            INSPECT_SEMAPHORE.release()

    return wrapper


def natural_sort_key(value: Any) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(value))
    ]


DELIMITER_LABELS = {
    "\t": "Tabulator",
    ",": "Komma",
    ";": "Semikolon",
    "|": "Pipe",
}
MANUAL_ENCODINGS = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "utf-16": "utf-16",
    "utf16": "utf-16",
    "utf-32": "utf-32",
    "utf32": "utf-32",
    "windows-1252": "cp1252",
    "cp1252": "cp1252",
    "iso-8859-1": "latin-1",
    "latin-1": "latin-1",
    "latin1": "latin-1",
}


def detect_encoding(raw: bytes) -> str:
    """Detect the source encoding, preferring explicit byte-order marks."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    # A bounded prefix is sufficient for charset detection and avoids an
    # unnecessary second pass over very large uploads.
    result = from_bytes(raw[:1_000_000]).best()
    if result is None or not result.encoding:
        raise ValueError("Die Zeichencodierung der Datei konnte nicht erkannt werden.")
    return result.encoding


def detect_delimiter(text: str) -> str:
    """Detect common tabular delimiters from a representative text sample."""
    lines = [line for line in text.splitlines() if line.strip()][:100]
    if not lines:
        raise ValueError("Die ausgewählte Datei enthält keine Datenzeilen.")
    sample = "\n".join(lines)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t,;|")
        widths = [len(row) for row in csv.reader(lines, delimiter=dialect.delimiter)]
        most_common = max(set(widths), key=widths.count)
        # Do not mistake an unquoted decimal comma in a one-column file for a
        # structural delimiter merely because only the data rows contain it.
        if most_common > 1 and widths[0] == most_common and widths.count(most_common) / len(widths) >= 0.8:
            return dialect.delimiter
    except csv.Error:
        pass

    candidates: list[tuple[float, int, str]] = []
    for delimiter in ("\t", ";", ",", "|"):
        widths = [len(row) for row in csv.reader(lines, delimiter=delimiter)]
        multi_column = [width for width in widths if width > 1]
        if not multi_column:
            continue
        most_common = max(set(multi_column), key=multi_column.count)
        consistency = multi_column.count(most_common) / len(lines)
        if widths[0] != most_common or consistency < 0.8:
            continue
        candidates.append((consistency, most_common, delimiter))
    if not candidates:
        # A valid table may consist of a single column and therefore contain
        # no delimiter at all. A tab is inert for such input and keeps the
        # effective parse configuration explicit.
        return "\t"
    return max(candidates)[2]


def _form_value(*names: str, default: str = "") -> str:
    for name in names:
        if name in request.form:
            return request.form.get(name, default)
    return default


def parse_encoding_option(raw_value: str, raw: bytes) -> tuple[str, str]:
    value = (raw_value or "auto").strip().lower()
    if value in {"", "auto", "automatisch"}:
        encoding = detect_encoding(raw)
        try:
            codecs.lookup(encoding)
        except LookupError as exc:
            raise ValueError("Die erkannte Zeichencodierung wird nicht unterstützt.") from exc
        return encoding, "auto"
    if value not in MANUAL_ENCODINGS:
        raise ValueError("Die gewählte Zeichencodierung wird nicht unterstützt.")
    return MANUAL_ENCODINGS[value], "manual"


def parse_delimiter_option(raw_value: str, text: str) -> tuple[str, str]:
    value = raw_value if raw_value is not None else "auto"
    if value == "\t":
        return "\t", "manual"
    normalized = value.strip().lower()
    aliases = {
        "tab": "\t",
        "tabulator": "\t",
        "\\t": "\t",
        "comma": ",",
        "komma": ",",
        "semicolon": ";",
        "semikolon": ";",
        "pipe": "|",
    }
    if normalized in {"", "auto", "automatisch"}:
        return detect_delimiter(text), "auto"
    delimiter = aliases.get(normalized, value)
    if delimiter not in DELIMITER_LABELS:
        raise ValueError("Das gewählte Trennzeichen wird nicht unterstützt.")
    return delimiter, "manual"


def _sample_cells(text: str, delimiter: str) -> list[str]:
    lines = [line for line in text.splitlines() if line.strip()][:1001]
    if len(lines) <= 1:
        return []
    try:
        rows = csv.reader(lines, delimiter=delimiter)
        next(rows, None)
        return [str(cell).strip() for row in rows for cell in row][:10000]
    except csv.Error:
        return []


def detect_number_format(text: str, delimiter: str) -> tuple[str, str | None]:
    cells = _sample_cells(text, delimiter)
    comma_decimal = 0
    dot_decimal = 0
    german_grouping = 0
    english_grouping = 0
    for cell in cells:
        compact = cell.replace("\u00a0", " ").strip()
        if re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+),\d+(?:[eE][+-]?\d+)?", compact):
            comma_decimal += 1
        if re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)\.\d+(?:[eE][+-]?\d+)?", compact):
            dot_decimal += 1
        if re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3})+,\d+(?:[eE][+-]?\d+)?", compact):
            german_grouping += 1
        if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+\.\d+(?:[eE][+-]?\d+)?", compact):
            english_grouping += 1

    if comma_decimal > dot_decimal and (delimiter != "," or comma_decimal >= 2):
        return ",", "." if german_grouping else None
    return ".", "," if english_grouping else None


def parse_decimal_option(raw_value: str, detected: str) -> tuple[str, str]:
    normalized = (raw_value or "auto").strip().lower()
    aliases = {"comma": ",", "komma": ",", "dot": ".", "punkt": "."}
    if normalized in {"", "auto", "automatisch"}:
        return detected, "auto"
    value = aliases.get(normalized, raw_value)
    if value not in {".", ","}:
        raise ValueError("Das Dezimaltrennzeichen muss Punkt oder Komma sein.")
    return value, "manual"


def parse_thousands_option(raw_value: str, detected: str | None) -> tuple[str | None, str]:
    if raw_value is None:
        return detected, "auto"
    if raw_value == " ":
        return " ", "manual"
    normalized = raw_value.strip().lower()
    if normalized in {"auto", "automatisch"}:
        return detected, "auto"
    if normalized in {"", "none", "kein", "keines", "null"}:
        return None, "manual"
    aliases = {
        "comma": ",",
        "komma": ",",
        "dot": ".",
        "punkt": ".",
        "space": " ",
        "leerzeichen": " ",
        "apostrophe": "'",
    }
    value = aliases.get(normalized, raw_value)
    if value not in {".", ",", " ", "'"}:
        raise ValueError("Das Tausendertrennzeichen ist ungültig.")
    return value, "manual"


def _localized_numeric(series: pd.Series, decimal: str, thousands: str | None) -> pd.Series:
    text = series.astype("string").str.strip().str.replace("\u00a0", " ", regex=False)
    text = text.str.replace("−", "-", regex=False)
    if thousands:
        text = text.str.replace(thousands, "", regex=False)
    if decimal != ".":
        text = text.str.replace(decimal, ".", regex=False)
    text = text.mask(text.eq(""))
    return pd.to_numeric(text, errors="coerce")


def normalize_numeric_columns(
    frame: pd.DataFrame, decimal: str, thousands: str | None
) -> tuple[pd.DataFrame, dict[str, dict[str, int]]]:
    normalized = frame.copy()
    parse_info: dict[str, dict[str, int]] = {}
    for column in normalized.columns:
        source = normalized[column]
        original_missing = int(source.isna().sum())
        if pd.api.types.is_numeric_dtype(source) and not pd.api.types.is_bool_dtype(source):
            parse_info[str(column)] = {
                "missing_count": original_missing,
                "invalid_numeric_count": 0,
            }
            continue
        if pd.api.types.is_bool_dtype(source):
            parse_info[str(column)] = {
                "missing_count": original_missing,
                "invalid_numeric_count": 0,
            }
            continue
        parsed = _localized_numeric(source, decimal, thousands)
        nonmissing = int(source.notna().sum())
        parsed_count = int(parsed.notna().sum())
        ratio = parsed_count / nonmissing if nonmissing else 0.0
        should_convert = parsed_count > 0 and ratio >= 0.8
        invalid = nonmissing - parsed_count if should_convert else 0
        if should_convert:
            normalized[column] = parsed
        parse_info[str(column)] = {
            "missing_count": original_missing,
            "invalid_numeric_count": int(invalid),
        }
    return normalized, parse_info


@dataclass
class ParsedDataset:
    frame: pd.DataFrame
    raw: bytes
    filename: str
    encoding: str
    separator: str
    decimal_separator: str
    thousands_separator: str | None
    option_sources: dict[str, str]
    column_parse_info: dict[str, dict[str, int]]
    raw_size: int
    memory_bytes: int

    @property
    def parse_options(self) -> dict[str, Any]:
        return {
            "encoding": self.encoding.upper().replace("_", "-"),
            "delimiter": self.separator,
            "delimiter_label": DELIMITER_LABELS.get(self.separator, repr(self.separator)),
            "decimal_separator": self.decimal_separator,
            "thousands_separator": self.thousands_separator,
            "sources": dict(self.option_sources),
        }


def parse_dataset_bytes(raw: bytes, filename: str) -> ParsedDataset:
    """Parse already-bounded bytes using the current request's import options."""
    if not raw:
        raise ValueError("Die ausgewählte Datei ist leer.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"Die Datei überschreitet das Limit von {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        )

    try:
        encoding, encoding_source = parse_encoding_option(
            _form_value("parse_encoding", "encoding", default="auto"), raw
        )
        text = raw.decode(encoding)
        separator, delimiter_source = parse_delimiter_option(
            _form_value("parse_delimiter", "delimiter", default="auto"), text
        )
        detected_decimal, detected_thousands = detect_number_format(text, separator)
        decimal, decimal_source = parse_decimal_option(
            _form_value("decimal_separator", "decimal", default="auto"), detected_decimal
        )
        thousands_raw = None
        for name in ("thousands_separator", "thousands"):
            if name in request.form:
                thousands_raw = request.form.get(name)
                break
        thousands, thousands_source = parse_thousands_option(thousands_raw, detected_thousands)
        if thousands == decimal:
            raise ValueError("Dezimal- und Tausendertrennzeichen dürfen nicht identisch sein.")
        frame = pd.read_csv(
            io.StringIO(text),
            sep=separator,
            decimal=decimal,
            thousands=thousands,
            low_memory=False,
        )
    except ValueError:
        raise
    except (LookupError, UnicodeDecodeError, pd.errors.ParserError, csv.Error) as exc:
        raise ValueError(f"Die Datei konnte nicht gelesen werden: {exc}") from exc

    if frame.empty or not len(frame.columns):
        raise ValueError("Die Datei enthält keine auswertbaren Daten.")
    if len(frame.columns) > 5000:
        raise ValueError("Die Datei enthält zu viele Spalten (maximal 5000).")
    frame.columns = [str(column) for column in frame.columns]
    frame, parse_info = normalize_numeric_columns(frame, decimal, thousands)
    memory_bytes = int(frame.memory_usage(index=True, deep=True).sum()) + len(raw)
    return ParsedDataset(
        frame=frame,
        raw=raw,
        filename=Path(filename).name[:255],
        encoding=encoding,
        separator=separator,
        decimal_separator=decimal,
        thousands_separator=thousands,
        option_sources={
            "encoding": encoding_source,
            "delimiter": delimiter_source,
            "decimal_separator": decimal_source,
            "thousands_separator": thousands_source,
        },
        column_parse_info=parse_info,
        raw_size=len(raw),
        memory_bytes=memory_bytes,
    )


def parse_uploaded_dataset() -> ParsedDataset:
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        raise ValueError("Bitte zuerst eine TSV- oder CSV-Datei auswählen.")
    raw = uploaded.read(MAX_UPLOAD_BYTES + 1)
    return parse_dataset_bytes(raw, uploaded.filename)


def read_upload() -> tuple[pd.DataFrame, str, str, str]:
    """Backward-compatible upload helper retained for integrations and tests."""
    dataset = parse_uploaded_dataset()
    return dataset.frame, dataset.filename, dataset.encoding, dataset.separator


@dataclass
class CacheEntry:
    dataset: ParsedDataset
    owner: str
    created_monotonic: float
    expires_monotonic: float


class UploadCache:
    """Bounded, thread-safe, process-local cache for parsed uploads."""

    def __init__(self, ttl_seconds: int, max_bytes: int, max_items: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.lock = threading.Lock()
        self.entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self.total_bytes = 0

    def _purge_expired(self, now: float) -> None:
        expired = [
            token for token, entry in self.entries.items() if entry.expires_monotonic <= now
        ]
        for token in expired:
            entry = self.entries.pop(token)
            self.total_bytes -= entry.dataset.memory_bytes

    def put(
        self, dataset: ParsedDataset, owner: str, *, token: str | None = None
    ) -> tuple[str, int]:
        if dataset.memory_bytes > self.max_bytes:
            raise ValueError(
                "Der Datensatz ist größer als der konfigurierte temporäre RAM-Speicher."
            )
        now = time.monotonic()
        token = token or secrets.token_urlsafe(32)
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise ValueError("Der temporäre Upload-Token ist ungültig.")
        entry = CacheEntry(dataset, owner, now, now + self.ttl_seconds)
        with self.lock:
            self._purge_expired(now)
            previous = self.entries.pop(token, None)
            if previous is not None:
                self.total_bytes -= previous.dataset.memory_bytes
            while self.entries and (
                self.total_bytes + dataset.memory_bytes > self.max_bytes
                or len(self.entries) >= self.max_items
            ):
                _, removed = self.entries.popitem(last=False)
                self.total_bytes -= removed.dataset.memory_bytes
            self.entries[token] = entry
            self.total_bytes += dataset.memory_bytes
        return token, self.ttl_seconds

    def get(self, token: str, owner: str) -> ParsedDataset | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token or ""):
            return None
        now = time.monotonic()
        with self.lock:
            self._purge_expired(now)
            entry = self.entries.get(token)
            if entry is None or not secrets.compare_digest(entry.owner, owner):
                return None
            self.entries.move_to_end(token)
            return entry.dataset

    def stats(self) -> dict[str, int]:
        now = time.monotonic()
        with self.lock:
            self._purge_expired(now)
            return {
                "entries": len(self.entries),
                "bytes": self.total_bytes,
                "max_bytes": self.max_bytes,
                "max_items": self.max_items,
                "ttl_seconds": self.ttl_seconds,
            }

    def clear(self) -> None:
        """Test/support helper; never called for ordinary requests."""
        with self.lock:
            self.entries.clear()
            self.total_bytes = 0


UPLOAD_CACHE = UploadCache(
    UPLOAD_CACHE_TTL_SECONDS, UPLOAD_CACHE_MAX_BYTES, UPLOAD_CACHE_MAX_ITEMS
)


class AnalysisMetrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.completed = 0
        self.failed = 0
        self.total_ms = 0.0
        self.last_ms: float | None = None

    def record(self, elapsed_ms: float, success: bool) -> None:
        with self.lock:
            if success:
                self.completed += 1
                self.total_ms += elapsed_ms
                self.last_ms = elapsed_ms
            else:
                self.failed += 1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "completed": self.completed,
                "failed": self.failed,
                "last_ms": round(self.last_ms, 2) if self.last_ms is not None else None,
                "average_ms": (
                    round(self.total_ms / self.completed, 2) if self.completed else None
                ),
            }


ANALYSIS_METRICS = AnalysisMetrics()


def dataset_from_request() -> tuple[ParsedDataset, str]:
    token = request.form.get("upload_token", "").strip()
    if token:
        dataset = UPLOAD_CACHE.get(token, client_identity())
        if dataset is None:
            raise ValueError(
                "Der temporäre Upload ist abgelaufen oder gehört zu einer anderen Sitzung. Bitte die Datei erneut einlesen."
            )
        return dataset, "cache"
    return parse_uploaded_dataset(), "file"


def finite_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def browser_text_length(value: str) -> int:
    """Return JavaScript string length (UTF-16 code units), or a rejecting size."""
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        return MAX_FORM_JSON_CHARS + 1


def json_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return finite_or_none(value)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    return str(value)


def _column_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    return "text"


def column_inspection(dataset: ParsedDataset) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    frame = dataset.frame
    metadata: list[dict[str, Any]] = []
    recommended_candidates: list[tuple[float, str]] = []
    for position, column in enumerate(frame.columns):
        series = frame[column]
        values = series.dropna()
        numeric = bool(
            pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)
        )
        unique_count = int(values.nunique(dropna=True))
        nonmissing_count = int(values.size)
        unique_ratio = unique_count / nonmissing_count if nonmissing_count else 0.0
        high_cardinality = unique_count > 50 and unique_ratio >= 0.5
        finite_values = np.array([], dtype=float)
        if numeric:
            numeric_values = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
            finite_values = numeric_values[np.isfinite(numeric_values)]

        name = str(column)
        semantic_name = name.split(":", 1)[0]
        id_like = bool(
            re.search(
                r"(^|[_\s-])(id|uuid|index|key|nr)([_\s-]|$)", semantic_name, re.I
            )
            or re.search(r"id$", semantic_name, re.I)
        )
        monotonic_id_like = bool(
            numeric
            and unique_ratio >= 0.95
            and len(values) >= 3
            and (series.dropna().is_monotonic_increasing or series.dropna().is_monotonic_decreasing)
        )
        if numeric and finite_values.size >= 2 and np.unique(finite_values).size >= 2:
            score = math.log1p(finite_values.size) + min(unique_count, 100) / 100
            if id_like:
                score -= 100
            if monotonic_id_like:
                score -= 20
            score -= position / 10000
            recommended_candidates.append((score, name))

        value_counts = series.value_counts(dropna=False)
        count_items = [(value, int(count)) for value, count in value_counts.items()]
        count_items.sort(key=lambda item: (-item[1], natural_sort_key(json_value(item[0]))))
        top_values = [
            {"value": json_value(value), "count": count} for value, count in count_items[:10]
        ]
        unique_values = sorted(
            (str(value) for value in values.unique()[:500]), key=natural_sort_key
        )
        parse_info = dataset.column_parse_info.get(name, {})
        metadata.append(
            {
                "name": name,
                "datatype": _column_type(series),
                "dtype": str(series.dtype),
                "numeric": numeric,
                "missing_count": int(parse_info.get("missing_count", series.isna().sum())),
                "invalid_numeric_count": int(parse_info.get("invalid_numeric_count", 0)),
                "non_finite_count": (
                    int(np.count_nonzero(~np.isfinite(pd.to_numeric(values, errors="coerce"))))
                    if numeric
                    else 0
                ),
                "unique_count": unique_count,
                "high_cardinality": high_cardinality,
                "minimum": finite_or_none(np.min(finite_values)) if finite_values.size else None,
                "maximum": finite_or_none(np.max(finite_values)) if finite_values.size else None,
                "min": finite_or_none(np.min(finite_values)) if finite_values.size else None,
                "max": finite_or_none(np.max(finite_values)) if finite_values.size else None,
                "recommended_x": False,
                "id_like": id_like or monotonic_id_like,
                "unique": unique_values,
                "unique_truncated": unique_count > 500,
                "top_values": top_values,
            }
        )

    if recommended_candidates:
        recommended_name = max(recommended_candidates, key=lambda item: item[0])[1]
        for item in metadata:
            item["recommended_x"] = item["name"] == recommended_name

    warnings: list[dict[str, Any]] = []
    total_missing = sum(item["missing_count"] for item in metadata)
    total_invalid = sum(item["invalid_numeric_count"] for item in metadata)
    total_nonfinite = sum(item["non_finite_count"] for item in metadata)
    constants = [item["name"] for item in metadata if item["unique_count"] <= 1]
    high_cardinality = [item["name"] for item in metadata if item["high_cardinality"]]
    if total_missing:
        warnings.append(
            {
                "code": "missing_values",
                "message": f"{total_missing} fehlende Werte wurden erkannt.",
                "count": total_missing,
            }
        )
    if total_invalid:
        warnings.append(
            {
                "code": "invalid_numeric_values",
                "message": f"{total_invalid} ungültige Werte in numerisch erkannten Spalten wurden ausgeschlossen.",
                "count": total_invalid,
            }
        )
    if total_nonfinite:
        warnings.append(
            {
                "code": "non_finite_values",
                "message": f"{total_nonfinite} nicht-endliche Zahlen (±Infinity) wurden erkannt.",
                "count": total_nonfinite,
            }
        )
    if constants:
        warnings.append(
            {
                "code": "constant_columns",
                "message": f"{len(constants)} konstante Spalte(n) eignen sich nicht als X-Achse.",
                "columns": constants[:50],
            }
        )
    if high_cardinality:
        warnings.append(
            {
                "code": "high_cardinality",
                "message": f"{len(high_cardinality)} Spalte(n) haben eine hohe Kardinalität.",
                "columns": high_cardinality[:50],
            }
        )
    return metadata, warnings


def _parse_user_number(value: Any) -> float:
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError
    return number


def condition_mask(frame: pd.DataFrame, item: dict[str, Any]) -> pd.Series:
    column = item.get("column")
    operator = item.get("operator")
    value = item.get("value")
    if column not in frame.columns or operator not in {"==", "!=", ">", "<"}:
        raise ValueError("Ein Filter enthält eine ungültige Spalte oder Bedingung.")

    if operator in {">", "<"}:
        numeric = pd.to_numeric(frame[column], errors="coerce")
        try:
            limit = _parse_user_number(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"'{value}' ist kein gültiger Zahlenwert.") from exc
        return numeric > limit if operator == ">" else numeric < limit

    if pd.api.types.is_numeric_dtype(frame[column]):
        try:
            expected = _parse_user_number(value)
            mask = frame[column] == expected
        except (ValueError, TypeError):
            mask = frame[column].astype(str) == str(value)
    else:
        mask = frame[column].astype(str) == str(value)
    return ~mask if operator == "!=" else mask


def filter_mask(
    frame: pd.DataFrame,
    node: dict[str, Any],
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> pd.Series:
    """Recursively evaluate nested AND/OR filter groups."""
    if not isinstance(node, dict):
        raise ValueError("Die Filterstruktur ist ungültig.")
    if depth > 8:
        raise ValueError("Filter dürfen höchstens acht Ebenen tief verschachtelt sein.")
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if counter[0] > 100:
        raise ValueError("Es sind höchstens 100 Filterelemente erlaubt.")

    if node.get("type") == "condition":
        return condition_mask(frame, node)
    if node.get("type") != "group":
        raise ValueError("Die Filterstruktur enthält einen unbekannten Elementtyp.")

    logic = str(node.get("logic", "AND")).upper()
    children = node.get("children", [])
    if logic not in {"AND", "OR"} or not isinstance(children, list):
        raise ValueError("Eine Filtergruppe enthält eine ungültige Verknüpfung.")
    if not children:
        return pd.Series(True, index=frame.index)

    child_iterator = iter(children)
    result = filter_mask(
        frame, next(child_iterator), depth=depth + 1, counter=counter
    )
    # Combine one child at a time. A wide filter tree therefore retains at
    # most the accumulated result and the current child mask, rather than up
    # to 100 full-size Boolean Series simultaneously.
    for child in child_iterator:
        child_mask = filter_mask(
            frame, child, depth=depth + 1, counter=counter
        )
        if logic == "AND":
            result &= child_mask
        else:
            result |= child_mask
        del child_mask
    return result


def apply_filter_tree(frame: pd.DataFrame, tree: dict[str, Any]) -> pd.DataFrame:
    return frame[filter_mask(frame, tree)]


def filter_expression(node: dict[str, Any], *, is_root: bool = True) -> str:
    operators = {"==": "=", "!=": "≠", ">": ">", "<": "<"}
    if node.get("type") == "condition":
        return f'{node.get("column", "")} {operators.get(node.get("operator"), "?")} "{node.get("value", "")}"'
    children = node.get("children", [])
    if not children:
        return "Keine Filter"
    connector = " UND " if str(node.get("logic", "AND")).upper() == "AND" else " ODER "
    expression = connector.join(filter_expression(child, is_root=False) for child in children)
    return f"({expression})" if not is_root or len(children) > 1 else expression


def parse_filter_tree() -> dict[str, Any]:
    serialized_tree = request.form.get("filter_tree")
    try:
        if serialized_tree is not None:
            if len(serialized_tree) > MAX_FORM_JSON_CHARS:
                raise ValueError("Die Filterdefinition ist zu groß.")
            tree = json.loads(serialized_tree)
        else:
            serialized_filters = request.form.get("filters", "[]")
            if len(serialized_filters) > MAX_FORM_JSON_CHARS:
                raise ValueError("Die Filterdefinition ist zu groß.")
            tree = {
                "type": "group",
                "logic": request.form.get("logic", "AND").upper(),
                "children": [
                    {"type": "condition", **item} for item in json.loads(serialized_filters)
                ],
            }
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        raise ValueError("Die Filter konnten nicht verarbeitet werden.") from exc
    if not isinstance(tree, dict):
        raise ValueError("Die Filter konnten nicht verarbeitet werden.")
    return tree


def parse_column_config(frame: pd.DataFrame) -> dict[str, dict[str, str]]:
    raw = request.form.get("column_config", "").strip()
    if not raw:
        return {}
    if len(raw) > MAX_FORM_JSON_CHARS:
        raise ValueError("Die Spaltenkonfiguration ist zu groß.")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Die Spaltenkonfiguration ist ungültig.") from exc
    if not isinstance(decoded, dict) or len(decoded) > len(frame.columns):
        raise ValueError("Die Spaltenkonfiguration ist ungültig oder zu umfangreich.")
    result: dict[str, dict[str, str]] = {}
    for column, config in decoded.items():
        if column not in frame.columns or not isinstance(config, dict):
            raise ValueError("Die Spaltenkonfiguration enthält eine unbekannte Spalte.")
        alias = str(config.get("alias", "")).strip()
        unit = str(config.get("unit", "")).strip()
        if len(alias) > 100 or len(unit) > 40:
            raise ValueError("Alias oder Einheit in der Spaltenkonfiguration ist zu lang.")
        if any(ord(character) < 32 for character in alias + unit):
            raise ValueError("Alias oder Einheit enthält ungültige Steuerzeichen.")
        if alias or unit:
            result[column] = {"alias": alias, "unit": unit}
            if len(result) > MAX_COLUMN_CONFIG_ITEMS:
                raise ValueError("Es sind zu viele benutzerdefinierte Spaltenangaben vorhanden.")
    return result


def display_column_label(column: str, config: dict[str, dict[str, str]]) -> str:
    item = config.get(column, {})
    label = item.get("alias") or column
    unit = item.get("unit")
    return f"{label} [{unit}]" if unit else label


def parse_bandwidth() -> str | float:
    raw = request.form.get("bandwidth", "scott").strip().lower()
    if raw in {"", "scott"}:
        return "scott"
    if raw == "silverman":
        return "silverman"
    try:
        value = float(raw.replace(",", "."))
    except ValueError as exc:
        raise ValueError("Die KDE-Bandbreite muss 'scott', 'silverman' oder positiv sein.") from exc
    if not math.isfinite(value) or value <= 0 or value > 100:
        raise ValueError("Der numerische Bandbreitenfaktor muss größer als 0 und höchstens 100 sein.")
    return value


def parse_share_expiry_days() -> int | None:
    raw = request.form.get("share_expiry_days", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("Die Freigabedauer muss eine ganze Zahl von Tagen sein.") from exc
    if value < 1 or value > 3650:
        raise ValueError("Die Freigabedauer muss zwischen 1 und 3650 Tagen liegen.")
    return value


def parse_segment_top_n(hues: list[str]) -> dict[str, int]:
    raw = request.form.get("segment_top_n", "").strip()
    if not raw or not hues:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = raw

    def integer_value(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("Top N muss eine ganze Zahl sein.")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise ValueError("Top N muss eine ganze Zahl sein.")
        text = str(value).strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError("Top N muss eine ganze Zahl sein.")
        return int(text)

    if isinstance(decoded, (int, float, str)) and not isinstance(decoded, bool):
        number = integer_value(decoded)
        values = {hue: number for hue in hues}
    elif isinstance(decoded, dict):
        unknown = set(decoded) - set(hues)
        if unknown:
            raise ValueError("Top N enthält eine unbekannte Segmentspalte.")
        values = {str(key): integer_value(value) for key, value in decoded.items()}
    else:
        raise ValueError("Top N ist ungültig.")
    if any(value < 1 or value > MAX_CURVES for value in values.values()):
        raise ValueError(f"Top N muss zwischen 1 und {MAX_CURVES} liegen.")
    return values


def _deterministic_indices(length: int, sample_size: int, salt: str) -> np.ndarray:
    if length <= sample_size:
        return np.arange(length)
    seed = int.from_bytes(hashlib.sha256(salt.encode("utf-8")).digest()[:8], "big")
    generator = np.random.default_rng(seed)
    return np.sort(generator.choice(length, size=sample_size, replace=False))


def validate_numeric_range(values: np.ndarray) -> None:
    maximum_safe_magnitude = math.sqrt(np.finfo(float).max) / 4
    maximum_magnitude = float(np.max(np.abs(values)))
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        numeric_span = float(np.max(values) - np.min(values))
    if maximum_magnitude > maximum_safe_magnitude or not math.isfinite(numeric_span):
        raise ValueError(
            "Der endliche Zahlenbereich der X-Achse ist für stabile Statistik- und KDE-Berechnungen zu groß."
        )
    # Non-zero subnormal spans cannot produce finite density values because
    # dividing probability mass by the representable bin width overflows.
    if 0 < numeric_span < np.finfo(float).tiny * 128:
        raise ValueError(
            "Der numerische Wertebereich der X-Achse ist für eine stabile Dichteschätzung zu klein."
        )


def _histogram(values: np.ndarray) -> dict[str, list[float] | list[int]]:
    validate_numeric_range(values)
    minimum, maximum = float(np.min(values)), float(np.max(values))
    if minimum == maximum:
        width = max(abs(minimum) * 0.02, 1.0)
        edges = np.array([minimum - width / 2, maximum + width / 2], dtype=float)
    else:
        q1, q3 = np.quantile(values, [0.25, 0.75])
        iqr = float(q3 - q1)
        fd_width = 2 * iqr / np.cbrt(len(values)) if iqr > 0 else 0
        bins = math.ceil((maximum - minimum) / fd_width) if fd_width > 0 else math.ceil(math.sqrt(len(values)))
        bins = max(1, min(60, bins))
        edges = np.linspace(minimum, maximum, bins + 1)
    counts, edges = np.histogram(values, bins=edges)
    widths = np.diff(edges)
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        density = counts / (len(values) * widths)
    centers = (edges[:-1] + edges[1:]) / 2
    if not np.all(np.isfinite(centers)) or not np.all(np.isfinite(density)):
        raise ValueError(
            "Der numerische Wertebereich erlaubt kein endliches Histogramm."
        )
    return {
        "x": [float(value) for value in centers],
        "y": [float(value) for value in density],
        "counts": [int(value) for value in counts],
        "edges": [float(value) for value in edges],
    }


def curve_for(
    series: pd.Series,
    bandwidth: str | float = "scott",
    *,
    salt: str = "all",
) -> dict[str, Any]:
    values = series.to_numpy(dtype=float)
    validate_numeric_range(values)
    minimum, maximum = float(np.min(values)), float(np.max(values))
    kde_indices = _deterministic_indices(len(values), KDE_MAX_SAMPLE_SIZE, f"kde:{salt}")
    kde_values = values[kde_indices]
    effective_factor: float | None = None
    fallback = False
    if minimum == maximum or len(kde_values) < 2:
        x_values = np.array([minimum])
        y_values = np.array([1.0])
        peak_count = 1
    else:
        try:
            if isinstance(bandwidth, (int, float)):
                multiplier = float(bandwidth)
                if not math.isfinite(multiplier) or multiplier <= 0:
                    raise ValueError("Der Bandbreitenmultiplikator muss positiv sein.")
                bw_method: str | Any = (
                    lambda kde: kde.scotts_factor() * multiplier
                )
            else:
                bw_method = bandwidth
            estimator = gaussian_kde(kde_values, bw_method=bw_method)
            effective_factor = finite_or_none(estimator.factor)
            padding = (maximum - minimum) * 0.08
            x_values = np.linspace(minimum - padding, maximum + padding, 320)
            y_values = estimator(x_values)
            if not np.all(np.isfinite(x_values)) or not np.all(np.isfinite(y_values)):
                raise ValueError("Die KDE enthält nicht-endliche Ergebnisse.")
            prominence = max(float(np.max(y_values)) * 0.03, np.finfo(float).eps)
            peaks, _ = find_peaks(y_values, prominence=prominence)
            peak_count = len(peaks)
            if peak_count == 0 and len(y_values):
                peak_count = 1
        except (ValueError, np.linalg.LinAlgError):
            fallback = True
            histogram = _histogram(kde_values)
            x_values = np.asarray(histogram["x"], dtype=float)
            y_values = np.asarray(histogram["y"], dtype=float)
            peaks, _ = find_peaks(y_values)
            peak_count = max(1, len(peaks))

    if peak_count == 1:
        modality_label = "Unimodal"
    elif peak_count == 2:
        modality_label = "Bimodal"
    elif peak_count > 2:
        modality_label = f"Multimodal ({peak_count})"
    else:
        modality_label = "Unbestimmt"
    density_mode = finite_or_none(x_values[int(np.argmax(y_values))]) if len(y_values) else None
    rug_indices = _deterministic_indices(len(values), RUG_MAX_POINTS, f"rug:{salt}")
    rug = np.sort(values[rug_indices])
    return {
        "x": [float(value) for value in x_values],
        "y": [float(value) for value in y_values],
        "histogram": _histogram(values),
        "rug": [float(value) for value in rug],
        "references": {
            "mean": finite_or_none(np.mean(values)),
            "median": finite_or_none(np.median(values)),
        },
        "density_mode": density_mode,
        "kde_sample_size": int(len(kde_values)),
        "kde_sampled": len(kde_values) < len(values),
        "kde_bandwidth_factor": effective_factor,
        "modality_heuristic": {
            "label": modality_label,
            "heuristic": True,
            "method": "kde_peak_prominence",
            "peak_count": int(peak_count),
            "prominence_ratio": 0.03,
            "evaluation_points": int(len(x_values)),
            "fallback": fallback,
        },
    }


def modality(series: pd.Series) -> str:
    if len(series) < 5:
        return "Zu wenig Daten"
    return str(curve_for(series)["modality_heuristic"]["label"])


def statistic_row(
    label: str,
    series: pd.Series,
    *,
    density_mode: float | None = None,
    modality_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values = series.to_numpy(dtype=float)
    counts = series.value_counts(dropna=True)
    maximum_frequency = int(counts.max()) if not counts.empty else 0
    mode_value: float | None = None
    mode_values: list[float] = []
    if maximum_frequency >= 2:
        mode_values = sorted(
            float(value) for value in counts[counts == maximum_frequency].index
        )
        if len(mode_values) == 1:
            mode_value = mode_values[0]
    q1, median, q3 = (float(value) for value in np.quantile(values, [0.25, 0.5, 0.75]))
    mean = float(np.mean(values))
    minimum, maximum = float(np.min(values)), float(np.max(values))
    std = float(np.std(values, ddof=1)) if len(values) > 1 else None
    variance = float(np.var(values, ddof=1)) if len(values) > 1 else None
    if std is not None:
        margin = float(student_t.ppf(0.975, len(values) - 1)) * std / math.sqrt(len(values))
        ci_low, ci_high = mean - margin, mean + margin
    else:
        ci_low = ci_high = None
    if len(series) < 5:
        modality_label = "Zu wenig Daten"
    elif modality_metadata:
        modality_label = str(modality_metadata.get("label", "Unbestimmt"))
    else:
        modality_label = modality(series)
    return {
        "segment": label,
        "mode": mode_value,
        "mode_count": maximum_frequency if mode_values else 0,
        "mode_tied": len(mode_values) > 1,
        "mode_values": mode_values[:20],
        "mode_values_truncated": len(mode_values) > 20,
        "density_mode": density_mode,
        "median": median,
        "mean": mean,
        "std": finite_or_none(std),
        "variance": finite_or_none(variance),
        "range": maximum - minimum,
        "minimum": minimum,
        "maximum": maximum,
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
        "mad": float(np.median(np.abs(values - median))),
        "ci_low": finite_or_none(ci_low),
        "ci_high": finite_or_none(ci_high),
        "mean_ci95_low": finite_or_none(ci_low),
        "mean_ci95_high": finite_or_none(ci_high),
        "modality": modality_label,
        "modality_metadata": modality_metadata
        or {
            "heuristic": True,
            "method": "kde_peak_prominence",
            "peak_count": None,
            "prominence_ratio": 0.03,
        },
        "skew": finite_or_none(series.skew()) if len(series) > 2 else None,
        "kurtosis": finite_or_none(series.kurtosis()) if len(series) > 3 else None,
        "count": int(len(series)),
    }


def _validate_shared_segment_values(value: Any) -> None:
    if isinstance(value, str):
        if browser_text_length(value) > 500:
            raise ValueError(
                "Ein Segmentwert ist für einen signierten Freigabelink zu lang (maximal 500 Zeichen)."
            )
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if browser_text_length(str(key)) > 500:
                raise ValueError(
                    "Ein Segmentname ist für einen signierten Freigabelink zu lang."
                )
            _validate_shared_segment_values(child)
    elif isinstance(value, list):
        for child in value:
            _validate_shared_segment_values(child)


def shared_result_copy(
    result: dict[str, Any], allowed_columns: set[str] | None = None
) -> dict[str, Any]:
    """Return the privacy-minimized result embedded in a signed share payload."""
    shared = copy.deepcopy(result)
    x_label = shared.get("x_label")
    if not isinstance(x_label, str) or browser_text_length(x_label) > 500:
        raise ValueError(
            "Der Name der X-Achse ist für einen Freigabelink zu lang (maximal 500 Zeichen)."
        )
    display_x_label = shared.get("display_x_label")
    if isinstance(display_x_label, str) and browser_text_length(display_x_label) > 500:
        raise ValueError("Der Anzeigename der X-Achse ist für einen Freigabelink zu lang.")
    if allowed_columns is None:
        allowed_columns = {x_label}
        allowed_columns.update(
            item.get("name")
            for item in shared.get("segment_columns", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
    column_config = shared.get("column_config")
    if isinstance(column_config, dict):
        shared["column_config"] = {
            column: config
            for column, config in column_config.items()
            if column in allowed_columns
        }
    segment_columns = shared.get("segment_columns")
    if isinstance(segment_columns, list):
        shared["segment_columns"] = [
            item
            for item in segment_columns
            if isinstance(item, dict) and item.get("name") in allowed_columns
        ]
    for keyed_metadata in ("cardinalities", "segment_top_n", "segment_other_labels"):
        value = shared.get(keyed_metadata)
        if isinstance(value, dict):
            shared[keyed_metadata] = {
                column: metadata
                for column, metadata in value.items()
                if column in allowed_columns
            }
    for curve in shared.get("curves", []):
        label = curve.get("label")
        if not isinstance(label, str) or browser_text_length(label) > 500:
            raise ValueError(
                "Eine Segmentbezeichnung ist für einen Freigabelink zu lang (maximal 500 Zeichen)."
            )
        # Rug samples contain individual observations and are intentionally
        # local-only even though the aggregate curve may be shared.
        curve.pop("rug", None)
        _validate_shared_segment_values(curve.get("segment_values", {}))
    for row in shared.get("statistics", []):
        segment = row.get("segment")
        if not isinstance(segment, str) or browser_text_length(segment) > 500:
            raise ValueError(
                "Eine Statistik-Segmentbezeichnung ist für einen Freigabelink zu lang."
            )
    return shared


def signed_share_material(
    result: dict[str, Any],
    filter_summary: str,
    reproducibility: dict[str, Any] | None = None,
    share_expiry_days: int | None = None,
) -> dict[str, Any]:
    if browser_text_length(filter_summary) > 10000:
        raise ValueError(
            "Die Filterzusammenfassung ist für einen Freigabelink zu lang (maximal 10000 Zeichen)."
        )
    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=share_expiry_days) if share_expiry_days else None
    allowed_columns = {str(result.get("x_label", ""))}
    if isinstance(reproducibility, dict):
        x_column = reproducibility.get("x_column")
        if isinstance(x_column, str):
            allowed_columns.add(x_column)
        hues = reproducibility.get("hues", [])
        if isinstance(hues, list):
            allowed_columns.update(hue for hue in hues if isinstance(hue, str))
    else:
        allowed_columns.update(
            item.get("name")
            for item in result.get("segment_columns", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
    payload_object: dict[str, Any] = {
        "v": 1,
        "app_version": APP_VERSION,
        "created_at": created.isoformat(timespec="seconds"),
        "result": shared_result_copy(result, allowed_columns),
    }
    if reproducibility is not None:
        shared_reproducibility = copy.deepcopy(reproducibility)
        config = shared_reproducibility.get("column_config")
        if isinstance(config, dict):
            shared_reproducibility["column_config"] = {
                column: value
                for column, value in config.items()
                if column in allowed_columns
            }
        for keyed_metadata in ("segment_top_n", "segment_other_labels"):
            value = shared_reproducibility.get(keyed_metadata)
            if isinstance(value, dict):
                shared_reproducibility[keyed_metadata] = {
                    column: metadata
                    for column, metadata in value.items()
                    if column in allowed_columns
                }
        payload_object["reproducibility"] = shared_reproducibility
    if expires is not None:
        payload_object["expires_at"] = expires.isoformat(timespec="seconds")
    payload = compact_json(payload_object)
    payload_digest = base64url_encode(hashlib.sha256(payload.encode("utf-8")).digest())
    context_payload = compact_json(
        {"v": 1, "result_digest": payload_digest, "filter_summary": filter_summary}
    )
    return {
        "algorithm": "Ed25519",
        "key_id": SHARE_KEY_ID,
        "payload": payload,
        "signature": sign_text(payload),
        "context_payload": context_payload,
        "context_signature": sign_text(context_payload),
        "expires_at": payload_object.get("expires_at"),
    }


def share_material_size(material: dict[str, Any]) -> tuple[int, int]:
    """Return UTF-8 byte sizes for payload and worst-case browser envelope."""
    payload_size = len(str(material["payload"]).encode("utf-8"))
    envelope = {
        "v": 1,
        "algorithm": material["algorithm"],
        "key_id": material["key_id"],
        "payload": material["payload"],
        "signature": material["signature"],
        "context_payload": material["context_payload"],
        "context_signature": material["context_signature"],
    }
    envelope_size = len(compact_json(envelope).encode("utf-8"))
    return payload_size, envelope_size


@dataclass(frozen=True)
class OtherCategory:
    column: str
    display: str

    def __str__(self) -> str:
        return self.display


@dataclass
class AnalysisGroup:
    label: str
    segment_values: dict[str, Any]
    segment_other: dict[str, bool]
    segment_key: str
    series: pd.Series


def _segment_json_value(value: Any) -> Any:
    if isinstance(value, OtherCategory):
        return {"kind": "other", "label": value.display}
    if isinstance(value, (float, np.floating)) and not math.isfinite(float(value)):
        return {
            "kind": "non_finite",
            "value": "+Infinity" if float(value) > 0 else "-Infinity",
        }
    return json_value(value)


def _segment_descriptor(
    hues: list[str], parts: tuple[Any, ...]
) -> tuple[str, str, dict[str, Any], dict[str, bool], str]:
    segment_values = {
        hue: _segment_json_value(value) for hue, value in zip(hues, parts)
    }
    segment_other = {
        hue: isinstance(value, OtherCategory) for hue, value in zip(hues, parts)
    }
    for value in segment_values.values():
        _validate_shared_segment_values(value)
    canonical = compact_json(
        {"segment_values": segment_values, "segment_other": segment_other}
    )
    segment_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    display_values = [str(value) for value in parts]
    if not hues:
        base_label = "Alle Daten"
    elif len(hues) == 1:
        base_label = display_values[0]
    else:
        base_label = " · ".join(
            f"{json.dumps(hue, ensure_ascii=False)}={json.dumps(value, ensure_ascii=False)}"
            for hue, value in zip(hues, display_values)
        )
    if browser_text_length(base_label) > 470:
        raise ValueError(
            "Eine Segmentbezeichnung ist zu lang. Segmentwerte dürfen höchstens 500 Zeichen lang sein."
        )
    return canonical, base_label, segment_values, segment_other, segment_key


def _group_metadata(
    names: list[Any], hues: list[str]
) -> dict[str, tuple[str, dict[str, Any], dict[str, bool], str]]:
    descriptors = []
    base_counts: dict[str, int] = {}
    for name in names:
        parts = name if isinstance(name, tuple) else (name,)
        descriptor = _segment_descriptor(hues, parts)
        descriptors.append(descriptor)
        base_counts[descriptor[1]] = base_counts.get(descriptor[1], 0) + 1
    result = {}
    used_labels: set[str] = set()
    for canonical, base, values, other, key in descriptors:
        label = f"{base} [{key[:8]}]" if base_counts[base] > 1 else base
        candidate = label
        disambiguator = 2
        while candidate in used_labels:
            candidate = f"{label}-{disambiguator}"
            disambiguator += 1
        label = candidate
        used_labels.add(label)
        result[canonical] = (label, values, other, key)
    return result


def _group_name_parts(name: Any) -> tuple[Any, ...]:
    return name if isinstance(name, tuple) else (name,)


@dataclass
class PreparedAnalysis:
    x_column: str
    hues: list[str]
    filter_tree: dict[str, Any]
    filter_summary: str
    groups: list[AnalysisGroup]
    all_group_sizes: list[tuple[str, int]]
    observed_group_count: int
    curve_count: int
    small_group_count: int
    omitted_small_group_rows: int
    original_group_count: int
    original_min_group_size: int | None
    share_blocked_group_count: int
    filtered_rows: int
    analyzed_rows: int
    plotted_rows: int
    exclusions: dict[str, int]
    cardinalities: dict[str, dict[str, int]]
    segment_top_n: dict[str, int]
    segment_other_labels: dict[str, str]


def prepare_analysis(
    dataset: ParsedDataset,
    *,
    materialize_groups: bool = True,
    enforce_curve_limit: bool = True,
) -> PreparedAnalysis:
    frame = dataset.frame
    x_column = request.form.get("x_column", "")
    hue1 = request.form.get("hue1", "")
    hue2 = request.form.get("hue2", "")
    if x_column not in frame.columns:
        raise ValueError("Bitte eine gültige numerische X-Achse auswählen.")
    requested_hues = [column for column in (hue1, hue2) if column]
    if len(requested_hues) != len(set(requested_hues)):
        raise ValueError("Dieselbe Segmentspalte darf nicht zweimal ausgewählt werden.")
    unknown_hues = [column for column in requested_hues if column not in frame.columns]
    if unknown_hues:
        raise ValueError("Eine ausgewählte Segmentspalte ist nicht vorhanden.")
    if browser_text_length(x_column) > 500 or any(
        browser_text_length(column) > 500 for column in requested_hues
    ):
        raise ValueError("Spaltennamen dürfen für die Analyse höchstens 500 Zeichen lang sein.")

    filter_tree = parse_filter_tree()
    filter_summary = filter_expression(filter_tree)
    if browser_text_length(filter_summary) > 10000:
        raise ValueError(
            "Die Filterzusammenfassung ist zu lang (maximal 10000 Zeichen)."
        )
    # Evaluate filters against the source frame, then project immediately to
    # the only columns needed by grouping/statistics. This avoids full-frame
    # copies for wide uploads.
    selected_columns = list(dict.fromkeys([x_column, *requested_hues]))
    mask = filter_mask(frame, filter_tree)
    filtered_rows = int(mask.sum())
    projected = frame.loc[mask, selected_columns].copy()
    projected[x_column] = pd.to_numeric(projected[x_column], errors="coerce")
    numeric_filtered = projected[x_column]
    missing_or_invalid = int(numeric_filtered.isna().sum())
    non_finite = int((numeric_filtered.notna() & ~np.isfinite(numeric_filtered)).sum())
    finite_mask = numeric_filtered.notna() & np.isfinite(numeric_filtered)
    finite_frame = projected.loc[finite_mask].copy()
    if len(finite_frame) < 2:
        raise ValueError(
            "Die X-Achse enthält nach Filtern und Bereinigung zu wenige endliche Zahlenwerte."
        )
    finite_values = finite_frame[x_column].to_numpy(dtype=float, copy=False)
    validate_numeric_range(finite_values)

    hue_missing = (
        int(finite_frame[requested_hues].isna().any(axis=1).sum())
        if requested_hues
        else 0
    )
    original_grouped_frame = (
        finite_frame.dropna(subset=requested_hues) if requested_hues else finite_frame
    )
    grouper: str | list[str] | None = None
    original_grouping: Any = None
    original_sizes: Any = None
    if requested_hues:
        grouper = requested_hues[0] if len(requested_hues) == 1 else requested_hues
        original_grouping = original_grouped_frame.groupby(
            grouper, sort=False, observed=True
        )
        original_sizes = original_grouping.size()
        original_group_count = int(len(original_sizes))
        original_min_group_size = (
            int(original_sizes.min()) if original_group_count else None
        )
        share_blocked_group_count = int(
            (original_sizes < MIN_SHARED_GROUP_SIZE).sum()
        )
    else:
        original_group_count = 1
        original_min_group_size = int(len(original_grouped_frame))
        share_blocked_group_count = int(
            len(original_grouped_frame) < MIN_SHARED_GROUP_SIZE
        )

    top_n = parse_segment_top_n(requested_hues)
    cardinalities: dict[str, dict[str, int]] = {}
    segment_other_labels: dict[str, str] = {}
    for hue in requested_hues:
        original_cardinality = int(finite_frame[hue].nunique(dropna=True))
        if hue in top_n:
            counts = finite_frame[hue].value_counts(dropna=True)
            ranked = sorted(
                counts.items(), key=lambda item: (-int(item[1]), natural_sort_key(item[0]))
            )
            keep = {value for value, _ in ranked[: top_n[hue]]}
            existing = {str(value) for value in counts.index}
            other_label = "Sonstige"
            if other_label in existing:
                other_label = "Sonstige (gebündelt)"
            suffix = 2
            while other_label in existing:
                other_label = f"Sonstige (gebündelt {suffix})"
                suffix += 1
            sentinel = OtherCategory(hue, other_label)
            source = finite_frame[hue].astype("object")
            finite_frame[hue] = source.where(
                source.isna() | source.isin(keep), sentinel
            )
            segment_other_labels[hue] = other_label
        cardinalities[hue] = {
            "original": original_cardinality,
            "effective": int(finite_frame[hue].nunique(dropna=True)),
        }

    grouped_frame = (
        finite_frame.dropna(subset=requested_hues) if requested_hues else finite_frame
    )
    grouped: Any = None
    if requested_hues:
        if not top_n:
            grouped_frame = original_grouped_frame
            grouped = original_grouping
            effective_sizes = original_sizes
        else:
            grouped = grouped_frame.groupby(grouper, sort=False, observed=True)
            effective_sizes = grouped.size()
        size_values = effective_sizes.to_numpy(dtype=np.int64, copy=False)
        observed_group_count = int(len(effective_sizes))
        curve_count = int(np.count_nonzero(size_values >= 2))
        small_group_count = int(np.count_nonzero(size_values < 2))
        omitted_small_group_rows = int(size_values[size_values < 2].sum())
        plotted_rows = int(size_values[size_values >= 2].sum())
        size_items = list(islice(effective_sizes.items(), max(MAX_CURVES, 100)))
    else:
        size_items = [(tuple(), int(len(grouped_frame)))]
        observed_group_count = 1
        curve_count = int(len(grouped_frame) >= 2)
        small_group_count = int(len(grouped_frame) < 2)
        omitted_small_group_rows = (
            int(len(grouped_frame)) if len(grouped_frame) < 2 else 0
        )
        plotted_rows = int(len(grouped_frame)) if len(grouped_frame) >= 2 else 0
    if enforce_curve_limit and observed_group_count > MAX_CURVES:
        raise ValueError(
            f"Die Segmentierung erzeugt {observed_group_count} Gruppen und überschreitet das Limit von {MAX_CURVES}. Bitte Top N oder stärkere Filter verwenden."
        )

    bounded_items = size_items
    metadata = _group_metadata([name for name, _ in bounded_items], requested_hues)
    all_group_sizes: list[tuple[str, int]] = []
    for name, count in bounded_items:
        parts = _group_name_parts(name) if requested_hues else tuple()
        canonical, _, _, _, _ = _segment_descriptor(requested_hues, parts)
        label = metadata[canonical][0]
        if browser_text_length(f"{label} (n={int(count)})") > 500:
            raise ValueError("Eine Segmentbezeichnung überschreitet maximal 500 Zeichen.")
        all_group_sizes.append((label, int(count)))

    groups: list[AnalysisGroup] = []
    if materialize_groups:
        if observed_group_count > MAX_CURVES:
            raise ValueError(
                f"Die Segmentierung erzeugt {observed_group_count} Gruppen und überschreitet das Limit von {MAX_CURVES}."
            )
        if requested_hues:
            for name, group in grouped:
                if len(group) < 2:
                    continue
                parts = _group_name_parts(name)
                canonical, _, _, _, _ = _segment_descriptor(requested_hues, parts)
                label, values, other, key = metadata[canonical]
                groups.append(
                    AnalysisGroup(label, values, other, key, group[x_column])
                )
            groups.sort(key=lambda item: natural_sort_key(item.label))
        elif len(grouped_frame) >= 2:
            canonical, _, values, other, key = _segment_descriptor([], tuple())
            label = metadata[canonical][0]
            groups = [AnalysisGroup(label, values, other, key, grouped_frame[x_column])]

    return PreparedAnalysis(
        x_column=x_column,
        hues=requested_hues,
        filter_tree=filter_tree,
        filter_summary=filter_summary,
        groups=groups,
        all_group_sizes=all_group_sizes,
        observed_group_count=observed_group_count,
        curve_count=curve_count,
        small_group_count=small_group_count,
        omitted_small_group_rows=omitted_small_group_rows,
        original_group_count=original_group_count,
        original_min_group_size=original_min_group_size,
        share_blocked_group_count=share_blocked_group_count,
        filtered_rows=filtered_rows,
        analyzed_rows=int(len(finite_frame)),
        plotted_rows=plotted_rows,
        exclusions={
            "x_missing_or_invalid": missing_or_invalid,
            "x_non_finite": non_finite,
            "x_total": missing_or_invalid + non_finite,
            "hue_missing": hue_missing,
            "omitted_small_group_rows": omitted_small_group_rows,
        },
        cardinalities=cardinalities,
        segment_top_n=top_n,
        segment_other_labels=segment_other_labels,
    )


@app.get("/")
def index() -> str:
    return render_template("index.html", version=APP_VERSION)


@app.get("/health")
def health() -> Response:
    return jsonify(
        status="ok",
        version=APP_VERSION,
        uptime_seconds=int(time.monotonic() - APP_STARTED_MONOTONIC),
        upload_cache=UPLOAD_CACHE.stats(),
        analysis={
            **ANALYSIS_METRICS.snapshot(),
            "max_concurrent_per_worker": MAX_CONCURRENT_ANALYSES,
            "kde_max_sample_size": KDE_MAX_SAMPLE_SIZE,
            "max_shared_json_bytes": MAX_SHARED_JSON_BYTES,
            "state_scope": "process_local",
        },
        inspection={
            "max_concurrent_per_worker": MAX_CONCURRENT_INSPECTIONS,
        },
    )


@app.get("/api/version")
def version() -> Response:
    response = jsonify(version=APP_VERSION)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/share-key")
def share_key() -> Response:
    keys = [
        {
            "key_id": key_id,
            "public_key": base64url_encode(public_key),
            "current": key_id == SHARE_KEY_ID,
        }
        for key_id, public_key in SHARE_PUBLIC_KEYRING.items()
    ]
    response = jsonify(
        algorithm="Ed25519",
        key_id=SHARE_KEY_ID,
        public_key=base64url_encode(SHARE_PUBLIC_KEY),
        current={
            "key_id": SHARE_KEY_ID,
            "public_key": base64url_encode(SHARE_PUBLIC_KEY),
        },
        keys=keys,
        public_keys={item["key_id"]: item["public_key"] for item in keys},
        keyring={item["key_id"]: item["public_key"] for item in keys},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/service-worker.js")
def service_worker() -> Response:
    source = (BASE_DIR / "static" / "service-worker.js").read_text(encoding="utf-8")
    response = Response(
        source.replace("__APP_VERSION__", APP_VERSION),
        content_type="application/javascript; charset=utf-8",
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.post("/api/inspect")
@rate_limited("inspect", RATE_LIMIT_INSPECT)
@inspect_concurrency_limited
def inspect_file() -> Response:
    owner = client_identity()
    requested_token = request.form.get("upload_token", "").strip()
    if requested_token:
        cached = UPLOAD_CACHE.get(requested_token, owner)
        if cached is None:
            raise ValueError(
                "Der temporäre Upload ist abgelaufen oder gehört zu einer anderen Sitzung. Bitte die Datei erneut einlesen."
            )
        dataset = parse_dataset_bytes(cached.raw, cached.filename)
    else:
        dataset = parse_uploaded_dataset()
    columns, warnings = column_inspection(dataset)
    token, expires_in = UPLOAD_CACHE.put(
        dataset, owner, token=requested_token or None
    )
    preview = [
        {str(key): json_value(value) for key, value in row.items()}
        for row in dataset.frame.head(10).to_dict(orient="records")
    ]
    return jsonify(
        filename=dataset.filename,
        rows=int(len(dataset.frame)),
        columns=columns,
        preview=preview,
        encoding=dataset.parse_options["encoding"],
        encoding_value=dataset.encoding.lower().replace("_", "-"),
        delimiter=dataset.parse_options["delimiter_label"],
        delimiter_value=dataset.separator,
        decimal=dataset.decimal_separator,
        thousands=dataset.thousands_separator,
        parse_options=dataset.parse_options,
        effective_parse_options=dataset.parse_options,
        data_quality_warnings=warnings,
        upload_token=token,
        upload_expires_in=expires_in,
    )


@app.post("/api/estimate")
@rate_limited("estimate", RATE_LIMIT_ESTIMATE)
@concurrency_limited
def estimate() -> Response:
    dataset, upload_source = dataset_from_request()
    prepared = prepare_analysis(
        dataset, materialize_groups=False, enforce_curve_limit=False
    )
    group_sizes = [
        {"label": label, "count": count, "eligible": count >= 2}
        for label, count in prepared.all_group_sizes[:100]
    ]
    curve_count = prepared.curve_count
    exceeds = prepared.observed_group_count > MAX_CURVES or curve_count > MAX_CURVES
    return jsonify(
        filtered_rows=prepared.filtered_rows,
        analyzed_rows=prepared.analyzed_rows,
        plotted_rows=prepared.plotted_rows,
        curve_count=curve_count,
        observed_group_count=prepared.observed_group_count,
        group_sizes=group_sizes,
        group_sizes_truncated=prepared.observed_group_count > len(group_sizes),
        small_group_count=prepared.small_group_count,
        omitted_small_group_count=prepared.small_group_count,
        omitted_small_group_rows=prepared.omitted_small_group_rows,
        share_blocked_group_count=prepared.share_blocked_group_count,
        original_group_count=prepared.original_group_count,
        original_min_group_size=prepared.original_min_group_size,
        exceeds_curve_limit=exceeds,
        warning=(
            f"Die Segmentierung erzeugt mehr als {MAX_CURVES} Gruppen. Bitte Top N oder Filter verwenden."
            if exceeds
            else None
        ),
        cardinalities=prepared.cardinalities,
        segment_top_n=prepared.segment_top_n,
        segment_other_labels=prepared.segment_other_labels,
        exclusions=prepared.exclusions,
        upload_source=upload_source,
    )


@app.post("/api/analyze")
@rate_limited("analyze", RATE_LIMIT_ANALYZE)
@concurrency_limited
def analyze() -> Response:
    started = time.perf_counter()
    success = False
    try:
        load_started = time.perf_counter()
        dataset, upload_source = dataset_from_request()
        load_ms = (time.perf_counter() - load_started) * 1000
        prepare_started = time.perf_counter()
        prepared = prepare_analysis(dataset)
        prepare_ms = (time.perf_counter() - prepare_started) * 1000
        if not prepared.groups:
            raise ValueError("Für die gewählte Segmentierung sind zu wenige Daten vorhanden.")

        bandwidth = parse_bandwidth()
        share_expiry_days = parse_share_expiry_days()
        column_config = parse_column_config(dataset.frame)
        curves: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        kde_started = time.perf_counter()
        for group in prepared.groups:
            display = f"{group.label} (n={len(group.series)})"
            curve = curve_for(group.series, bandwidth, salt=group.segment_key)
            curve.update(
                {
                    "label": display,
                    "segment_values": group.segment_values,
                    "segment_other": group.segment_other,
                    "segment_key": group.segment_key,
                }
            )
            curves.append(curve)
            rows.append(
                statistic_row(
                    display,
                    group.series,
                    density_mode=curve["density_mode"],
                    modality_metadata=curve["modality_heuristic"],
                )
            )
        kde_ms = (time.perf_counter() - kde_started) * 1000

        bandwidth_value: str | float = bandwidth
        display_x_label = display_column_label(prepared.x_column, column_config)
        methodology = {
            "kde": {
                "bandwidth": bandwidth_value,
                "numeric_bandwidth_interpretation": "scott_multiplier",
                "max_sample_size": KDE_MAX_SAMPLE_SIZE,
                "sampling": "deterministic_without_replacement",
                "evaluation_points": 320,
            },
            "modality": {
                "heuristic": True,
                "method": "kde_peak_prominence",
                "prominence_ratio": 0.03,
                "note": "Die Modalität ist eine bandbreitenabhängige heuristische Schätzung.",
            },
            "mad": "median_absolute_deviation",
            "mean_ci": "two-sided Student-t 95% confidence interval",
        }
        result = {
            "x_label": prepared.x_column,
            "display_x_label": display_x_label,
            "source_rows": prepared.analyzed_rows,
            "filtered_rows": prepared.filtered_rows,
            "plotted_rows": prepared.plotted_rows,
            "omitted_small_group_count": prepared.small_group_count,
            "omitted_small_group_rows": prepared.omitted_small_group_rows,
            "curves": curves,
            "statistics": rows,
            "exclusions": prepared.exclusions,
            "cardinalities": prepared.cardinalities,
            "segment_top_n": prepared.segment_top_n,
            "segment_other_labels": prepared.segment_other_labels,
            "column_config": column_config,
            "segment_columns": [
                {
                    "name": hue,
                    "display_name": display_column_label(hue, column_config),
                }
                for hue in prepared.hues
            ],
            "methodology": methodology,
        }
        reproducibility = {
            "parse_options": dataset.parse_options,
            "x_column": prepared.x_column,
            "hues": prepared.hues,
            "bandwidth": bandwidth_value,
            "exclusions": prepared.exclusions,
            "omitted_small_group_count": prepared.small_group_count,
            "omitted_small_group_rows": prepared.omitted_small_group_rows,
            "segment_top_n": prepared.segment_top_n,
            "segment_other_labels": prepared.segment_other_labels,
            "column_config": column_config,
            "kde_max_sample_size": KDE_MAX_SAMPLE_SIZE,
        }
        share_material = None
        share_blocked_reason = None
        if prepared.share_blocked_group_count:
            share_blocked_reason = (
                f"Freigabe gesperrt: {prepared.share_blocked_group_count} ursprüngliche "
                f"Segmentgruppe(n) haben weniger als n = {MIN_SHARED_GROUP_SIZE}."
            )
        else:
            candidate = signed_share_material(
                result,
                prepared.filter_summary,
                reproducibility,
                share_expiry_days,
            )
            payload_size, envelope_size = share_material_size(candidate)
            if max(payload_size, envelope_size) > MAX_SHARED_JSON_BYTES:
                share_blocked_reason = (
                    "Das Ergebnis ist für einen Freigabelink zu groß "
                    f"(Limit {MAX_SHARED_JSON_BYTES} Bytes). Bitte weniger Segmente verwenden."
                )
            else:
                candidate["payload_size_bytes"] = payload_size
                candidate["envelope_size_bytes"] = envelope_size
                share_material = candidate
        elapsed_ms = (time.perf_counter() - started) * 1000
        timings = {
            "load": round(load_ms, 2),
            "prepare": round(prepare_ms, 2),
            "kde_and_statistics": round(kde_ms, 2),
            "total": round(elapsed_ms, 2),
        }
        success = True
        ANALYSIS_METRICS.record(elapsed_ms, True)
        app.logger.info(
            compact_json(
                {
                    "event": "analysis_complete",
                    "duration_ms": timings,
                    "rows": prepared.analyzed_rows,
                    "curves": len(curves),
                    "upload_source": upload_source,
                    "kde_sampled_curves": sum(curve["kde_sampled"] for curve in curves),
                }
            )
        )
        return jsonify(
            **result,
            share=share_material,
            share_blocked_reason=share_blocked_reason,
            reproducibility=reproducibility,
            upload_source=upload_source,
            timing_ms=timings,
        )
    finally:
        if not success:
            elapsed_ms = (time.perf_counter() - started) * 1000
            ANALYSIS_METRICS.record(elapsed_ms, False)
            app.logger.log(
                logging.WARNING,
                compact_json(
                    {
                        "event": "analysis_failed",
                        "duration_ms": round(elapsed_ms, 2),
                    }
                ),
            )


@app.errorhandler(ValueError)
def handle_value_error(error: ValueError) -> tuple[Response, int]:
    return jsonify(error=str(error)), 400


@app.errorhandler(413)
def file_too_large(_: Exception) -> tuple[Response, int]:
    return (
        jsonify(
            error=f"Die Datei überschreitet das Limit von {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
        ),
        413,
    )


if __name__ == "__main__":
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
