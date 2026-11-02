import os
import re
import sys
import time
import json
import ctypes
import argparse
import threading
from datetime import datetime
from typing import Tuple, List, Dict, Any, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from html import unescape as html_unescape
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote, quote

# Optional Pillow import: Used for WebP/PNG -> JPG conversion and dimension analysis.
try:
    from PIL import Image, ExifTags
except ImportError:
    Image = None
    ExifTags = None

import requests
from playwright.sync_api import sync_playwright
from tqdm import tqdm

# Tkinter import for GUI mode
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, scrolledtext
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False

# Configuration & Constants
DEFAULT_OUTPUT_DIR = "downloads"


def get_usable_cpu_count() -> int:
    """Return the number of logical CPUs available to this process."""
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if callable(process_cpu_count):
        try:
            cpu_count = process_cpu_count()
        except OSError:
            cpu_count = None
        if cpu_count:
            return max(1, cpu_count)

    get_affinity = getattr(os, "sched_getaffinity", None)
    if callable(get_affinity):
        try:
            return max(1, len(get_affinity(0)))
        except OSError:
            pass

    return max(1, os.cpu_count() or 1)


DEFAULT_MAX_WORKERS = get_usable_cpu_count()
DEFAULT_MIN_IMAGE_SIZE = 300
DEFAULT_SAVE_JSON = False
DEFAULT_SAVE_PHOTO_METADATA = False
DEFAULT_CONVERT_JPG = True
DEFAULT_ENABLE_DEEP_SCAN = True
DEFAULT_ID_PROBE_RANGE = 150
DEFAULT_DEBUG_MODE = True

PHOTO_FILENAME_PATTERN = re.compile(
    r"[A-Za-z0-9_-]{1,80}\.(?:webp|jpe?g|png|avif)",
    re.IGNORECASE,
)

SESSION_FILE = "vinted_session.json"
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(APP_DIR, "vinted_settings.json")
LEGACY_SETTINGS_FILE = os.path.join(os.getcwd(), "vinted_settings.json")
LOG_DIR = os.path.join(APP_DIR, "logs")


def load_app_settings() -> Dict[str, Any]:
    candidates = [SETTINGS_FILE]
    if os.path.normcase(os.path.abspath(LEGACY_SETTINGS_FILE)) != os.path.normcase(os.path.abspath(SETTINGS_FILE)):
        candidates.append(LEGACY_SETTINGS_FILE)

    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(f"[!] Could not load settings from '{path}': {exc}")
            continue

        if isinstance(data, dict):
            if path != SETTINGS_FILE and not save_app_settings(data):
                print(f"[!] Could not migrate settings to '{SETTINGS_FILE}'.")
            return data

        print(f"[!] Ignoring settings file with invalid format: '{path}'.")
    return {}


def save_app_settings(settings: Dict[str, Any]) -> bool:
    payload = dict(settings)
    payload["_version"] = 1
    temp_path = SETTINGS_FILE + ".tmp"
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, SETTINGS_FILE)
        return True
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        print(f"[!] Could not save settings to '{SETTINGS_FILE}': {exc}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError as cleanup_error:
            print(f"[!] Could not remove temporary settings file '{temp_path}': {cleanup_error}")
        return False


def save_session_state(context: Any) -> None:
    """Write Playwright session state atomically without changing its legacy path."""
    state = context.storage_state()
    temp_path = SESSION_FILE + ".pending"
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, SESSION_FILE)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError as cleanup_error:
            print(f"[!] Could not remove temporary session file '{temp_path}': {cleanup_error}")
        raise OSError(f"Could not save session to '{SESSION_FILE}': {exc}") from exc


class DebugFileLogger:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handle = None
        self.path: Optional[str] = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._handle is not None and not self._handle.closed

    def enable(self, new_file: bool = False) -> Optional[str]:
        with self._lock:
            if self.enabled and not new_file:
                return self.path
            self.close()
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                path = os.path.join(LOG_DIR, f"vinted_{stamp}.log")
                suffix = 1
                while os.path.exists(path):
                    path = os.path.join(LOG_DIR, f"vinted_{stamp}_{suffix}.log")
                    suffix += 1
                self._handle = open(path, "a", encoding="utf-8", buffering=1)
                self.path = path
                self.write("[INFO] Debug logging enabled")
                return path
            except OSError as exc:
                handle = self._handle
                self._handle = None
                self.path = None
                print(f"[!] Could not start debug logging: {exc}")
                if handle is not None:
                    try:
                        handle.close()
                    except OSError as close_error:
                        print(f"[!] Could not close debug log after startup failure: {close_error}")
                return None

    def write(self, message: str) -> None:
        with self._lock:
            if self._handle is None or self._handle.closed:
                return
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            try:
                self._handle.write(f"[{timestamp}] {message}\n")
                self._handle.flush()
            except OSError as exc:
                print(f"[!] Could not write to debug log '{self.path}': {exc}")
                handle = self._handle
                self._handle = None
                try:
                    handle.close()
                except OSError as close_error:
                    print(f"[!] Could not close debug log '{self.path}': {close_error}")

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            handle = self._handle
            self.write("[INFO] Debug logging closed")
            if self._handle is handle:
                try:
                    handle.close()
                except OSError as exc:
                    print(f"[!] Could not close debug log '{self.path}': {exc}")
                finally:
                    self._handle = None


DEBUG_LOGGER = DebugFileLogger()
# Current public Vinted market domains (October 2026). Not every market shares
# listings/shipping with every other market, but public item pages can still be
# indexed separately, so Deep Scan should consider each host independently.
VINTED_DOMAINS = [
    "vinted.at", "vinted.be", "vinted.com", "vinted.co.uk", "vinted.cz",
    "vinted.de", "vinted.dk", "vinted.ee", "vinted.es", "vinted.fi",
    "vinted.fr", "vinted.gr", "vinted.hr", "vinted.hu", "vinted.ie",
    "vinted.it", "vinted.lt", "vinted.lu", "vinted.lv", "vinted.nl",
    "vinted.pl", "vinted.pt", "vinted.ro", "vinted.se", "vinted.si",
    "vinted.sk",
]


def sanitize_folder_name(name: str) -> str:
    """Sanitizes folder and file names by removing invalid characters."""
    if not name:
        return "unnamed_item"
    name = re.sub(r'[\r\n\t]+', ' ', str(name))
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip()


def parse_item_date(item: Dict[str, Any]) -> Tuple[datetime, str]:
    """Deeply searches item metadata for creation timestamps."""
    ts = None
    for key in ["created_at_ts", "created_at", "updated_at_ts", "user_updated_at_ts"]:
        if item.get(key):
            ts = item.get(key)
            break

    if not ts and item.get("photos") and isinstance(item["photos"], list):
        for photo in item["photos"]:
            if isinstance(photo, dict):
                for p_key in ["high_resolution", "full_size"]:
                    if photo.get(p_key) and isinstance(photo[p_key], dict):
                        ts = photo[p_key].get("timestamp")
                        if ts:
                            break
                if not ts:
                    ts = photo.get("created_at") or photo.get("timestamp")
                if ts:
                    break

    dt = None
    if isinstance(ts, (int, float)):
        if ts > 10000000000:
            ts = ts / 1000
        try:
            dt = datetime.fromtimestamp(ts)
        except Exception:
            pass
    elif isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            pass

    if not dt:
        dt = datetime.now()

    return dt, dt.strftime("%Y-%m-%d")


def set_file_creation_time(filepath: str, dt: datetime) -> None:
    """Sets file creation and modification timestamps on Windows and Linux."""
    try:
        timestamp = dt.timestamp()
        os.utime(filepath, (timestamp, timestamp))

        if os.name == 'nt':
            from ctypes import wintypes
            FILE_WRITE_ATTRIBUTES = 0x0100
            OPEN_EXISTING = 3
            
            filetime = int((timestamp + 11644473600) * 10000000)
            ctime = wintypes.FILETIME(filetime & 0xFFFFFFFF, filetime >> 32)
            
            handle = ctypes.windll.kernel32.CreateFileW(
                filepath, FILE_WRITE_ATTRIBUTES, 0, None, OPEN_EXISTING, 0, None
            )
            if handle != -1:
                ctypes.windll.kernel32.SetFileTime(handle, ctypes.byref(ctime), None, None)
                ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def get_image_urls(photo: Any) -> List[str]:
    """Return known photo URLs, preferring explicit original/full-size fields.

    Vinted's embedded HTML payloads are not always identical to the public API:
    some photos expose full_size_url directly, others wrap URL variants in nested
    objects or lists. Collect these without assuming a thumbnail is the original.
    """
    if isinstance(photo, str):
        raw_values = [photo]
    elif isinstance(photo, dict):
        raw_values: List[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str) and value.strip():
                value = value.strip()
                if value.startswith('//'):
                    value = 'https:' + value
                if value.startswith(('https://', 'http://')) and value not in raw_values:
                    raw_values.append(value)

        # Explicit high-quality fields always win.
        for key in ('full_size_url', 'fullSizeUrl', 'original_url', 'originalUrl', 'original_photo_url', 'originalPhotoUrl'):
            add(photo.get(key))

        priority_objects = (
            'high_resolution', 'highResolution', 'full_size', 'fullSize',
            'original', 'extra_large', 'extraLarge', 'large', 'medium',
        )
        for key in priority_objects:
            value = photo.get(key)
            if isinstance(value, dict):
                for url_key in ('full_size_url', 'fullSizeUrl', 'original_url', 'originalUrl', 'url', 'src'):
                    add(value.get(url_key))
            elif isinstance(value, str):
                add(value)

        # URL collections appear as either arrays or dictionaries depending on
        # which Vinted response/hydration version produced the photo object.
        for key in ('urls', 'url_options', 'image_urls', 'urlOptions', 'imageUrls'):
            value = photo.get(key)
            if isinstance(value, dict):
                priority = ('full_size', 'fullSize', 'original', 'high_resolution', 'highResolution', 'extra_large', 'extraLarge', 'large', 'f2400', 'f2000', 'f1600', 'f1200', 'f1000', 'f800', 'url')
                for variant_key in priority:
                    variant = value.get(variant_key)
                    if isinstance(variant, dict):
                        add(variant.get('url') or variant.get('src'))
                    else:
                        add(variant)
                for variant in value.values():
                    if isinstance(variant, dict):
                        add(variant.get('url') or variant.get('src'))
                    else:
                        add(variant)
            elif isinstance(value, list):
                variants = []
                for variant in value:
                    if isinstance(variant, str):
                        variants.append((0, variant))
                    elif isinstance(variant, dict):
                        width = int(variant.get('width') or 0) if str(variant.get('width') or '').isdigit() else 0
                        height = int(variant.get('height') or 0) if str(variant.get('height') or '').isdigit() else 0
                        variants.append((width * height, variant.get('url') or variant.get('src') or ''))
                for _, variant_url in sorted(variants, key=lambda pair: pair[0], reverse=True):
                    add(variant_url)

        thumbnails = photo.get('thumbnails')
        if isinstance(thumbnails, list):
            thumbnails = sorted(
                (t for t in thumbnails if isinstance(t, dict) and (t.get('url') or t.get('src'))),
                key=lambda t: (t.get('width', 0) or 0) * (t.get('height', 0) or 0),
                reverse=True,
            )
            for thumbnail in thumbnails:
                add(thumbnail.get('url') or thumbnail.get('src'))

        add(photo.get('url'))
        # Limited recursive fallback for URL fields nested under future schema
        # names (skip seller/profile URLs by only accepting Vinted image CDN hosts).
        stack = [(photo, 0)]
        seen = set()
        nested = []
        while stack:
            value, depth = stack.pop()
            if depth > 5:
                continue
            if isinstance(value, dict):
                if id(value) in seen:
                    continue
                seen.add(id(value))
                for key, child in value.items():
                    key_norm = re.sub(r'[^a-z0-9]', '', str(key).lower())
                    if isinstance(child, str) and ('url' in key_norm or key_norm in {'src', 'image'}):
                        if re.search(r'(?:images\d*\.vinted\.net|/t/|/tc/)', child, re.I):
                            nested.append(child)
                    elif isinstance(child, (dict, list)):
                        stack.append((child, depth + 1))
            elif isinstance(value, list):
                stack.extend((child, depth + 1) for child in value if isinstance(child, (dict, list)))
        for value in nested:
            add(value)
    else:
        return []

    # Normalize duplicates while preserving the preferred-source order.
    unique_urls = []
    for value in raw_values:
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if value.startswith('//'):
            value = 'https:' + value
        if value not in unique_urls:
            unique_urls.append(value)
    return unique_urls


_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff")


def _normalise_image_filename(value: Any, image_urls: Optional[List[str]] = None) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip('"\'')
    if not candidate:
        return None

    try:
        if candidate.startswith(("http://", "https://", "//")):
            candidate = unquote(urlsplit(candidate).path)
    except (TypeError, ValueError):
        return None
    candidate = candidate.split("?", 1)[0].split("#", 1)[0]
    candidate = re.split(r"[\\/]", candidate)[-1].strip().rstrip(".")
    if not candidate or candidate in {".", ".."}:
        return None

    candidate = sanitize_folder_name(candidate).strip().rstrip(". ")
    if not candidate:
        return None

    root, ext = os.path.splitext(candidate)
    if ext.lower() not in _IMAGE_EXTENSIONS:
        inferred_ext = ""
        for url in image_urls or []:
            try:
                url_ext = os.path.splitext(unquote(urlsplit(url).path))[1].lower()
            except (TypeError, ValueError):
                continue
            if url_ext in _IMAGE_EXTENSIONS:
                inferred_ext = url_ext
                break
        if not inferred_ext:
            return None
        candidate = candidate + inferred_ext

    stem = os.path.splitext(candidate)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
        candidate = "_" + candidate
    return candidate


def get_original_photo_filename(photo: Any, image_urls: Optional[List[str]] = None) -> Optional[Tuple[str, str]]:
    if not isinstance(photo, dict):
        return None

    strong_keys = {
        "originalfilename", "sourcefilename", "uploadfilename",
        "uploaderfilename", "clientfilename", "originalname",
    }
    ordinary_keys = {"filename", "file"}
    generic_keys = {"name"}
    stack = [photo]
    dictionaries: List[Dict[str, Any]] = []
    seen = set()
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if id(value) in seen:
                continue
            seen.add(id(value))
            dictionaries.append(value)
            stack.extend(v for v in value.values() if isinstance(v, (dict, list)))
        elif isinstance(value, list):
            stack.extend(value)

    def normal_key(key: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", str(key).lower())

    for allowed_keys in (strong_keys, ordinary_keys, generic_keys):
        for obj in dictionaries:
            for key, value in obj.items():
                if normal_key(key) not in allowed_keys or not isinstance(value, str):
                    continue
                filename = _normalise_image_filename(value, image_urls)
                if filename:
                    return filename, str(key)
    return None


def _filename_from_url(urls: List[str], photo: Any, index: int) -> Tuple[str, str]:
    for url in urls:
        try:
            path = unquote(urlsplit(url).path)
            candidate = os.path.basename(path.rstrip("/"))
            if candidate and candidate.lower().endswith(_IMAGE_EXTENSIONS):
                cleaned = _normalise_image_filename(candidate, urls)
                if cleaned:
                    return cleaned, "vinted_cdn_url"
        except (TypeError, ValueError):
            continue

    photo_id = photo.get("id") if isinstance(photo, dict) else None
    if photo_id is not None:
        safe_id = sanitize_folder_name(str(photo_id))
        if safe_id:
            return f"photo_{safe_id}.jpg", "photo_id"
    return f"image_{index}.jpg", "generated_fallback"


def get_photo_filename_and_source(photo: Any, urls: List[str], index: int) -> Tuple[str, str, Optional[str]]:
    found_name = get_original_photo_filename(photo, urls)
    if found_name:
        key = re.sub(r"[^a-z0-9]", "", found_name[1].lower())
        if key in {"originalfilename", "sourcefilename", "uploadfilename", "uploaderfilename", "clientfilename", "originalname"}:
            return found_name[0], "original_metadata", found_name[1]
        return found_name[0], "photo_metadata_filename", found_name[1]
    fallback_name, fallback_source = _filename_from_url(urls, photo, index)
    return fallback_name, fallback_source, None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _inspect_image_content(content: bytes) -> Dict[str, Any]:
    width, height = _get_image_dimensions(content)
    info: Dict[str, Any] = {
        "pixel_width": width or None,
        "pixel_height": height or None,
        "format": None,
        "mode": None,
        "exif": {},
    }
    if Image is None:
        if content.startswith(b"\xff\xd8\xff"):
            info["format"] = "JPEG"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            info["format"] = "PNG"
        elif content.startswith((b"GIF87a", b"GIF89a")):
            info["format"] = "GIF"
        elif len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            info["format"] = "WEBP"
        return info

    try:
        with Image.open(BytesIO(content)) as image:
            info["format"] = image.format
            info["mode"] = image.mode
            info["pixel_width"], info["pixel_height"] = image.size
            exif_data: Dict[str, Any] = {}
            exif = image.getexif()
            if ExifTags is not None:
                for tag_id, value in exif.items():
                    tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                    if tag_name == "GPSInfo":
                        continue
                    if isinstance(value, (str, int, float, bytes, tuple, list)):
                        exif_data[tag_name] = _json_safe(value)
                try:
                    exif_ifd_id = getattr(getattr(ExifTags, "IFD", object), "Exif", 34665)
                    sub_exif = exif.get_ifd(exif_ifd_id)
                    for tag_id, value in sub_exif.items():
                        tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                        if tag_name in {"GPSInfo", "MakerNote", "UserComment"}:
                            continue
                        if isinstance(value, (str, int, float, bytes, tuple, list)):
                            exif_data[tag_name] = _json_safe(value)
                except Exception:
                    pass
            info["exif"] = exif_data
    except Exception:
        pass
    return info


def _filename_from_content_disposition(value: Optional[str], image_urls: Optional[List[str]] = None) -> Optional[str]:
    if not value:
        return None
    match = re.search(r"filename\*\s*=\s*([^;]+)", value, flags=re.IGNORECASE)
    if match:
        raw = match.group(1).strip().strip('"')
        if "''" in raw:
            raw = raw.split("''", 1)[1]
        try:
            raw = unquote(raw)
        except Exception:
            pass
    else:
        match = re.search(r"filename\s*=\s*(?:\"([^\"]+)\"|([^;]+))", value, flags=re.IGNORECASE)
        if not match:
            return None
        raw = (match.group(1) or match.group(2) or "").strip().strip('"')
    return _normalise_image_filename(raw, image_urls)


def _write_photo_metadata(metadata_by_folder: Dict[str, Dict[str, Any]]) -> None:
    for folder_path, payload in metadata_by_folder.items():
        try:
            output_path = os.path.join(folder_path, "photo_metadata.json")
            with open(output_path, "w", encoding="utf-8") as handle:
                json.dump(_json_safe(payload), handle, indent=4, ensure_ascii=False)
        except (OSError, TypeError, ValueError):
            pass


def _image_url_candidates(url: str) -> List[str]:
    """Generate likely Vinted CDN full-size URLs without reusing stale URL signatures.

    Vinted commonly serves page images as /t/<asset>/f800/<file>, while its
    full-size form uses /tc/<asset>/<file>. The `s=` query token can be specific
    to the original path, so transformed paths are tried without that token first.
    """
    if not url:
        return []
    if url.startswith("//"):
        url = "https:" + url
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError):
        return [url]
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return [url]

    host = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or ""
    original_query = parts.query
    query_pairs = parse_qsl(original_query, keep_blank_values=True)
    # Vinted's `s` value can be bound to the exact asset path/resolution.
    unsigned_query = urlencode([(k, v) for k, v in query_pairs if k.lower() not in {"s", "signature"}])

    candidates: List[str] = []

    def add(candidate_path: str, query: str = "") -> None:
        candidate = urlunsplit((parts.scheme, parts.netloc, candidate_path, query, ""))
        if candidate not in candidates:
            candidates.append(candidate)

    asset = None
    filename = None
    match_t = re.match(r"^/t/([^/]+)/([^/]+)/(.+)$", path, re.I)
    match_tc = re.match(r"^/tc/([^/]+)/(.+)$", path, re.I)
    if match_t:
        asset, _size_tag, filename = match_t.groups()
    elif match_tc:
        asset, filename = match_tc.groups()

    if asset and filename:
        # Full-size CDN endpoint is the best candidate; other size labels are
        # fallbacks because not all photo generations support every label.
        target_paths = [
            f"/tc/{asset}/{filename}",
            f"/t/{asset}/full_size/{filename}",
            f"/t/{asset}/f2400/{filename}",
            f"/t/{asset}/f2000/{filename}",
            f"/t/{asset}/f1600/{filename}",
            f"/t/{asset}/f1200/{filename}",
            f"/t/{asset}/f1000/{filename}",
            f"/t/{asset}/f800/{filename}",
            f"/t/{asset}/large/{filename}",
            f"/t/{asset}/extra_large/{filename}",
        ]
        for target_path in target_paths:
            # First attempt a path-safe URL without stale `s=`; if the source
            # used a signature, retain it as a later compatibility fallback.
            add(target_path, unsigned_query)
            if original_query and original_query != unsigned_query:
                add(target_path, original_query)

    # Retain old generic URL upgrades for other URL styles used by Vinted.
    if "/f800/" in path or "/t310/" in path:
        add(path.replace("/f800/", "/full_size/").replace("/t310/", "/full_size/"), unsigned_query)
    if original_query:
        add(path, original_query)
    else:
        add(path, "")
    return candidates


def _get_image_dimensions(content: bytes) -> Tuple[int, int]:
    if Image is not None:
        try:
            with Image.open(BytesIO(content)) as image:
                return image.size
        except Exception:
            pass

    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return (int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big"))

    if content.startswith((b"GIF87a", b"GIF89a")) and len(content) >= 10:
        return (int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little"))

    if content.startswith(b"\xff\xd8"):
        i = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while i + 8 < len(content):
            if content[i] != 0xFF:
                i += 1
                continue
            while i < len(content) and content[i] == 0xFF:
                i += 1
            if i >= len(content):
                break
            marker = content[i]
            i += 1
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            if i + 2 > len(content):
                break
            segment_length = int.from_bytes(content[i:i + 2], "big")
            if segment_length < 2 or i + segment_length > len(content):
                break
            if marker in sof_markers and i + 7 <= len(content):
                height = int.from_bytes(content[i + 3:i + 5], "big")
                width = int.from_bytes(content[i + 5:i + 7], "big")
                if width > 0 and height > 0:
                    return (width, height)
            i += segment_length

    if len(content) >= 30 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        chunk_type = content[12:16]
        if chunk_type == b"VP8X":
            width = 1 + int.from_bytes(content[24:27], "little")
            height = 1 + int.from_bytes(content[27:30], "little")
            if width > 0 and height > 0:
                return (width, height)
        elif chunk_type == b"VP8 " and content[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(content[26:28], "little") & 0x3FFF
            height = int.from_bytes(content[28:30], "little") & 0x3FFF
            if width > 0 and height > 0:
                return (width, height)
        elif chunk_type == b"VP8L" and len(content) >= 25 and content[20] == 0x2F:
            b1, b2, b3, b4 = content[21:25]
            width = 1 + b1 + ((b2 & 0x3F) << 8)
            height = 1 + ((b2 >> 6) | (b3 << 2) | ((b4 & 0x0F) << 10))
            if width > 0 and height > 0:
                return (width, height)

    return (0, 0)


def _image_quality_score(content: bytes) -> Tuple[int, int, int, int]:
    width, height = _get_image_dimensions(content)
    return (width * height, max(width, height), width + height, len(content))


def _looks_like_image(content: bytes) -> bool:
    return (
        content.startswith(b"\xff\xd8")
        or content.startswith(b"\x89PNG\r\n\x1a\n")
        or content.startswith((b"GIF87a", b"GIF89a"))
        or (len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP")
        or (len(content) > 12 and content[4:8] == b"ftyp" and content[8:12] in (b"avif", b"avis"))
    )


def download_single_image(args: Tuple[Any, ...]) -> bool:
    status_callback = None
    debug_mode = False
    if len(args) >= 8:
        url_input, folder_path, filename, item_datetime, photo_record, convert_jpg, status_callback, debug_mode = args[:8]
    elif len(args) >= 6:
        url_input, folder_path, filename, item_datetime, photo_record, convert_jpg = args[:6]
    else:
        url_input, folder_path, filename, item_datetime = args[:4]
        photo_record = None
        convert_jpg = DEFAULT_CONVERT_JPG

    def dlog(message: str) -> None:
        if debug_mode:
            rendered = f"[DEBUG] {message}"
            if status_callback:
                try:
                    status_callback(rendered)
                except Exception:
                    pass
            else:
                print(rendered)
                DEBUG_LOGGER.write(rendered)

    def report_error(message: str) -> None:
        rendered = f"[!] {message}"
        if status_callback:
            status_callback(rendered)
        else:
            print(rendered)
            DEBUG_LOGGER.write(rendered)

    original_filepath = os.path.join(folder_path, filename)
    if convert_jpg and Image is None:
        report_error("Pillow is required to convert images to JPG. Install the project requirements and retry.")
        if photo_record is not None:
            photo_record["download"] = {"status": "conversion_dependency_missing"}
        return False

    if convert_jpg:
        base_stem, _ = os.path.splitext(filename)
        filename = base_stem + ".jpg"

    filepath = os.path.join(folder_path, filename)
    os.makedirs(folder_path, exist_ok=True)

    def remove_converted_source() -> None:
        if not convert_jpg or original_filepath == filepath or not os.path.exists(original_filepath):
            return
        try:
            os.remove(original_filepath)
        except OSError as exc:
            report_error(f"Could not remove the previous non-JPG image '{original_filepath}': {exc}")

    if isinstance(url_input, str):
        source_urls = [url_input]
    else:
        source_urls = list(url_input or [])

    exact_urls: List[str] = []
    for source_url in source_urls:
        if not isinstance(source_url, str) or not source_url.strip():
            continue
        source_url = source_url.strip()
        if source_url.startswith("//"):
            source_url = "https:" + source_url
        if source_url not in exact_urls:
            exact_urls.append(source_url)
    if not exact_urls:
        if photo_record is not None:
            photo_record["download"] = {"status": "no_image_url"}
        return False

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://www.vinted.de/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    best_content: Optional[bytes] = None
    best_score = (-1, -1, -1, -1)
    best_response_info: Dict[str, Any] = {}
    download_status = "download_failed"

    def record_file(content: bytes, status: str) -> None:
        if photo_record is None:
            return
        image_info = _inspect_image_content(content)
        image_info["file_size_bytes"] = len(content)
        photo_record["downloaded_image"] = image_info
        photo_record["download"] = {"status": status, **best_response_info}
        photo_record["saved_filename"] = os.path.basename(filepath)

    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        try:
            with open(filepath, "rb") as existing_file:
                existing_content = existing_file.read()
            if _looks_like_image(existing_content):
                existing_score = _image_quality_score(existing_content)
                best_content = existing_content
                best_score = existing_score
            else:
                existing_score = (0, 0, 0, 0)
            if existing_score[0] >= 1_500_000 and existing_score[1] >= 1400:
                # Keep it as the current best candidate, but still probe the
                # public CDN variants because a larger original may exist.
                dlog(f"Bild {filename}: vorhandene Datei {_get_image_dimensions(existing_content)[0]}x{_get_image_dimensions(existing_content)[1]}; prüfe weiterhin mögliche größere Varianten.")
        except OSError:
            pass

    tried = set()
    http_status_counts: Dict[str, int] = {}
    accepted_variant_counts: Dict[str, int] = {}
    transport_error_count = 0

    def _variant_label(candidate_url: str) -> str:
        path = urlsplit(candidate_url).path
        if '/tc/' in path.lower():
            return 'tc'
        match = re.search(r'/t/[^/]+/([^/]+)/', path, re.I)
        if match:
            label = match.group(1)
            if label.lower() in {'full_size', 'large', 'extra_large'}:
                return label
            if re.fullmatch(r'f?\d{2,4}(?:x\d{2,4})?', label, re.I):
                return label
        return 'other'

    def fetch_and_score(candidate_url: str) -> bool:
        nonlocal best_content, best_score, best_response_info, transport_error_count
        if not candidate_url or candidate_url in tried:
            return best_score[0] >= 1_500_000 and best_score[1] >= 1400
        tried.add(candidate_url)
        try:
            response = requests.get(candidate_url, headers=headers, timeout=(4, 8))
            status_key = str(response.status_code)
            http_status_counts[status_key] = http_status_counts.get(status_key, 0) + 1
            if response.status_code != 200:
                return False
            content_type = response.headers.get("content-type", "")
            if not _looks_like_image(response.content):
                accepted_variant_counts['200_non_image'] = accepted_variant_counts.get('200_non_image', 0) + 1
                return False

            variant_label = _variant_label(candidate_url)
            accepted_variant_counts[variant_label] = accepted_variant_counts.get(variant_label, 0) + 1
            score = _image_quality_score(response.content)
            if score[0] <= 0:
                accepted_variant_counts['image_dimensions_unavailable'] = accepted_variant_counts.get('image_dimensions_unavailable', 0) + 1
                return False
            if score > best_score:
                best_content = response.content
                best_score = score
                best_response_info = {
                    "download_source_url": urlsplit(response.url)._replace(query="", fragment="").geturl(),
                    "http_content_type": content_type or None,
                    "http_content_length": response.headers.get("content-length"),
                    "http_etag": response.headers.get("etag"),
                    "http_last_modified": response.headers.get("last-modified"),
                    "http_content_disposition": response.headers.get("content-disposition"),
                    "content_disposition_filename": _filename_from_content_disposition(
                        response.headers.get("content-disposition"), exact_urls
                    ),
                    "http_status": response.status_code,
                }
            return score[0] >= 1_500_000 and score[1] >= 1400
        except requests.RequestException:
            transport_error_count += 1
            return False
        except Exception:
            transport_error_count += 1
            return False

    # Score every source URL instead of stopping at the first image above a size
    # threshold: an URL called `full_size` or `tc` is not guaranteed to be the
    # largest variant available for every Vinted photo generation.
    for candidate_url in exact_urls:
        fetch_and_score(candidate_url)

    generated_variants: List[str] = []
    for source_url in exact_urls:
        for candidate_url in _image_url_candidates(source_url):
            if candidate_url not in tried and candidate_url not in generated_variants:
                generated_variants.append(candidate_url)
    size_priority = {
        "tc": 0,
        "full_size": 1,
        "extra_large": 2,
        "large": 3,
        "f2400": 4,
        "f2000": 5,
        "f1600": 6,
        "f1200": 7,
        "f1000": 8,
        "f800": 9,
    }

    def variant_priority(candidate_url: str) -> Tuple[int, int]:
        path = urlsplit(candidate_url).path
        if re.match(r"^/tc/[^/]+/", path, re.I):
            size_rank = size_priority["tc"]
        else:
            match = re.match(r"^/t/[^/]+/([^/]+)/", path, re.I)
            label = match.group(1).lower() if match else ""
            size_rank = size_priority.get(label, 10)
            if label not in size_priority:
                dimensions = re.fullmatch(r"(\d+)x(\d+)", label)
                if dimensions:
                    size_rank = 10 + 1_000_000_000 // (int(dimensions.group(1)) * int(dimensions.group(2)))
        return size_rank, 0 if urlsplit(candidate_url).query else 1

    generated_variants.sort(key=variant_priority)
    # Try all high-priority signatures and size paths, while bounding fallback
    # requests if a future page exposes an unusually large number of URLs.
    max_variant_attempts = 48
    for candidate_url in generated_variants[:max_variant_attempts]:
        fetch_and_score(candidate_url)

    if best_content is None:
        if photo_record is not None:
            photo_record["download"] = {"status": download_status, "url_variants_tried": len(tried)}
        status_summary = ', '.join(f'HTTP {code}: {count}' for code, count in sorted(http_status_counts.items())) or 'keine HTTP-Antwort'
        dlog(f"Bild {filename}: kein gültiger Bild-Download; {len(tried)} Varianten geprüft; {status_summary}; Transportfehler={transport_error_count}.")
        return False

    if convert_jpg and Image is not None:
        try:
            with Image.open(BytesIO(best_content)) as source_image:
                source_image.load()
                if source_image.mode in ("RGBA", "P", "LA"):
                    background = Image.new("RGB", source_image.size, (255, 255, 255))
                    if source_image.mode == "RGBA":
                        background.paste(source_image, mask=source_image.split()[3])
                    else:
                        background.paste(source_image.convert("RGBA"))
                    image = background
                else:
                    image = source_image.convert("RGB")

                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=95)
                best_content = buffer.getvalue()
            if not best_content.startswith(b"\xff\xd8"):
                raise ValueError("Image conversion did not produce JPEG data.")
        except Exception as exc:
            report_error(f"Could not convert downloaded image '{filename}' to JPG: {exc}")
            if photo_record is not None:
                photo_record["download"] = {"status": "jpg_conversion_failed", **best_response_info}
            return False

    try:
        current_score = (-1, -1, -1, -1)
        if os.path.exists(filepath):
            try:
                with open(filepath, "rb") as current_file:
                    current_content = current_file.read()
                if current_content == best_content:
                    if photo_record is not None:
                        record_file(best_content, "existing_file_kept")
                    remove_converted_source()
                    width, height = _get_image_dimensions(best_content)
                    status_summary = ', '.join(f'HTTP {code}: {count}' for code, count in sorted(http_status_counts.items())) or 'keine HTTP-Antwort'
                    variant_summary = ', '.join(f'{name}: {count}' for name, count in sorted(accepted_variant_counts.items())) or 'keine gültigen Varianten'
                    dlog(f"Bild {filename}: {len(tried)} URL-Varianten geprüft; beste gefundene Auflösung {width}x{height}; vorhandene Datei beibehalten.")
                    dlog(f"Bildvarianten-Diagnose {filename}: {variant_summary}; {status_summary}; Transportfehler={transport_error_count}.")
                    if max(width, height) < 1400:
                        if accepted_variant_counts.get('tc', 0):
                            dlog(f"Hinweis {filename}: Selbst die erreichbare /tc/-Variante ist nur {width}x{height}; möglicherweise liegt keine größere Originaldatei mehr vor.")
                        else:
                            dlog(f"Hinweis {filename}: Keine erreichbare /tc/-Originalvariante gefunden; größte geprüfte Datei nur {width}x{height}.")
                    return True
                current_score = _image_quality_score(current_content)
            except OSError:
                pass

        if current_score >= best_score and os.path.exists(filepath):
            if photo_record is not None:
                record_file(best_content, "existing_better_file_kept")
            remove_converted_source()
            current_width, current_height = _get_image_dimensions(current_content)
            dlog(f"Bild {filename}: vorhandene Datei mit {current_width}x{current_height} beibehalten; gefundene Varianten bringen keine bessere Qualität.")
            if max(current_width, current_height) < 1400:
                dlog(f"Hinweis {filename}: Keine erreichbare größere Originalvariante nachweisbar; maximale gefundene Auflösung {current_width}x{current_height}.")
            return True

        temp_path = filepath + ".tmp"
        with open(temp_path, "wb") as image_file:
            image_file.write(best_content)
        os.replace(temp_path, filepath)
        remove_converted_source()
        if item_datetime:
            set_file_creation_time(filepath, item_datetime)
        download_status = "downloaded_or_upgraded"
        if photo_record is not None:
            record_file(best_content, download_status)
        best_width, best_height = _get_image_dimensions(best_content)
        best_source = best_response_info.get("download_source_url") or "bereits vorhandene Datei"
        status_summary = ', '.join(f'HTTP {code}: {count}' for code, count in sorted(http_status_counts.items())) or 'keine HTTP-Antwort'
        variant_summary = ', '.join(f'{name}: {count}' for name, count in sorted(accepted_variant_counts.items())) or 'keine gültigen Varianten'
        dlog(f"Bild {filename}: {len(tried)} URL-Varianten geprüft; beste gefundene Auflösung {best_width}x{best_height}; Quelle={best_source}")
        dlog(f"Bildvarianten-Diagnose {filename}: {variant_summary}; {status_summary}; Transportfehler={transport_error_count}.")
        if max(best_width, best_height) < 1400:
            if accepted_variant_counts.get('tc', 0):
                dlog(f"Hinweis {filename}: Auch die erreichbare /tc/-Variante bleibt bei {best_width}x{best_height}; möglicherweise liegt keine größere Originaldatei mehr vor.")
            else:
                dlog(f"Hinweis {filename}: Keine erreichbare /tc/-Originalvariante gefunden. Die Seite/der CDN lieferte nur kleine Varianten; ohne eine gültige Original-URL lässt sich die Auflösung nicht verlustfrei erhöhen.")
        return True
    except OSError:
        try:
            if os.path.exists(filepath + ".tmp"):
                os.remove(filepath + ".tmp")
        except OSError:
            pass
        if photo_record is not None:
            photo_record["download"] = {"status": "write_failed", **best_response_info}
        return False


# ================= SESSION & AUTH MANAGER =================
def open_interactive_session_window(domain: str = "vinted.de", status_callback: Optional[Callable[[str], None]] = None) -> None:
    """Öffnet ein sichtbares Browserfenster zum manuellen Akzeptieren von Cookies / Einloggen."""
    def log(msg: str):
        if status_callback:
            status_callback(msg)
        else:
            print(msg)

    log("[*] Öffne sichtbaren Browser für Login & Cookie-Akzeptanz...")
    with sync_playwright() as p:
        browser = None
        for channel in ["chrome", "msedge"]:
            try:
                browser = p.chromium.launch(headless=False, channel=channel)
                log(f"[*] Nutze installierten Browser: {channel.upper()}")
                break
            except Exception:
                continue

        if not browser:
            browser = p.chromium.launch(headless=False)

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.goto(f"https://www.{domain}")

        log("[!] Bitte akzeptiere die Cookies oder logge dich im geöffneten Fenster ein.")
        log("[!] Schließe das Browserfenster manuell, wenn du fertig bist...")

        try:
            while len(browser.contexts) > 0 and len(context.pages) > 0:
                time.sleep(1)
        except Exception:
            pass

        try:
            save_session_state(context)
            log(f"[✔] Session-Daten erfolgreich in '{SESSION_FILE}' gespeichert!")
        except Exception as e:
            log(f"[!] Fehler beim Speichern der Session: {e}")


def get_vinted_auth_token(page) -> Optional[str]:
    """Extrahiert den aktuellen Bearer/OAuth-Token aus dem Session- oder LocalStorage."""
    try:
        token = page.evaluate("""() => {
            try {
                for (let i = 0; i < sessionStorage.length; i++) {
                    const key = sessionStorage.key(i);
                    if (key && key.includes('token')) return sessionStorage.getItem(key);
                }
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i);
                    if (key && key.includes('token')) return localStorage.getItem(key);
                }
            } catch(e) {}
            return null;
        }""")
        if token:
            return token.replace('"', '').strip()

        cookies = page.context.cookies()
        for c in cookies:
            if 'access_token' in c['name'] or 'oauth' in c['name']:
                return c['value']
    except Exception:
        pass
    return None


# ================= NATIVE PLAYWRIGHT FETCHING & MULTI-DOMAIN PROBING =================
def _normalise_domain(value: str) -> str:
    value = (value or '').strip().lower()
    value = re.sub(r'^https?://', '', value)
    value = value.split('/', 1)[0]
    return value[4:] if value.startswith('www.') else value


def _domain_list(preferred_domain: str = 'vinted.de') -> List[str]:
    preferred = _normalise_domain(preferred_domain) or 'vinted.de'
    return [preferred] + [d for d in VINTED_DOMAINS if d != preferred]


def _request_headers(auth_token: Optional[str] = None, referer_domain: str = 'vinted.de') -> Dict[str, str]:
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Referer': f'https://www.{_normalise_domain(referer_domain)}/',
    }
    if auth_token:
        headers['Authorization'] = auth_token if auth_token.startswith('Bearer ') else f'Bearer {auth_token}'
    return headers


def _extract_embedded_json_objects(text: str) -> List[Any]:
    """Extract regular JSON and JSON hidden inside Next.js/React Flight script strings."""
    results: List[Any] = []
    if not text:
        return results

    decoder = json.JSONDecoder()
    seen_payload_ids = set()
    seen_strings = set()
    key_pattern = re.compile(
        r'(?:\\?"|\")?(?:itemDto|item_dto|itemDetails|item_details|item|product|listing|user)'
        r'(?:\\?"|\")?\s*:\s*', re.I
    )

    def add_payload(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, (dict, list)):
            marker = id(value)
            if marker not in seen_payload_ids:
                seen_payload_ids.add(marker)
                results.append(value)
            values = value.values() if isinstance(value, dict) else value
            for child in values:
                if isinstance(child, (dict, list)):
                    add_payload(child, depth + 1)
                elif isinstance(child, str) and len(child) < 4_000_000:
                    scan_blob(child, depth + 1)
        elif isinstance(value, str):
            scan_blob(value, depth + 1)

    def scan_blob(blob: str, depth: int = 0) -> None:
        if depth > 8 or not blob or len(blob) > 8_000_000:
            return
        marker = (hash(blob), len(blob))
        if marker in seen_strings:
            return
        seen_strings.add(marker)

        stripped = blob.strip()
        if stripped[:1] in ('{', '['):
            try:
                parsed = json.loads(stripped)
                add_payload(parsed, depth + 1)
            except Exception:
                pass

        # Vinted sometimes double-escapes JSON embedded inside a React Flight
        # string. Try a few bounded unescaping passes so `\\"itemDto\\":{...}`
        # becomes parseable JSON rather than being silently skipped.
        decoded_variant = blob
        for _unescape_pass in range(3):
            next_variant = (decoded_variant
                            .replace(r'\\"', '"')
                            .replace(r'\"', '"')
                            .replace(r'\/', '/')
                            .replace(r'\u0022', '"')
                            .replace(r'\u002F', '/')
                            .replace(r'\n', '\n'))
            if next_variant == decoded_variant:
                break
            decoded_variant = next_variant
            scan_blob(decoded_variant, depth + 1)

        # In RSC/Flight payloads, itemDto may occur inside a decoded string that
        # also contains framing text. Extract the JSON value after the key.
        for match in key_pattern.finditer(blob):
            pos = match.end()
            while pos < len(blob) and blob[pos].isspace():
                pos += 1
            if pos >= len(blob):
                continue
            try:
                if blob[pos] in '{[':
                    obj, _ = decoder.raw_decode(blob[pos:])
                    if isinstance(obj, (dict, list)):
                        add_payload(obj, depth + 1)
                elif blob[pos] == '"':
                    embedded, _ = decoder.raw_decode(blob[pos:])
                    if isinstance(embedded, str) and embedded != blob:
                        scan_blob(embedded, depth + 1)
            except Exception:
                continue

    # Normal application/json and schema.org JSON-LD blocks.
    script_matches = list(re.finditer(r'<script\b([^>]*)>(.*?)</script\s*>', text, re.I | re.S))
    for match in script_matches:
        attrs, raw = match.group(1) or '', html_unescape(match.group(2) or '').strip()
        if not raw:
            continue
        if re.search(r'type\s*=\s*["\']application/(?:ld\+)?json["\']', attrs, re.I):
            try:
                add_payload(json.loads(raw))
            except Exception:
                scan_blob(raw)

        # Next.js app router serializes RSC data in self.__next_f.push([..., "..."]).
        # Parse those JS string arrays as JSON first so escaped quotes are decoded.
        found_flight = False
        for flight_match in re.finditer(
            r'self\.__next_f\.push\(\s*(\[.*?\])\s*\)\s*;?', raw, re.S
        ):
            try:
                flight_payload = json.loads(flight_match.group(1))
                add_payload(flight_payload)
                found_flight = True
            except Exception:
                scan_blob(flight_match.group(1))
        # Other Next.js/React payloads and non-standard hydration scripts.
        scan_blob(raw)

    # Some pages place JSON or URLs outside script tags; inspect the full document too.
    scan_blob(html_unescape(text))
    return results


def _enrich_photo_urls_from_html(item: Dict[str, Any], html: str) -> Dict[str, Any]:
    """Merge image-CDN variants found in the rendered document into matching photos.

    Vinted often repeats the same photo in serialized item data and in gallery/srcset
    markup. The serialized object may expose only an f800 URL even when another
    signed /tc/ or larger variant appears elsewhere in the document. Match by the
    stable CDN asset token; never attach an unrelated recommendation image.
    """
    photos = item.get('photos') if isinstance(item, dict) else None
    if not isinstance(photos, list) or not html:
        return item

    normalized = html_unescape(html)
    for old, new in ((r'\\/', '/'), (r'\/', '/'), (r'\u002F', '/'), (r'\u002f', '/'), (r'\u0026', '&'), ('&amp;', '&')):
        normalized = normalized.replace(old, new)

    url_pattern = re.compile(
        r'https?:/{2}images\d*\.vinted\.net/(?:t|tc)/[^\s"\'<>\\]+', re.I
    )
    discovered: Dict[str, List[str]] = {}
    for match in url_pattern.finditer(normalized):
        candidate = match.group(0).rstrip('),;]')
        # Remove common JSON escape leftovers and preserve the signed query exactly.
        candidate = candidate.replace('\\/', '/').replace('&amp;', '&')
        try:
            parts = urlsplit(candidate)
            if parts.scheme not in ('http', 'https') or not parts.netloc:
                continue
            asset_match = re.match(r'^/(?:t|tc)/([^/]+)/', parts.path, re.I)
            if not asset_match:
                continue
            asset = asset_match.group(1)
            urls = discovered.setdefault(asset, [])
            if candidate not in urls:
                urls.append(candidate)
        except Exception:
            continue

    for photo in photos:
        if not isinstance(photo, dict):
            continue
        current_urls = get_image_urls(photo)
        assets = set()
        for url in current_urls:
            m = re.search(r'/(?:t|tc)/([^/?#]+)/', url, re.I)
            if m:
                assets.add(m.group(1))
        if not assets:
            continue
        merged = list(current_urls)
        for asset in assets:
            for candidate in discovered.get(asset, []):
                if candidate not in merged:
                    merged.append(candidate)
        if not merged:
            continue
        full_urls = [u for u in merged if re.search(r'/tc/[^/]+/', urlsplit(u).path, re.I)]
        if full_urls:
            # This is a genuine URL found in page source, not a guessed URL rewrite.
            photo['full_size_url'] = full_urls[0]
        # Retain all source variants so download_single_image can compare their actual
        # pixel dimensions instead of trusting a URL's name/path.
        photo['url_options'] = list(dict.fromkeys(full_urls + merged))
    return item


def _extract_item_from_html(html: str, item_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Recover the richest matching item object from duplicate HTML hydration payloads."""
    if not html:
        return None

    candidates: List[Dict[str, Any]] = []
    seen_ids = set()
    for payload in _extract_embedded_json_objects(html):
        item = _find_item_payload(payload, item_id)
        if isinstance(item, dict) and item.get('photos') and (item_id is None or str(item.get('id')) == str(item_id)):
            if id(item) not in seen_ids:
                candidates.append(item)
                seen_ids.add(id(item))
        for candidate in _find_all_item_payloads(payload):
            if not candidate.get('photos') or (item_id is not None and str(candidate.get('id')) != str(item_id)):
                continue
            if id(candidate) not in seen_ids:
                candidates.append(candidate)
                seen_ids.add(id(candidate))

    if not candidates:
        return None

    def richness(item: Dict[str, Any]) -> Tuple[int, int, int, int]:
        photos = item.get('photos') if isinstance(item.get('photos'), list) else []
        full_size = 0
        url_count = 0
        high_res_ids = 0
        for photo in photos:
            if not isinstance(photo, dict):
                continue
            if any(photo.get(k) for k in ('full_size_url', 'fullSizeUrl', 'original_url', 'originalUrl')):
                full_size += 1
            urls = get_image_urls(photo)
            url_count += len(urls)
            if isinstance(photo.get('high_resolution'), dict) and photo['high_resolution'].get('id'):
                high_res_ids += 1
            if any('/tc/' in url for url in urls):
                full_size += 1
        item_fields = sum(1 for key in ('title', 'description', 'user', 'url', 'created_at', 'price', 'brand', 'status') if item.get(key))
        return (len(photos), full_size, url_count + high_res_ids, item_fields)

    candidates.sort(key=richness, reverse=True)
    result = dict(candidates[0])

    def merge_nested(base: Any, other: Any, depth: int = 0) -> Any:
        if depth > 8:
            return base if base not in (None, '', [], {}) else other
        if isinstance(base, dict) and isinstance(other, dict):
            merged = dict(base)
            for key, value in other.items():
                if key not in merged or merged[key] in (None, '', [], {}):
                    merged[key] = value
                elif isinstance(merged[key], dict) and isinstance(value, dict):
                    merged[key] = merge_nested(merged[key], value, depth + 1)
                elif isinstance(merged[key], list) and isinstance(value, list) and key in {'urls', 'url_options', 'image_urls', 'urlOptions', 'imageUrls', 'thumbnails'}:
                    combined = list(merged[key])
                    for entry in value:
                        if entry not in combined:
                            combined.append(entry)
                    merged[key] = combined
            return merged
        return base if base not in (None, '', [], {}) else other

    for candidate in candidates[1:]:
        for key, value in candidate.items():
            if key == 'photos' and isinstance(result.get('photos'), list) and isinstance(value, list):
                base_photos = result['photos']
                if len(base_photos) == len(value):
                    result['photos'] = [
                        merge_nested(base_photo, other_photo)
                        if isinstance(base_photo, dict) and isinstance(other_photo, dict)
                        else (base_photo if base_photo not in (None, '', [], {}) else other_photo)
                        for base_photo, other_photo in zip(base_photos, value)
                    ]
            elif key in result:
                result[key] = merge_nested(result[key], value)
            else:
                result[key] = value
    return result


def _extract_seller_link_from_html(html: str) -> Optional[Dict[str, Any]]:
    """Find a public Vinted member link, including HTML/Flight-escaped hrefs."""
    if not html:
        return None

    normalized = html_unescape(html)
    # HTML may contain a URL inside a JSON/React Flight string with several
    # escaping layers. Decode only common URL/quote escapes, boundedly.
    variants = [normalized]
    current = normalized
    for _ in range(3):
        nxt = (current.replace(r'\\/', '/')
                      .replace(r'\/', '/')
                      .replace(r'\u002F', '/')
                      .replace(r'\u002f', '/')
                      .replace(r'\u0022', '"')
                      .replace(r'\u002f', '/')
                      .replace(r'\"', '"')
                      .replace('&amp;', '&'))
        if nxt == current:
            break
        variants.append(nxt)
        current = nxt

    # Prefer explicit anchor hrefs over free-text occurrences, since pages may
    # contain links to members offering similar items as well.
    candidates: List[Tuple[int, Dict[str, Any]]] = []
    seen = set()
    anchor_re = re.compile(r'<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a\s*>', re.I | re.S)
    href_re = re.compile(r'\bhref\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.I | re.S)

    for source_index, source in enumerate(variants):
        for match in anchor_re.finditer(source):
            attrs = match.group('attrs') or ''
            href_match = href_re.search(attrs)
            if not href_match:
                continue
            href = next((v for v in href_match.groups() if v is not None), '').replace('\\/', '/')
            member = re.search(r'(?:https?://(?:www\.)?vinted\.[a-z.]+)?/member/(\d+)(?:-([^/?#"&\s<>\\]+))?', href, re.I)
            if not member:
                continue
            member_id = member.group(1)
            login = unquote(member.group(2)) if member.group(2) else None
            anchor_text = re.sub(r'<[^>]+>', ' ', match.group('body') or '')
            anchor_text = re.sub(r'\s+', ' ', html_unescape(anchor_text)).strip()
            if login and login.lower() in {'member', 'profile'}:
                login = None
            if not login and anchor_text:
                if not re.search(r'\b(reviews?|followers?|following|last seen|member blocked)\b', anchor_text, re.I):
                    login = anchor_text.split()[0]
            result = {'id': member_id, 'login': login or anchor_text or None}
            key = (member_id, result['login'])
            if key in seen:
                continue
            seen.add(key)
            score = 100 - source_index * 5
            if login:
                score += 10
            # Seller anchors often include a visible review count next to the username.
            if re.search(r'\b(?:reviews?|member blocked|last seen)\b', anchor_text, re.I):
                score += 3
            candidates.append((score, result))
        if candidates:
            # Use the earliest normalized representation that exposes actual anchors.
            break

    if candidates:
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]

    # Some current pages keep profile hrefs in hydration data or use anchor markup
    # that is not a conventional <a href=...>...</a>. Accept a raw member path as a
    # last resort; this is still tied to the item page response, not an ID guess.
    for source in reversed(variants):
        match = re.search(r'(?:https?://(?:www\.)?vinted\.[a-z.]+)?/member/(\d+)(?:-([^/?#"&\s<>\\]+))?', source, re.I)
        if match:
            login = unquote(match.group(2)) if match.group(2) else None
            return {'id': match.group(1), 'login': login}
    return None


def _extract_user_from_html(html: str, item_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Recover seller identity from HTML anchors or embedded item/user data."""
    item = _extract_item_from_html(html, item_id)
    if item and isinstance(item.get('user'), dict) and item['user'].get('id'):
        return item['user']

    seller_link = _extract_seller_link_from_html(html)
    if seller_link:
        return seller_link

    for payload in _extract_embedded_json_objects(html):
        stack = [payload]
        seen = set()
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                marker = id(value)
                if marker in seen:
                    continue
                seen.add(marker)
                if value.get('id') is not None and (value.get('login') or value.get('username')):
                    return value
                stack.extend(v for v in value.values() if isinstance(v, (dict, list)))
            elif isinstance(value, list):
                stack.extend(value)
    return None


def fetch_item_with_domain_fallback(
    context,
    item_id: str,
    preferred_domain: str = 'vinted.de',
    auth_token: Optional[str] = None,
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None
) -> Optional[Dict[str, Any]]:
    """Fetch an item across all configured Vinted domains using the browser session."""
    def dlog(msg: str):
        if debug_mode:
            if status_callback:
                status_callback(f'[DEBUG] {msg}')
            else:
                print(f'[DEBUG] {msg}')

    for domain in _domain_list(preferred_domain):
        dlog(f"Versuche Artikel {item_id} auf Domain 'https://www.{domain}' abzufragen...")
        try:
            api_url = f'https://www.{domain}/api/v2/items/{item_id}'
            response = context.request.get(api_url, headers=_request_headers(auth_token, domain), timeout=15000)
            dlog(f'Antwort von {domain}: HTTP Status {response.status}')
            if response.status == 200:
                data = response.json()
                item = _find_item_payload(data, item_id) or (data.get('item') if isinstance(data, dict) else None)
                if item and item.get('photos'):
                    if status_callback:
                        status_callback(f"[+] Artikel {item_id} erfolgreich auf '{domain}' gefunden!")
                    return item
        except Exception as e:
            dlog(f'Fetch-Fehler auf {domain}: {e}')
    return None


def fetch_user_items_all_domains(
    context,
    user_id: Any,
    preferred_domain: str = 'vinted.de',
    auth_token: Optional[str] = None,
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
    max_pages: int = 200
) -> Dict[str, Dict[str, Any]]:
    """Enumerate a seller's currently exposed listings on every configured Vinted market."""
    found: Dict[str, Dict[str, Any]] = {}
    if not user_id:
        return found

    def dlog(msg: str):
        if debug_mode:
            if status_callback:
                status_callback(f'[DEBUG] {msg}')
            else:
                print(f'[DEBUG] {msg}')

    for domain in _domain_list(preferred_domain):
        page_no = 1
        empty_pages = 0
        dlog(f'User-Artikel Scan auf {domain} für Seller-ID {user_id}...')
        while page_no <= max_pages:
            try:
                api_url = f'https://www.{domain}/api/v2/wardrobe/{user_id}/items'
                params = {'page': page_no, 'per_page': 20, 'order': 'newest_first'}
                response = context.request.get(api_url, headers=_request_headers(auth_token, domain), params=params, timeout=15000)
                if response.status != 200:
                    dlog(f'{domain}: User-Items Seite {page_no} -> HTTP {response.status}')
                    break
                data = response.json()
                items = data.get('items', []) if isinstance(data, dict) else []
                if not isinstance(items, list) or not items:
                    break
                added = 0
                for item in items:
                    if isinstance(item, dict) and item.get('id'):
                        key = str(item['id'])
                        if key not in found:
                            found[key] = item
                            added += 1
                dlog(f'{domain}: Seite {page_no}: {len(items)} Artikel, {added} neu.')
                pagination = data.get('pagination', {}) if isinstance(data, dict) else {}
                total_pages = pagination.get('total_pages') if isinstance(pagination, dict) else None
                try:
                    total_pages = int(total_pages) if total_pages is not None else None
                except (TypeError, ValueError):
                    total_pages = None
                if total_pages and page_no >= total_pages:
                    break
                if total_pages is None and len(items) < params['per_page']:
                    break
                page_no += 1
                time.sleep(0.15)
            except Exception as e:
                dlog(f'{domain}: User-Items Fehler Seite {page_no}: {e}')
                break
    if status_callback:
        status_callback(f'[+] Seller-Scan abgeschlossen: {len(found)} aktuell erreichbare Artikel gefunden.')
    return found


def fetch_exact_item_ids_native(
    context,
    item_ids: List[int],
    domain: str,
    target_user_id: Optional[Any] = None,
    auth_token: Optional[str] = None,
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None
) -> Dict[str, Dict[str, Any]]:
    """Fetch only IDs actually returned by a market's public search index."""
    result: Dict[str, Dict[str, Any]] = {}
    unique_ids = sorted({int(value) for value in item_ids if str(value).isdigit()})
    for index, item_id in enumerate(unique_ids, 1):
        if status_callback and index % 10 == 0:
            status_callback(f'[*] Indexed items ({domain}): {index}/{len(unique_ids)}...')
        try:
            url = f'https://www.{_normalise_domain(domain)}/items/{item_id}'
            response = context.request.get(
                url,
                headers={**_request_headers(auth_token, domain), 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'},
                timeout=12000
            )
            if response.status != 200:
                continue
            item = _public_item_from_html(response.text(), str(item_id), url)
            if not item or not item.get('photos'):
                continue
            user = item.get('user') if isinstance(item.get('user'), dict) else None
            if target_user_id and (not user or str(user.get('id')) != str(target_user_id)):
                continue
            result[str(item_id)] = item
        except Exception as e:
            if debug_mode:
                message = f'[DEBUG] Indexed item {item_id} on {domain} failed: {e}'
                status_callback(message) if status_callback else print(message)
    return result


def fetch_user_items_from_public_profile_html(
    context,
    user_id: Any,
    user_login: Optional[str] = None,
    preferred_domain: str = 'vinted.de',
    auth_token: Optional[str] = None,
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
    max_links_per_domain: int = 80,
) -> Dict[str, Dict[str, Any]]:
    """Recover seller-owned listings linked or embedded in public member pages.

    Unlike nearby-ID probing, this follows item links discovered on the seller's
    public profile. Each candidate item is re-fetched and must match user_id before
    it is accepted, so unrelated recommendation cards are not downloaded.
    """
    found: Dict[str, Dict[str, Any]] = {}
    if not user_id:
        return found

    def dlog(message: str) -> None:
        if debug_mode:
            if status_callback:
                status_callback(f'[DEBUG] {message}')
            else:
                print(f'[DEBUG] {message}')

    login_slug = quote(str(user_login).strip(), safe='._-') if user_login else ''
    for domain in _domain_list(preferred_domain):
        profile_urls: List[str] = []
        if login_slug:
            profile_urls.append(f'https://www.{domain}/member/{user_id}-{login_slug}')
        profile_urls.append(f'https://www.{domain}/member/{user_id}')
        seen_urls = set()
        domain_profile_loaded = False

        for profile_url in profile_urls:
            if profile_url in seen_urls:
                continue
            seen_urls.add(profile_url)
            try:
                headers = {
                    **_request_headers(auth_token, domain),
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Upgrade-Insecure-Requests': '1',
                }
                response = context.request.get(profile_url, headers=headers, timeout=18000)
                status = getattr(response, 'status', -1)
                if status != 200:
                    dlog(f'Public-Profil {domain}: HTTP {status} ({profile_url})')
                    continue

                raw_html = response.text()
                normalized = html_unescape(raw_html)
                for _ in range(3):
                    decoded = (normalized.replace(r'\/', '/')
                                         .replace(r'\u002F', '/')
                                         .replace(r'\u002f', '/')
                                         .replace(r'\u0022', '"')
                                         .replace(r'\"', '"'))
                    if decoded == normalized:
                        break
                    normalized = decoded

                # Some profiles include structured item cards directly in the hydration payload.
                for payload in _extract_embedded_json_objects(normalized):
                    for item in _find_all_item_payloads(payload):
                        owner = item.get('user') if isinstance(item.get('user'), dict) else None
                        if owner and str(owner.get('id')) == str(user_id) and item.get('photos'):
                            found[str(item.get('id'))] = item

                # Follow item URLs from the profile. The page may also contain recommendations,
                # so every candidate is owner-checked by fetch_exact_item_ids_native below.
                patterns = (
                    r'(?:https?://(?:www\.)?vinted\.[a-z.]+)?/items/(\d+)(?:-[^/?#"\'<>\\\s]*)?',
                    r'/(?:items)/(\d+)(?=[/?#"\'<>\\\s]|$)',
                )
                candidate_ids = set()
                for pattern in patterns:
                    for match in re.finditer(pattern, normalized, re.I):
                        candidate_ids.add(int(match.group(1)))
                candidate_ids = set(sorted(candidate_ids)[:max_links_per_domain])
                dlog(f'Public-Profil {domain}: HTTP 200, {len(raw_html)} Zeichen, {len(candidate_ids)} verlinkte Artikel-IDs.')

                if candidate_ids:
                    recovered = fetch_exact_item_ids_native(
                        context=context,
                        item_ids=sorted(candidate_ids),
                        domain=domain,
                        target_user_id=user_id,
                        auth_token=auth_token,
                        debug_mode=debug_mode,
                        status_callback=status_callback,
                    )
                    _merge_items(found, recovered)
                    dlog(f'Public-Profil {domain}: {len(recovered)} Artikel sicher dem Verkäufer zugeordnet.')

                domain_profile_loaded = True
                break
            except Exception as exc:
                dlog(f'Public-Profil {domain} fehlgeschlagen ({profile_url}): {exc}')
        if domain_profile_loaded:
            break

    if status_callback:
        status_callback(f'[+] Öffentlicher Profil-Scan abgeschlossen: {len(found)} zusätzliche Verkäufer-Artikel gefunden.')
    return found


def probe_vinted_id_range_native(
    context,
    seed_ids: List[int],
    target_user_id: Optional[Any] = None,
    buffer_range: int = 150,
    preferred_domain: str = 'vinted.de',
    auth_token: Optional[str] = None,
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None
) -> Dict[str, Dict[str, Any]]:
    """Probe public HTML item pages across markets; the legacy item JSON endpoint is unreliable."""
    if not seed_ids:
        return {}
    if not target_user_id:
        if status_callback:
            status_callback('[!] ID-Probing übersprungen: Ohne Verkäufer-ID lassen sich Treffer nicht zuverlässig zuordnen.')
        return {}

    seeds = sorted({int(x) for x in seed_ids if str(x).isdigit()})
    ranges = []
    for seed in seeds:
        lo, hi = max(1, seed - buffer_range), seed + buffer_range
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
        else:
            ranges.append((lo, hi))
    total_per_domain = sum(hi - lo + 1 for lo, hi in ranges)
    # Probe consecutive IDs only on the source market. Seller inventories and indexed IDs are
    # queried separately across every configured market, avoiding thousands of duplicate page downloads.
    domains = [_normalise_domain(preferred_domain) or 'vinted.de']

    def dlog(msg: str):
        if debug_mode:
            if status_callback:
                status_callback(f'[DEBUG] {msg}')
            else:
                print(f'[DEBUG] {msg}')

    found: Dict[str, Dict[str, Any]] = {}
    checked = 0
    for domain in domains:
        if status_callback:
            status_callback(f'[*] HTML-ID-Probing aktiv auf {domain}: {total_per_domain} Seiten in {len(ranges)} Bereich(en)...')
        for lo, hi in ranges:
            for current_id in range(lo, hi + 1):
                checked += 1
                if checked % 50 == 0 and status_callback:
                    status_callback(f'HTML-Probing ({domain}): {checked}/{total_per_domain * len(domains)}...')
                try:
                    item_url = f'https://www.{domain}/items/{current_id}'
                    response = context.request.get(
                        item_url,
                        headers={**_request_headers(auth_token, domain), 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'},
                        timeout=12000
                    )
                    if response.status == 200:
                        html = response.text()
                        item = _public_item_from_html(html, str(current_id), item_url)
                        if item and item.get('id'):
                            seller = item.get('user') if isinstance(item.get('user'), dict) else None
                            if not seller or not seller.get('id'):
                                seller = _extract_seller_link_from_html(html)
                                if seller:
                                    item['user'] = seller
                            seller_id = str(seller.get('id')) if isinstance(seller, dict) and seller.get('id') else None
                            if seller_id == str(target_user_id) and item.get('photos'):
                                found[str(item.get('id', current_id))] = item
                                dlog(f'Historischer Treffer {current_id} auf {domain} für Seller {target_user_id}.')
                    # Public pages are larger than API responses; keep the scan polite.
                    time.sleep(0.04)
                except Exception as e:
                    dlog(f'HTML-Probe {current_id} auf {domain} fehlgeschlagen: {e}')
                    continue
    if status_callback:
        status_callback(f'[+] HTML-Probing beendet: {len(found)} Artikel des Verkäufers gefunden.')
    return found


def _extract_vinted_item_links_from_search_html(html: str, scoped_domain: Optional[str] = None) -> set:
    """Extract ``(domain, item_id)`` pairs from raw, escaped, or redirected SERP URLs."""
    found = set()
    if not html:
        return found
    first = html_unescape(str(html)).replace(r'\/', '/').replace('&amp;', '&')
    variants = [first]
    current = first
    for _ in range(4):
        decoded = unquote(current).replace(r'\/', '/').replace(r'\u002F', '/').replace(r'\u002f', '/')
        if decoded == current:
            break
        variants.append(decoded)
        current = decoded

    desired = _normalise_domain(scoped_domain) if scoped_domain else None
    for blob in variants:
        for match in re.finditer(
            r'https?://(?:www\.)?(vinted\.[a-z.]+)/items/(\d+)(?:-[^/?#"&\\\s<>]*)?',
            blob, re.I
        ):
            domain = _normalise_domain(match.group(1))
            if desired and domain != desired:
                continue
            found.add((domain, int(match.group(2))))
        # Only interpret relative item links when the query itself was scoped to a
        # single Vinted market. Relative URLs in a global search page are ambiguous.
        if desired:
            for match in re.finditer(r'(?<![\w.-])/items/(\d+)(?=[/?#"&\s<>-]|$)', blob, re.I):
                found.add((desired, int(match.group(1))))
    return found


def _extract_vinted_item_ids_from_search_html(html: str, scoped_domain: Optional[str] = None) -> set:
    """Convenience wrapper when the caller already knows the target market."""
    return {item_id for _, item_id in _extract_vinted_item_links_from_search_html(html, scoped_domain)}

def get_google_indexed_items_by_domain(
    query_term: str,
    preferred_domain: str = 'vinted.de',
    extra_terms: Optional[List[str]] = None,
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, set]:
    """Discover historical item URLs globally and by market using public SERPs.

    Global queries matter because the same seller may have indexed listings on a
    different Vinted regional hostname than the supplied direct URL. Each returned
    item is still only a candidate; callers must owner-verify the item page.
    """
    domains = _domain_list(preferred_domain)
    results: Dict[str, set] = {market: set() for market in domains}
    query_term = str(query_term or '').strip().strip('"')
    if not query_term:
        return results

    def dlog(message: str) -> None:
        if debug_mode:
            rendered = f'[DEBUG] {message}'
            if status_callback:
                status_callback(rendered)
            else:
                print(rendered)

    extras = []
    for term in extra_terms or []:
        term = re.sub(r'\s+', ' ', str(term or '')).strip().strip('"')
        if term and term.lower() != query_term.lower() and term not in extras:
            extras.append(term)

    endpoints = (
        ('Google', 'https://www.google.com/search?hl=en&num=100&q={query}'),
        ('Bing', 'https://www.bing.com/search?q={query}&count=50'),
        ('DuckDuckGo', 'https://html.duckduckgo.com/html/?q={query}'),
    )
    jobs = []
    # Search all regional domains in one query first. This is the crucial fallback
    # missing from per-domain site: queries when a sold listing is indexed elsewhere.
    global_queries = [f'"{query_term}" Vinted', f'"{query_term}" items Vinted']
    for term in extras:
        global_queries.append(f'"{query_term}" "{term}" Vinted')
        global_queries.append(f'"{term}" Vinted')
    for query in list(dict.fromkeys(global_queries)):
        for provider, template in endpoints:
            jobs.append((provider, template, query, None))

    # Still run market-scoped searches: they may produce results that global queries miss.
    google_provider, google_template = endpoints[0]
    for market in domains:
        scoped_queries = [f'site:{market}/items/ "{query_term}"']
        scoped_queries.extend(f'site:{market}/items/ "{term}"' for term in extras)
        for query in list(dict.fromkeys(scoped_queries)):
            jobs.append((google_provider, google_template, query, market))

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,fr;q=0.7,de;q=0.6',
    }

    def fetch_links(job):
        provider, template, query, scope = job
        links = set()
        try:
            response = requests.get(
                template.format(query=quote(query, safe='')),
                headers=headers,
                timeout=(4, 9),
                allow_redirects=True,
            )
            if response.status_code == 200:
                links = _extract_vinted_item_links_from_search_html(response.text, scope)
            # Log only provider/query result counts, so empty/captcha responses are distinguishable.
            dlog(f'Search {provider} ({scope or "all markets"}) for {query!r}: HTTP {response.status_code}, HTML={len(response.text)} chars, item URLs={len(links)}.')
        except Exception as exc:
            dlog(f'Search {provider} ({scope or "all markets"}) failed for {query!r}: {type(exc).__name__}: {exc}')
        return links

    with ThreadPoolExecutor(max_workers=6) as pool:
        for links in pool.map(fetch_links, jobs):
            for market, item_id in links:
                if market in results:
                    results[market].add(int(item_id))

    dlog('Search engines combined: ' + ', '.join(f'{market}={len(ids)}' for market, ids in results.items()))
    return results


def get_google_indexed_items(
    query_term: str,
    domain: str = 'vinted.de',
    extra_terms: Optional[List[str]] = None,
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
) -> set:
    """Compatibility wrapper returning IDs for a single domain."""
    results = get_google_indexed_items_by_domain(
        query_term=query_term,
        preferred_domain=domain,
        extra_terms=extra_terms,
        debug_mode=debug_mode,
        status_callback=status_callback,
    )
    return set(results.get(_normalise_domain(domain), set()))

def get_photo_filename_indexed_items(
    filenames: List[str],
    seller_login: Optional[str] = None,
    preferred_domain: str = 'vinted.de',
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, set]:
    """Find public item-page IDs that search engines associate with image filenames.

    A Vinted photo basename is not an item ID and usually is not reversible on its
    own. This best-effort fallback queries public search indexes for the exact
    basename and maps discovered item links back to their Vinted market domains.
    """
    domains = _domain_list(preferred_domain)
    results: Dict[str, set] = {domain: set() for domain in domains}
    cleaned_filenames = []
    for value in filenames or []:
        value = str(value or '').strip().strip('"\'')
        if PHOTO_FILENAME_PATTERN.fullmatch(value) and value not in cleaned_filenames:
            cleaned_filenames.append(value)
    if not cleaned_filenames:
        return results

    def dlog(message: str) -> None:
        if debug_mode:
            if status_callback:
                status_callback(f'[DEBUG] {message}')
            else:
                print(f'[DEBUG] {message}')

    endpoints = (
        'https://www.google.com/search?hl=en&num=100&q={query}',
        'https://www.bing.com/search?q={query}&count=50',
        'https://html.duckduckgo.com/html/?q={query}',
    )
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }
    jobs = []
    found_by_filename: Dict[str, Dict[str, set]] = {
        filename: {domain: set() for domain in domains} for filename in cleaned_filenames
    }
    for filename in cleaned_filenames:
        stem = filename.rsplit('.', 1)[0]
        possible_date = None
        if stem.isdigit() and len(stem) == 10:
            try:
                parsed_date = datetime.utcfromtimestamp(int(stem))
                if 2015 <= parsed_date.year <= 2035:
                    possible_date = parsed_date.strftime('%Y-%m-%d')
            except (OverflowError, OSError, ValueError):
                possible_date = None

        # Queries are deliberately exact: filenames can be search signals, but
        # they must never be mistaken for item IDs or treated as ownership proof.
        queries = [f'"{filename}" Vinted']
        if seller_login:
            queries.extend([f'"{filename}" "{seller_login}"', f'"{stem}" "{seller_login}" Vinted'])
            if possible_date:
                queries.append(f'"{seller_login}" "{possible_date}" Vinted')
        else:
            queries.append(f'"images1.vinted.net" "{filename}"')
        for query in dict.fromkeys(queries):
            for endpoint in endpoints:
                jobs.append((filename, query, endpoint, None))
        # Google-only site-scoped searches span all known regional domains; global
        # queries above use Google/Bing/DuckDuckGo and preserve the result hostname.
        google_endpoint = endpoints[0]
        for domain in domains:
            query = f'site:{domain}/items/ "{filename}"'
            jobs.append((filename, query, google_endpoint, domain))

    def run_job(job):
        filename, query, endpoint, scope_domain = job
        found: List[Tuple[str, int]] = []
        try:
            response = requests.get(
                endpoint.format(query=quote(query, safe='')),
                headers=headers,
                timeout=(4, 8),
                allow_redirects=True,
            )
            if response.status_code != 200:
                dlog(f'Foto-Suche {filename}: HTTP {response.status_code} bei {urlsplit(response.url).netloc}.')
                return filename, scope_domain, found
            links = _extract_vinted_item_links_from_search_html(response.text, scope_domain)
            for found_domain, item_id in links:
                if found_domain in results:
                    found.append((found_domain, int(item_id)))
        except Exception as exc:
            dlog(f'Foto-Suche {filename} fehlgeschlagen: {type(exc).__name__}: {exc}')
        return filename, scope_domain, found

    with ThreadPoolExecutor(max_workers=8) as pool:
        for filename, scope_domain, found_items in pool.map(run_job, jobs):
            for domain, item_id in found_items:
                if domain in results:
                    results[domain].add(item_id)
                    found_by_filename.setdefault(filename, {}).setdefault(domain, set()).add(item_id)

    for filename in cleaned_filenames:
        total = sum(len(ids) for ids in found_by_filename.get(filename, {}).values())
        dlog(f'Foto-Dateiname {filename}: {total} Suchindex-Kandidaten gefunden (Kandidaten werden auf der Artikelseite überprüft).')
    return results


def _photo_filename_stem(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip().strip('"\'')
    if value.startswith(('http://', 'https://', '//')):
        parsed = urlsplit(value if not value.startswith('//') else f'https:{value}')
        value = unquote(os.path.basename(parsed.path))
    else:
        value = os.path.basename(value.replace('\\', '/'))
    name, extension = os.path.splitext(value)
    if not name or extension.lower() not in {'.webp', '.jpg', '.jpeg', '.png', '.avif'}:
        return None
    return name.casefold()


def item_contains_photo_filename(item: Dict[str, Any], filename: str) -> bool:
    """Return whether item photo metadata names or links to the supplied image."""
    target_stem = _photo_filename_stem(filename)
    photos = item.get('photos') if isinstance(item, dict) else None
    if not target_stem or not isinstance(photos, list):
        return False

    filename_fields = {
        'filename', 'original_filename', 'original_file_name',
        'upload_file_name', 'client_file_name', 'source_filename',
    }

    for photo in photos:
        for image_url in get_image_urls(photo):
            if _photo_filename_stem(image_url) == target_stem:
                return True

        stack = [photo]
        seen = set()
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                marker = id(value)
                if marker in seen:
                    continue
                seen.add(marker)
                for key, child in value.items():
                    if key.casefold() in filename_fields and _photo_filename_stem(child) == target_stem:
                        return True
                    if isinstance(child, (dict, list, tuple)):
                        stack.append(child)
            elif isinstance(value, (list, tuple)):
                stack.extend(value)
    return False


def find_items_by_photo_filenames(
    context: Any,
    filenames: List[str],
    preferred_domain: str = 'vinted.de',
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
    max_candidate_pages: int = 100,
) -> Dict[str, Dict[str, Any]]:
    """Resolve indexed image filenames to publicly accessible articles that contain them."""
    cleaned_filenames = list(dict.fromkeys(
        str(value).strip().strip('"\'')
        for value in filenames
        if PHOTO_FILENAME_PATTERN.fullmatch(str(value).strip().strip('"\''))
    ))
    if not cleaned_filenames:
        return {}

    def log(message: str) -> None:
        if status_callback:
            status_callback(message)
        else:
            print(message)

    log(f'[*] Searching public Vinted indexes across all markets for {len(cleaned_filenames)} photo filename(s)...')
    indexed_ids = get_photo_filename_indexed_items(
        cleaned_filenames,
        preferred_domain=preferred_domain,
        debug_mode=debug_mode,
        status_callback=status_callback,
    )

    candidates = []
    for domain, item_ids in indexed_ids.items():
        for item_id in sorted(item_ids):
            candidates.append((domain, item_id))
    if len(candidates) > max_candidate_pages:
        log(f'[!] Limiting filename-search verification to {max_candidate_pages} of {len(candidates)} indexed article candidates.')
        candidates = candidates[:max_candidate_pages]

    found: Dict[str, Dict[str, Any]] = {}
    by_domain: Dict[str, List[int]] = {}
    for domain, item_id in candidates:
        by_domain.setdefault(domain, []).append(int(item_id))

    for domain, item_ids in by_domain.items():
        candidate_items = fetch_exact_item_ids_native(
            context=context,
            item_ids=item_ids,
            domain=domain,
            debug_mode=debug_mode,
            status_callback=status_callback,
        )
        for item_id, item in candidate_items.items():
            matched_filenames = [
                filename for filename in cleaned_filenames
                if item_contains_photo_filename(item, filename)
            ]
            if matched_filenames:
                found[str(item_id)] = item
                log(f'[+] Photo filename match {", ".join(matched_filenames)} -> item {item_id} on {domain}.')

    for filename in cleaned_filenames:
        match_count = sum(
            1 for item in found.values()
            if item_contains_photo_filename(item, filename)
        )
        if match_count == 0:
            log(f'[!] No publicly indexed, accessible article matched photo filename {filename}.')
    log(f'[+] Photo filename search verified {len(found)} matching article(s).')
    return found


def find_public_indexed_item_by_id(
    context: Any,
    item_id: str,
    preferred_domain: str = 'vinted.de',
    slug: Optional[str] = None,
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Recover one unavailable listing from public indexes without expanding its scope."""
    if not str(item_id).isdigit():
        return None

    if status_callback:
        status_callback(f'[*] Searching public Vinted indexes for requested article {item_id}...')
    indexed_ids = get_google_indexed_items_by_domain(
        query_term=str(item_id),
        preferred_domain=preferred_domain,
        extra_terms=[slug] if slug else None,
        debug_mode=debug_mode,
        status_callback=status_callback,
    )

    for domain, candidate_ids in indexed_ids.items():
        if int(item_id) not in candidate_ids:
            continue
        recovered = fetch_exact_item_ids_native(
            context=context,
            item_ids=[int(item_id)],
            domain=domain,
            debug_mode=debug_mode,
            status_callback=status_callback,
        )
        item = recovered.get(str(item_id))
        if item and str(item.get('id')) == str(item_id) and item.get('photos'):
            if status_callback:
                status_callback(f'[+] Public index recovery found requested article {item_id} on {domain}.')
            return item
    return None


def _extract_meta_content(html: str, prop: str) -> Optional[str]:
    if not html:
        return None
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.I)
        if m:
            return unquote(m.group(1))
    return None


def _public_item_from_html(html: str, item_id: Optional[str] = None, page_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Build a minimal downloadable item from public SSR HTML when structured item JSON is missing."""
    if not html:
        return None
    clean_html = html_unescape(html).replace('\\/', '/')
    parsed = _extract_item_from_html(clean_html, item_id)
    seller = None
    if isinstance(parsed, dict) and isinstance(parsed.get('user'), dict) and parsed['user'].get('id'):
        seller = parsed['user']
    if not seller:
        seller = _extract_seller_link_from_html(clean_html)
    if parsed and parsed.get('photos'):
        parsed = _enrich_photo_urls_from_html(parsed, clean_html)
        if seller and not (isinstance(parsed.get('user'), dict) and parsed['user'].get('id')):
            parsed['user'] = seller
        if page_url and not parsed.get('url'):
            parsed['url'] = page_url
        return parsed

    title = _extract_meta_content(clean_html, 'og:title')
    if not title:
        title_match = re.search(r'<h1\b[^>]*>(.*?)</h1\s*>', clean_html, re.I | re.S)
        if title_match:
            title = re.sub(r'<[^>]+>', ' ', title_match.group(1))
            title = re.sub(r'\s+', ' ', html_unescape(title)).strip()
    if not title:
        title_match = re.search(r'<title\b[^>]*>(.*?)</title\s*>', clean_html, re.I | re.S)
        if title_match:
            title = re.sub(r'\s+', ' ', html_unescape(re.sub(r'<[^>]+>', ' ', title_match.group(1)))).strip()
    title = (title or (f'item_{item_id}' if item_id else 'vinted_item')).split('| Vinted')[0].strip()
    title_key = re.sub(r'[^a-z0-9]', '', title.lower())

    similar_match = re.search(r'(?:similar\s+items|articles\s+similaires|artículos\s+similares|articoli\s+simili|ähnliche\s+artikel)', clean_html, re.I)
    scan_html = clean_html[:similar_match.start()] if similar_match else clean_html
    groups: Dict[str, Dict[str, Any]] = {}
    image_tag_pattern = re.compile(r'<img\b[^>]*>', re.I | re.S)

    def attr(tag: str, name: str) -> Optional[str]:
        m = re.search(r'\b' + re.escape(name) + r'\s*=\s*(["\'])(.*?)\1', tag, re.I | re.S)
        return html_unescape(m.group(2)).replace('\\/', '/') if m else None

    for img_match in image_tag_pattern.finditer(scan_html):
        tag = img_match.group(0)
        alt = attr(tag, 'alt') or ''
        alt_key = re.sub(r'[^a-z0-9]', '', alt.lower())
        srcset = attr(tag, 'srcset') or ''
        candidates: List[Tuple[int, str]] = []
        for part in srcset.split(','):
            pieces = part.strip().split()
            if not pieces:
                continue
            url = pieces[0]
            width = 0
            if len(pieces) > 1:
                width_match = re.search(r'(\d+)w$', pieces[1])
                if width_match:
                    width = int(width_match.group(1))
                else:
                    density_match = re.search(r'([\d.]+)x$', pieces[1])
                    if density_match:
                        width = int(float(density_match.group(1)) * 1000)
            candidates.append((width, url))
        for attr_name in ('src', 'data-src', 'data-original'):
            value = attr(tag, attr_name)
            if value:
                candidates.append((0, value))
        if title_key and not (title_key in alt_key or (alt_key and alt_key.startswith(title_key))):
            # The item gallery's alt text usually begins with the listing title.
            continue
        for width, url in candidates:
            url = url.strip().strip('"\'')
            if not url.startswith(('http://', 'https://', '//')):
                continue
            if url.startswith('//'):
                url = 'https:' + url
            if 'vinted.net' not in url and '/t/' not in url and '/tc/' not in url:
                continue
            if url not in [u for g in groups.values() for _, u in g['variants']]:
                asset_match = re.search(r'/(?:t|tc)/([^/?#]+)/', url)
                key = asset_match.group(1) if asset_match else url.split('?', 1)[0]
                # If a platform emits multiple resolutions, merge them under the stable CDN asset id.
                group = groups.setdefault(key, {'id': key, 'variants': []})
                group['variants'].append((width, url))

    # Fallback to OpenGraph and any public CDN URLs if image tags were stripped/reshaped.
    if not groups:
        fallback_urls: List[str] = []
        for prop in ('og:image', 'twitter:image', 'twitter:image:src'):
            value = _extract_meta_content(clean_html, prop)
            if value and value not in fallback_urls:
                fallback_urls.append(value)
        for match in re.findall(r'https?://[^"\'<>\\\s]+(?:/t/|/tc/)[^"\'<>\\\s]+', scan_html):
            value = match.replace('\\/', '/').replace('&amp;', '&')
            if value not in fallback_urls:
                fallback_urls.append(value)
            if len(fallback_urls) >= 12:
                break
        for index, url in enumerate(fallback_urls):
            asset_match = re.search(r'/(?:t|tc)/([^/?#]+)/', url)
            key = asset_match.group(1) if asset_match else f'og_{index}'
            groups.setdefault(key, {'id': key, 'variants': []})['variants'].append((0, url))

    photos = []
    for key, group in groups.items():
        variants = sorted(group['variants'], key=lambda pair: (pair[0], _infer_vinted_url_width(pair[1])), reverse=True)
        urls = []
        for _, url in variants:
            if url not in urls:
                urls.append(url)
        if urls:
            photos.append({'id': key, 'url': urls[0], 'urls': urls})
    if not photos:
        return parsed

    numeric_id: Any = int(item_id) if item_id and str(item_id).isdigit() else item_id
    item: Dict[str, Any] = {
        'id': numeric_id,
        'title': title,
        'url': page_url,
        'photos': photos,
        'user': seller or {},
        '_recovered_from_public_html': True,
    }
    # Recover a few useful visible fields when available; avoid fabricating missing values.
    status_match = re.search(r'\b(Removed!|Sold|Vendu|Verkauft|Vendido|Venduto)\b', clean_html, re.I)
    if status_match:
        item['status'] = status_match.group(1)
    item = _enrich_photo_urls_from_html(item, clean_html)
    return item


def _infer_vinted_url_width(url: str) -> int:
    """Approximate image size from common Vinted CDN path fragments for sorting variants."""
    path = urlsplit(url).path
    matches = re.findall(r'(?:^|/)(?:f|w)?(\d{2,4})x(\d{2,4})(?:/|$)', path, re.I)
    if matches:
        return max(int(a) for a, _ in matches)
    match = re.search(r'(?:^|/)(?:f|w)(\d{2,4})(?:/|$)', path, re.I)
    if match:
        return int(match.group(1))
    return 0


def _recover_item_from_public_http(
    context,
    item_url: str,
    item_id: Optional[str],
    preferred_domain: str = 'vinted.de',
    auth_token: Optional[str] = None,
    debug_mode: bool = False,
    status_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Fetch the public item document as raw HTML if the browser DOM exposes no data."""
    def dlog(message: str) -> None:
        if debug_mode:
            if status_callback:
                status_callback(f'[DEBUG] {message}')
            else:
                print(f'[DEBUG] {message}')

    urls = [item_url]
    if item_id:
        canonical = f'https://www.{_normalise_domain(preferred_domain)}/items/{item_id}'
        if canonical not in urls:
            urls.append(canonical)

    headers = _request_headers(auth_token, preferred_domain)
    headers.update({
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Upgrade-Insecure-Requests': '1',
    })

    for url in urls:
        try:
            response = context.request.get(url, headers=headers, timeout=20000)
            try:
                status = response.status
                raw_html = response.text()
            except Exception:
                status, raw_html = -1, ''
            member_path_count = len(re.findall(r'/member/\d+', raw_html, re.I))
            has_item_id = bool(item_id and str(item_id) in raw_html)
            dlog(f'Raw-HTML-Fallback: HTTP {status}, {len(raw_html)} Zeichen, item-ID im HTML={has_item_id}, Member-Pfade={member_path_count}, URL={url}')
            if status != 200 or not raw_html:
                continue

            seller = _extract_user_from_html(raw_html, item_id)
            recovered = _public_item_from_html(raw_html, item_id, url)
            if not recovered or not recovered.get('photos'):
                recovered = _extract_item_from_html(raw_html, item_id)
            if isinstance(recovered, dict):
                if seller and not (isinstance(recovered.get('user'), dict) and recovered['user'].get('id')):
                    recovered['user'] = seller
                recovered.setdefault('url', url)
            if not seller and isinstance(recovered, dict):
                user = recovered.get('user')
                if isinstance(user, dict) and user.get('id'):
                    seller = user

            photo_count = len(recovered.get('photos', [])) if isinstance(recovered, dict) and isinstance(recovered.get('photos'), list) else 0
            dlog(f'Raw-HTML-Fallback-Ergebnis: Verkäufer-ID={seller.get("id") if isinstance(seller, dict) else None}, Fotos={photo_count}, Titel={(recovered.get("title") if isinstance(recovered, dict) else None)!r}')
            if photo_count or seller:
                if not recovered:
                    recovered = {
                        'id': int(item_id) if item_id and str(item_id).isdigit() else item_id,
                        'title': f'item_{item_id}' if item_id else 'vinted_item',
                        'url': url,
                        'photos': [],
                        'user': seller or {},
                    }
                elif seller and not recovered.get('user'):
                    recovered['user'] = seller
                return recovered, seller
        except Exception as exc:
            dlog(f'Raw-HTML-Fallback fehlgeschlagen für {url}: {exc}')
    return None, None


def _recover_item_from_page(page, item_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Recover item photos and seller identity from the public page when JSON API returns 404."""
    try:
        html = page.content()
    except Exception:
        html = ''

    item = _extract_item_from_html(html, item_id) if html else None
    seller = _extract_user_from_html(html, item_id) if html else None
    if item and item.get('photos'):
        item = _enrich_photo_urls_from_html(item, html)
        if seller and not (isinstance(item.get('user'), dict) and item['user'].get('id')):
            item['user'] = seller
        item.setdefault('url', getattr(page, 'url', None))
        return item

    if html:
        item = _public_item_from_html(html, item_id, getattr(page, 'url', None))
        if item and item.get('photos'):
            if seller and not (isinstance(item.get('user'), dict) and item['user'].get('id')):
                item['user'] = seller
            return item

    # DOM fallback: use visible gallery image alt/srcset values and the public member link.
    try:
        dom = page.evaluate(r"""() => {
            const title = (document.querySelector('h1')?.innerText || document.title || '').trim();
            const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
            const anchors = [...document.querySelectorAll('a[href*="/member/"]')].map(a => ({href:a.href, text:(a.innerText || '').trim()}));
            const headers = [...document.querySelectorAll('h1,h2,h3,h4')];
            const similar = headers.find(h => /similar items|articles similaires|artículos similares|articoli simili|ähnliche artikel/i.test(h.innerText || ''));
            const all = [...document.images].filter(img => /vinted\.net\/(t|tc)\//i.test(img.currentSrc || img.src || img.srcset || ''));
            const before = all.filter(img => !similar || (img.compareDocumentPosition(similar) & Node.DOCUMENT_POSITION_FOLLOWING));
            const targeted = before.filter(img => title && norm(img.alt).includes(norm(title)));
            const picked = (targeted.length ? targeted : before).slice(0, 20);
            return {
                title,
                anchors,
                images: picked.map(img => ({alt:img.alt || '', src:img.currentSrc || img.src || '', srcset:img.srcset || ''}))
            };
        }""")
    except Exception:
        dom = None
    if isinstance(dom, dict):
        if not seller:
            for anchor in dom.get('anchors', []):
                href = str(anchor.get('href') or '')
                m = re.search(r'/member/(\d+)(?:-([^/?#]+))?', href, re.I)
                if m:
                    seller = {'id': m.group(1), 'login': unquote(m.group(2)) if m.group(2) else (anchor.get('text') or None)}
                    break
        grouped: Dict[str, Dict[str, Any]] = {}
        title = str(dom.get('title') or '').split('| Vinted')[0].strip()
        for image_info in dom.get('images', []):
            values: List[Tuple[int, str]] = []
            for part in str(image_info.get('srcset') or '').split(','):
                pieces = part.strip().split()
                if pieces:
                    width = int(re.sub(r'\D', '', pieces[1]) or 0) if len(pieces) > 1 else 0
                    values.append((width, pieces[0]))
            if image_info.get('src'):
                values.append((0, image_info['src']))
            for width, url in values:
                if url.startswith('//'):
                    url = 'https:' + url
                if not url.startswith('http') or 'vinted.net' not in url:
                    continue
                if title and image_info.get('alt') and not re.sub(r'[^a-z0-9]', '', title.lower()) in re.sub(r'[^a-z0-9]', '', str(image_info.get('alt')).lower()):
                    continue
                asset = re.search(r'/(?:t|tc)/([^/?#]+)/', url)
                key = asset.group(1) if asset else url.split('?', 1)[0]
                group = grouped.setdefault(key, {'id': key, 'variants': []})
                group['variants'].append((width, url))
        photos = []
        for key, group in grouped.items():
            urls = [u for _, u in sorted(group['variants'], key=lambda x: (x[0], _infer_vinted_url_width(x[1])), reverse=True)]
            urls = list(dict.fromkeys(urls))
            if urls:
                photos.append({'id': key, 'url': urls[0], 'urls': urls})
        if photos:
            return {
                'id': int(item_id) if item_id and str(item_id).isdigit() else item_id,
                'title': title or (f'item_{item_id}' if item_id else 'vinted_item'),
                'url': getattr(page, 'url', None),
                'photos': photos,
                'user': seller or {},
                '_recovered_from_public_html': True,
            }

    if seller:
        # User identity can still be useful for searching indexed and other market listings,
        # even if this particular page no longer exposes image URLs to the browser.
        return {'id': int(item_id) if item_id and str(item_id).isdigit() else item_id,
                'title': f'item_{item_id}', 'url': getattr(page, 'url', None), 'photos': [], 'user': seller}
    return item


def _find_seller_from_search_engine(item_id: str, item_url: str) -> Optional[Dict[str, Any]]:
    """Use public search indexing only as a last-resort seller-identity recovery mechanism."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36'}
        queries = [
            f'"{item_id}" Vinted',
            f'"{item_url}"',
            f'site:vinted.* "{item_id}"',
        ]
        for query in queries:
            res = requests.get(f'https://html.duckduckgo.com/html/?q={quote(query)}', headers=headers, timeout=10)
            if res.status_code != 200:
                continue
            html = res.text
            member = re.search(r'https?://(?:www\.)?vinted\.[a-z.]+/member/(\d+)(?:-([^/?#"&< ]+))?', html, re.I)
            if member:
                return {'id': member.group(1), 'login': unquote(member.group(2)) if member.group(2) else None}
    except Exception:
        pass
    return None


def _merge_items(target: Dict[str, Dict[str, Any]], items: Any) -> int:
    added = 0
    if isinstance(items, dict):
        iterable = items.values()
    elif isinstance(items, list):
        iterable = items
    else:
        iterable = []
    for item in iterable:
        if isinstance(item, dict) and item.get('id'):
            key = str(item['id'])
            if key not in target:
                target[key] = item
                added += 1
    return added


def _first_nonempty_value(obj: Any, keys: List[str], max_depth: int = 8) -> Any:
    wanted = {re.sub(r"[^a-z0-9]", "", str(k).lower()) for k in keys}
    stack = [(obj, 0)]
    seen = set()
    while stack:
        value, depth = stack.pop()
        if depth > max_depth:
            continue
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen:
                continue
            seen.add(marker)
            for key, candidate in value.items():
                norm = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if norm in wanted and candidate not in (None, "", [], {}):
                    return candidate
            for child in value.values():
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
        elif isinstance(value, list):
            for child in reversed(value):
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
    return None


def _value_as_text(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("title", "name", "label", "value", "text", "login", "code"):
            if value.get(key) not in (None, ""):
                return value[key]
    return value


def save_item_metadata_json(item: Dict[str, Any], folder_path: str, clean_username: str, save_enabled: bool = True) -> None:
    if not save_enabled:
        return

    json_path = os.path.join(folder_path, "info.json")

    dt_obj, date_str = parse_item_date(item)
    created_at_val = dt_obj.strftime("%Y-%m-%d %H:%M:%S") if dt_obj else date_str

    price = item.get("price") or item.get("total_item_price")
    currency = item.get("currency") or item.get("currency_code") or "EUR"
    if isinstance(price, dict):
        currency = price.get("currency_code") or price.get("currency") or currency
        price = price.get("amount") or price.get("value")

    brand = item.get("brand_title")
    if not brand:
        brand = _value_as_text(item.get("brand"))
    if not brand:
        brand = _first_nonempty_value(item, ["brand_title", "brand_name", "brand"])
    brand = _value_as_text(brand) or ""

    category = item.get("category_title")
    if not category:
        category = _value_as_text(item.get("category"))
    if not category:
        category = _first_nonempty_value(item, ["category_title", "category_name", "category", "catalog", "path", "category_path"])
    category = _value_as_text(category) or ""

    description = item.get("description")
    if description in (None, ""):
        description = _first_nonempty_value(item, ["description", "description_text", "item_description", "body", "details"])
    description = description or ""

    size = item.get("size_title") or item.get("size")
    if not size:
        size = _first_nonempty_value(item, ["size_title", "size_name", "size"])
    size = _value_as_text(size) or ""

    status = item.get("status")
    if not status:
        status = _first_nonempty_value(item, ["status", "status_title", "condition", "condition_title"])
    status = _value_as_text(status) or ""

    favourite_count = item.get("favourite_count")
    if favourite_count is None:
        favourite_count = _first_nonempty_value(item, ["favourite_count", "favorite_count", "favorites_count", "favourites_count"])

    view_count = item.get("view_count")
    if view_count is None:
        view_count = _first_nonempty_value(item, ["view_count", "views", "views_count"])

    user_value = item.get("user")
    if isinstance(user_value, dict):
        user_value = user_value.get("login") or user_value.get("username") or clean_username
    user_value = user_value or clean_username

    item_id = item.get("id") or _first_nonempty_value(item, ["id", "item_id"])

    meta = {
        "id": item_id,
        "title": item.get("title") or _first_nonempty_value(item, ["title", "name"]) or f"item_{item_id}",
        "description": description,
        "price": str(price) if price is not None else "0.0",
        "currency": currency,
        "brand": brand,
        "category": category,
        "size": size,
        "status": status,
        "url": item.get("url") or _first_nonempty_value(item, ["url", "item_url"]) or f"https://www.vinted.de/items/{item_id}",
        "user": user_value,
        "created_at": created_at_val,
        "favourite_count": favourite_count if favourite_count is not None else 0,
        "view_count": view_count if view_count is not None else 0
    }

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(_json_safe(meta), f, indent=4, ensure_ascii=False)
    except Exception:
        pass


def _find_item_payload(payload: Any, item_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    stack = [(payload, 0)]
    seen = set()
    while stack:
        value, depth = stack.pop()
        if depth > 14:
            continue
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen:
                continue
            seen.add(marker)
            value_id = value.get("id")
            photos = value.get("photos")
            if (
                isinstance(photos, list)
                and photos
                and value_id is not None
                and (item_id is None or str(value_id) == str(item_id))
            ):
                return value
            preferred = ("item", "itemDetails", "item_details", "data", "result", "pageProps", "initialReduxState", "state")
            children = []
            for key in preferred:
                child = value.get(key)
                if isinstance(child, (dict, list)):
                    children.append(child)
            for key, child in value.items():
                if key not in preferred and isinstance(child, (dict, list)):
                    children.append(child)
            for child in reversed(children):
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            for child in reversed(value):
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
    return None


def _find_all_item_payloads(payload: Any) -> List[Dict[str, Any]]:
    stack = [(payload, 0)]
    seen_objects = set()
    found: Dict[str, Dict[str, Any]] = {}
    while stack:
        value, depth = stack.pop()
        if depth > 14:
            continue
        if isinstance(value, dict):
            marker = id(value)
            if marker in seen_objects:
                continue
            seen_objects.add(marker)
            value_id = value.get("id")
            photos = value.get("photos")
            if value_id is not None and isinstance(photos, list) and photos:
                found[str(value_id)] = value
            for child in value.values():
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
    return list(found.values())


# ================= SHARED CORE DOWNLOAD ENGINE =================
def process_and_download_items(
    items_list: List[Dict[str, Any]],
    target_dir: str,
    owner_label: str,
    max_workers: int = DEFAULT_MAX_WORKERS,
    save_json: bool = DEFAULT_SAVE_JSON,
    save_photo_metadata: bool = DEFAULT_SAVE_PHOTO_METADATA,
    convert_jpg: bool = DEFAULT_CONVERT_JPG,
    status_callback: Optional[Callable[[str], None]] = None,
    debug_mode: bool = DEFAULT_DEBUG_MODE
) -> None:
    def log(msg: str) -> None:
        if status_callback:
            status_callback(msg)
        else:
            print(msg)
            DEBUG_LOGGER.write(msg)

    os.makedirs(target_dir, exist_ok=True)
    download_tasks = []
    metadata_by_folder: Dict[str, Dict[str, Any]] = {}

    for item in items_list:
        item_id = item.get("id", "unknown")
        dt_obj, date_str = parse_item_date(item)
        title = sanitize_folder_name(item.get("title", f"item_{item_id}"))

        folder_name = f"{date_str}_{item_id}_{title}"
        item_folder = os.path.join(target_dir, folder_name)
        os.makedirs(item_folder, exist_ok=True)

        item_owner = item.get("user", {}).get("login") if isinstance(item.get("user"), dict) else owner_label
        save_item_metadata_json(item, item_folder, item_owner, save_enabled=save_json)

        photo_records: List[Dict[str, Any]] = []
        metadata_by_folder[item_folder] = {
            "item_id": item_id,
            "item_title": item.get("title"),
            "item_url": item.get("url") or f"https://www.vinted.de/items/{item_id}",
            "metadata_source_note": (
                "vinted_photo_metadata contains photo fields provided by Vinted. "
                "original_filename is only populated if Vinted supplies an explicit upload filename. "
                "EXIF GPS location data is omitted for privacy reasons."
            ),
            "photos": photo_records,
        }

        photos = item.get("photos", [])
        used_filenames = set()
        for idx, photo in enumerate(photos):
            img_urls = get_image_urls(photo)
            filename, filename_source, filename_field = get_photo_filename_and_source(photo, img_urls, idx + 1)
            photo_name_info = get_original_photo_filename(photo, img_urls)
            is_explicit_original = bool(photo_name_info and get_photo_filename_and_source(photo, img_urls, idx + 1)[1] == "original_metadata")
            photo_id = photo.get("id") if isinstance(photo, dict) else None
            raw_photo_data = photo if isinstance(photo, (dict, list, str, int, float, bool)) or photo is None else str(photo)

            stem, ext = os.path.splitext(filename)
            unique_name = filename
            suffix = 2
            while unique_name.casefold() in used_filenames:
                unique_name = f"{stem}_{suffix}{ext}"
                suffix += 1
            filename = unique_name
            used_filenames.add(filename.casefold())

            photo_record: Dict[str, Any] = {
                "photo_index": idx + 1,
                "photo_id": photo_id,
                "original_filename": photo_name_info[0] if is_explicit_original and photo_name_info else None,
                "original_filename_field": photo_name_info[1] if photo_name_info else None,
                "photo_metadata_filename": photo_name_info[0] if photo_name_info and not is_explicit_original else None,
                "filename_source": filename_source,
                "saved_filename": filename if img_urls else None,
                "source_image_urls": img_urls,
                "vinted_photo_metadata": raw_photo_data,
                "download": {"status": "pending" if img_urls else "no_image_url"},
            }
            photo_records.append(photo_record)

            if not img_urls:
                continue

            download_tasks.append((img_urls, item_folder, filename, dt_obj, photo_record, convert_jpg, status_callback, debug_mode))

    if download_tasks:
        log(f"[*] Checking/upgrading {len(download_tasks)} images for maximum quality using {max_workers} threads...")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(download_single_image, task) for task in download_tasks]

            if not status_callback:
                for _ in tqdm(as_completed(futures), total=len(futures), desc="[2/2] Downloading images", unit="img"):
                    pass
            else:
                completed = 0
                for _ in as_completed(futures):
                    completed += 1
                    status_callback(f"Downloading images: {completed}/{len(download_tasks)}")

    if save_photo_metadata:
        _write_photo_metadata(metadata_by_folder)

    if not save_photo_metadata:
        for folder_path in metadata_by_folder:
            stale_path = os.path.join(folder_path, "photo_metadata.json")
            try:
                if os.path.isfile(stale_path):
                    os.remove(stale_path)
            except OSError:
                pass

    log(f"\n[✔] Done! All files saved to: '{os.path.abspath(target_dir)}'")


def resolve_user_to_url(user_input: str, domain: str = "vinted.de") -> str:
    user_input = user_input.strip()

    if user_input.startswith("http://") or user_input.startswith("https://"):
        return user_input

    if user_input.startswith("@"):
        user_input = user_input[1:]

    if re.match(r'^\d+(-.+)?$', user_input):
        return f"https://www.{domain}/member/{user_input}"

    print(f"[*] Looking up User ID for username '{user_input}' via Vinted API...")
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*"
        })
        session.get(f"https://www.{domain}", timeout=5)
        api_url = f"https://www.{domain}/api/v2/users?search_text={user_input}"
        res = session.get(api_url, timeout=5)
        
        if res.status_code == 200:
            data = res.json()
            users = data.get("users", [])
            for u in users:
                if str(u.get("login", "")).lower() == user_input.lower():
                    return f"https://www.{domain}/member/{u.get('id')}-{u.get('login')}"
            if users:
                return f"https://www.{domain}/member/{users[0].get('id')}-{users[0].get('login')}"
    except Exception:
        pass

    return f"https://www.{domain}/member/{user_input}"


def extract_clean_username(profile_url: str, page_title: str, captured_items: Dict[Any, Any]) -> str:
    for item in captured_items.values():
        user_info = item.get("user") or item.get("user_login")
        if isinstance(user_info, dict) and user_info.get("login"):
            return sanitize_folder_name(user_info.get("login"))
        elif isinstance(user_info, str):
            return sanitize_folder_name(user_info)

    if page_title and "-" in page_title:
        title_name = page_title.split("-")[0].strip()
        if title_name and "vinted" not in title_name.lower():
            return sanitize_folder_name(title_name)

    match = re.search(r'/member/(?:\d+-)?([^/?#]+)', profile_url)
    if match:
        raw_name = match.group(1)
        return sanitize_folder_name(re.sub(r'^\d+-', '', raw_name))

    return "vinted_user"


def matches_category(item: Dict[str, Any], category_filter: Optional[str]) -> bool:
    if not category_filter:
        return True

    filter_lower = category_filter.lower().strip()
    title = str(item.get("title", "")).lower()
    brand = str(item.get("brand_title", "")).lower()
    
    category_data = item.get("category", {})
    category_title = str(category_data.get("title", "")).lower() if isinstance(category_data, dict) else ""
    
    return (filter_lower in title) or (filter_lower in category_title) or (filter_lower in brand)


# ================= UNIFIED DOWNLOADER ENGINE =================
def download_vinted_items(
    raw_inputs: List[str],
    output_dir: str = DEFAULT_OUTPUT_DIR,
    category_filter: Optional[str] = None,
    headless: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
    save_json: bool = DEFAULT_SAVE_JSON,
    save_photo_metadata: bool = DEFAULT_SAVE_PHOTO_METADATA,
    convert_jpg: bool = DEFAULT_CONVERT_JPG,
    enable_deep_scan: bool = DEFAULT_ENABLE_DEEP_SCAN,
    id_probe_range: int = DEFAULT_ID_PROBE_RANGE,
    debug_mode: bool = DEFAULT_DEBUG_MODE,
    status_callback: Optional[Callable[[str], None]] = None,
    photo_filename_hints: Optional[List[str]] = None
) -> None:
    def log(msg: str) -> None:
        if status_callback:
            status_callback(msg)
        else:
            print(msg)
            DEBUG_LOGGER.write(msg)

    def dlog(msg: str) -> None:
        if debug_mode:
            log(f"[DEBUG] {msg}")

    clean_inputs = [i.strip() for i in raw_inputs if i.strip()]
    if not clean_inputs:
        log("[!] No inputs provided.")
        return

    item_urls = []
    profile_inputs = []
    parsed_photo_hints = list(photo_filename_hints or [])

    for inp in clean_inputs:
        filename_match = re.fullmatch(
            r'(?:(?:photo|image|filename)\s*:\s*)?([A-Za-z0-9_-]{1,80}\.(?:webp|jpe?g|png|avif))',
            inp.strip().strip('"\''),
            re.I,
        )
        if filename_match:
            filename = filename_match.group(1)
            if filename not in parsed_photo_hints:
                parsed_photo_hints.append(filename)
        elif re.search(r'/items/\d+', inp):
            item_urls.append(inp)
        else:
            profile_inputs.append(inp)

    log(f"[*] Input analysis: {len(profile_inputs)} profile(s) / username(s), {len(item_urls)} direct item URL(s), {len(parsed_photo_hints)} photo filename clue(s).")
    if parsed_photo_hints:
        log('[*] Photo filename clues: ' + ', '.join(parsed_photo_hints) + ' (will be checked against public item photo metadata).')

    with sync_playwright() as p:
        log("[*] Launching browser engine...")
        browser = None
        for channel in ["chrome", "msedge"]:
            try:
                browser = p.chromium.launch(headless=headless, channel=channel)
                dlog(f"Browser gestartet via Channel: {channel.upper()}")
                break
            except Exception:
                continue
        if not browser:
            browser = p.chromium.launch(headless=headless)

        storage_path = SESSION_FILE if os.path.exists(SESSION_FILE) else None
        if storage_path:
            dlog(f"Lade gespeicherte Session aus '{SESSION_FILE}'...")

        context = browser.new_context(
            storage_state=storage_path,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        api_captured_items: Dict[str, Dict[str, Any]] = {}

        def handle_response(response):
            try:
                if response.status != 200 or "json" not in response.headers.get("content-type", "").lower():
                    return
                if "/api/" not in response.url and "/items/" not in response.url:
                    return
                data = response.json()
                if isinstance(data, dict) and "items" in data:
                    for item in data["items"]:
                        if isinstance(item, dict) and item.get("id"):
                            api_captured_items[str(item["id"])] = item

                url_match = re.search(r"/items/(\d+)(?:[/?#]|$)", response.url)
                expected_id = url_match.group(1) if url_match else None

                if expected_id:
                    item = _find_item_payload(data, expected_id)
                    if item and item.get("id"):
                        api_captured_items[str(item["id"])] = item
                        return

                for item in _find_all_item_payloads(data):
                    if item.get("id"):
                        api_captured_items[str(item["id"])] = item
            except Exception:
                pass

        page.on("response", handle_response)

        # ---------------- 1. PROCESS PROFILE INPUTS ----------------
        for prof in profile_inputs:
            api_captured_items.clear()
            profile_url = resolve_user_to_url(prof)
            domain = urlsplit(profile_url).netloc.replace("www.", "") or "vinted.de"
            log(f"[*] Processing Profile: {profile_url}")

            prof_captured = {}
            try:
                page.goto(profile_url, wait_until="networkidle", timeout=25000)
                page_title = page.title()

                try:
                    page.click("#onetrust-accept-btn-handler", timeout=2000)
                except Exception:
                    pass

                log("[*] Scrolling profile to retrieve active items...")
                last_count = 0
                no_new_attempts = 0
                while True:
                    page.evaluate("window.scrollBy(0, 800);")
                    time.sleep(0.6)
                    current_count = len(api_captured_items)
                    if current_count == last_count:
                        no_new_attempts += 1
                        if no_new_attempts % 4 == 0:
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            time.sleep(1.5)
                        if no_new_attempts >= 10:
                            break
                    else:
                        no_new_attempts = 0
                        last_count = current_count

                prof_captured = dict(api_captured_items)
            except Exception as e:
                log(f"[!] Warning: Could not fully load profile page ({e}). Falling back to Deep Scan...")
                page_title = prof

            clean_username = extract_clean_username(profile_url, page_title, prof_captured)

            # Determine the seller even if there are no visible inventory cards.
            target_user_id = None
            for item in prof_captured.values():
                user_info = item.get("user")
                if isinstance(user_info, dict) and user_info.get("id"):
                    target_user_id = user_info.get("id")
                    break
            if not target_user_id:
                member_match = re.search(r'/member/(\d+)', page.url or profile_url, re.I)
                if member_match:
                    target_user_id = member_match.group(1)
            if not target_user_id:
                page_seller = _extract_seller_link_from_html(page.content())
                if page_seller:
                    target_user_id = page_seller.get('id')

            auth_token = get_vinted_auth_token(page)
            if target_user_id:
                log(f"[*] Seller {target_user_id} erkannt. Scanne aktuell sichtbare Artikel auf allen Vinted-Domains...")
                marketplace_items = fetch_user_items_all_domains(
                    context=context,
                    user_id=target_user_id,
                    preferred_domain=domain,
                    auth_token=auth_token,
                    debug_mode=debug_mode,
                    status_callback=log
                )
                _merge_items(prof_captured, marketplace_items)

                if enable_deep_scan:
                    public_profile_items = fetch_user_items_from_public_profile_html(
                        context=context,
                        user_id=target_user_id,
                        user_login=clean_username,
                        preferred_domain=domain,
                        auth_token=auth_token,
                        debug_mode=debug_mode,
                        status_callback=log,
                    )
                    if public_profile_items:
                        added_from_profile = _merge_items(prof_captured, public_profile_items)
                        log(f"[+] Public profile links: recovered {added_from_profile} additional seller-owned item(s).")
            else:
                log('[!] Keine Verkäufer-ID auf dem Profil ermittelbar; der Multi-Domain-Inventarscan ist nicht möglich.')

            if enable_deep_scan:
                log(f"[*] Running Deep Scan for profile '{clean_username}'...")
                indexed_ids_by_domain: Dict[str, set] = {}
                if clean_username:
                    indexed_ids_by_domain = get_google_indexed_items_by_domain(
                        clean_username,
                        preferred_domain=domain,
                        extra_terms=[str(target_user_id)] if target_user_id else None,
                        debug_mode=debug_mode,
                        status_callback=log,
                    )
                    for scan_domain, indexed_ids in indexed_ids_by_domain.items():
                        if indexed_ids:
                            log(f"[+] Search index {scan_domain}: {len(indexed_ids)} item IDs recovered for seller.")
                indexed_added = 0
                if target_user_id:
                    for scan_domain, ids_for_domain in indexed_ids_by_domain.items():
                        exact_items = fetch_exact_item_ids_native(
                            context=context,
                            item_ids=sorted(ids_for_domain),
                            domain=scan_domain,
                            target_user_id=target_user_id,
                            auth_token=auth_token,
                            debug_mode=debug_mode,
                            status_callback=log
                        )
                        indexed_added += _merge_items(prof_captured, exact_items)
                if indexed_added:
                    log(f"[+] Search-index recovery: {indexed_added} additional seller items recovered.")

                # A single representative recent ID keeps sequence probing bounded. Exact
                # indexed IDs and current inventory were already checked separately.
                valid_ids = sorted(int(k) for k in prof_captured.keys() if str(k).isdigit())
                if target_user_id and valid_ids:
                    seed_id = valid_ids[-1]
                    probed_items = probe_vinted_id_range_native(
                        context=context,
                        seed_ids=[seed_id],
                        target_user_id=target_user_id,
                        buffer_range=id_probe_range,
                        preferred_domain=domain,
                        auth_token=auth_token,
                        debug_mode=debug_mode,
                        status_callback=log
                    )
                    added_count = _merge_items(prof_captured, probed_items)
                    log(f"[+] Deep Scan complete: Recovered {added_count} additional historical items.")
                elif not target_user_id:
                    log('[!] Deep Scan ID-Probing skipped because no seller ID could be recovered.')
                else:
                    log('[!] Deep Scan has no usable item ID seed.')

            filtered = [item for item in prof_captured.values() if matches_category(item, category_filter)]
            if filtered:
                target_folder = os.path.join(output_dir, clean_username)
                process_and_download_items(
                    items_list=filtered,
                    target_dir=target_folder,
                    owner_label=clean_username,
                    max_workers=max_workers,
                    save_json=save_json,
                    save_photo_metadata=save_photo_metadata,
                    convert_jpg=convert_jpg,
                    debug_mode=debug_mode,
                    status_callback=log
                )

        # ---------------- 2. PROCESS DIRECT ITEM URLS ----------------
        if item_urls or parsed_photo_hints:
            direct_items_by_id: Dict[str, Dict[str, Any]] = {}
            if parsed_photo_hints:
                filename_items = find_items_by_photo_filenames(
                    context=context,
                    filenames=parsed_photo_hints,
                    preferred_domain='vinted.de',
                    debug_mode=debug_mode,
                    status_callback=log,
                )
                _merge_items(direct_items_by_id, list(filename_items.values()))

            if item_urls:
                log(f"[*] Processing {len(item_urls)} direct item URL(s)...")
            for idx, item_url in enumerate(item_urls, 1):
                log(f"[{idx}/{len(item_urls)}] Opening Direct Item: {item_url}")
                preferred_domain = _normalise_domain(urlsplit(item_url).netloc) or 'vinted.de'
                match = re.search(r'/items/(\d+)', item_url)
                item_id = str(match.group(1)) if match else None
                item_slug = None
                if match:
                    slug_match = re.match(r'/items/\d+-([^/?#]+)', urlsplit(item_url).path, re.I)
                    item_slug = unquote(slug_match.group(1)) if slug_match else None
                auth_token = None
                item_data = None

                try:
                    page_response = page.goto(item_url, wait_until='domcontentloaded', timeout=30000)
                    # Vinted's current item pages may hydrate the gallery and seller links
                    # after DOMContentLoaded. Give that short client-side pass time to finish.
                    try:
                        page.wait_for_load_state('networkidle', timeout=5000)
                    except Exception:
                        pass
                    try:
                        page.wait_for_timeout(900)
                    except Exception:
                        pass
                    try:
                        page.click('#onetrust-accept-btn-handler', timeout=1200)
                    except Exception:
                        pass
                    auth_token = get_vinted_auth_token(page)

                    try:
                        page_html_for_debug = page.content()
                        page_title_for_debug = page.title()
                        page_status_for_debug = page_response.status if page_response else 'unknown'
                        page_member_count = len(re.findall(r'/member/\d+', page_html_for_debug, re.I))
                        page_has_item_id = bool(item_id and str(item_id) in page_html_for_debug)
                        dlog(f'Direct page response={page_status_for_debug}, final_url={page.url}, title={page_title_for_debug!r}, HTML={len(page_html_for_debug)} chars, item-ID present={page_has_item_id}, member paths={page_member_count}')
                    except Exception as diagnostic_error:
                        dlog(f'Direct page diagnostics failed: {diagnostic_error}')

                    # Current Vinted pages can retain itemDto/photos in server-rendered HTML
                    # while /api/v2/items/{id} returns 404. Prefer DOM/Next.js recovery first.
                    if item_id:
                        item_data = _recover_item_from_page(page, item_id)
                        if item_data and item_data.get('photos'):
                            log(f"[+] Item recovered from public HTML: {len(item_data.get('photos', []))} photo entries.")
                        elif item_data and isinstance(item_data.get('user'), dict) and item_data['user'].get('id'):
                            user = item_data['user']
                            dlog(f'Seller link found in browser HTML without photos: ID={user.get("id")}, login={user.get("login") or user.get("username")}')

                    # A separate raw document request can contain the server-rendered data
                    # even when Chrome's live DOM was blocked or not fully hydrated.
                    if item_id and (not item_data or not item_data.get('photos') or not (isinstance(item_data.get('user'), dict) and item_data['user'].get('id'))):
                        raw_item, raw_seller = _recover_item_from_public_http(
                            context=context,
                            item_url=item_url,
                            item_id=item_id,
                            preferred_domain=preferred_domain,
                            auth_token=auth_token,
                            debug_mode=debug_mode,
                            status_callback=log,
                        )
                        if raw_item and raw_item.get('photos'):
                            item_data = raw_item
                        elif raw_seller and (not isinstance(item_data, dict) or not item_data.get('photos')):
                            item_data = raw_item or {
                                'id': int(item_id) if str(item_id).isdigit() else item_id,
                                'title': f'item_{item_id}',
                                'url': item_url,
                                'photos': [],
                                'user': raw_seller,
                            }

                    if not item_data or not item_data.get('photos'):
                        api_item = fetch_item_with_domain_fallback(
                            context=context,
                            item_id=item_id,
                            preferred_domain=preferred_domain,
                            auth_token=auth_token,
                            debug_mode=debug_mode,
                            status_callback=log
                        ) if item_id else None
                        if api_item:
                            item_data = api_item

                    if item_data and item_data.get('photos'):
                        if not item_id or str(item_data.get('id')) != item_id:
                            log(f"[!] Skipping recovered data that does not match the requested item ID {item_id or '(missing)'}.")
                            item_data = None

                    if item_data and item_data.get('photos'):
                        _merge_items(direct_items_by_id, [item_data])
                        log(f"[+] Direct item recovered ({len(item_data.get('photos', []))} photo entries).")
                        if debug_mode:
                            for photo_index, photo in enumerate(item_data.get('photos', []), 1):
                                urls = get_image_urls(photo)
                                tc_count = sum(1 for image_url in urls if '/tc/' in urlsplit(image_url).path.lower())
                                size_paths = sorted({
                                    (re.search(r'/(?:t/[^/]+/|tc/[^/]+/)([^/]+)/', urlsplit(image_url).path, re.I).group(1)
                                     if re.search(r'/(?:t/[^/]+/|tc/[^/]+/)([^/]+)/', urlsplit(image_url).path, re.I)
                                     else 'other')
                                    for image_url in urls
                                })
                                dlog(f'Fotoquelle {photo_index}: {len(urls)} URLs aus Metadaten/HTML, echte /tc/-URLs={tc_count}, Pfadvarianten={size_paths}.')
                    else:
                        item_data = None

                except Exception as e:
                    log(f"[!] Error processing {item_url}: {e}")

                if not any(str(existing.get('id')) == item_id for existing in direct_items_by_id.values()):
                    indexed_item = find_public_indexed_item_by_id(
                        context=context,
                        item_id=item_id,
                        preferred_domain=preferred_domain,
                        slug=item_slug,
                        debug_mode=debug_mode,
                        status_callback=log,
                    ) if item_id else None
                    if indexed_item:
                        _merge_items(direct_items_by_id, [indexed_item])

                # Direct item URLs are intentionally exact-target downloads. Seller
                # inventory and deep scans belong to profile downloads, not item URLs.

            filtered_direct = [item for item in direct_items_by_id.values() if matches_category(item, category_filter)]
            if filtered_direct:
                log(f"[*] Download queue: {len(filtered_direct)} requested item(s) after filtering.")
                target_folder = os.path.join(output_dir, 'Direct_Archive')
                process_and_download_items(
                    items_list=filtered_direct,
                    target_dir=target_folder,
                    owner_label='direct_archive',
                    max_workers=max_workers,
                    save_json=save_json,
                    save_photo_metadata=save_photo_metadata,
                    convert_jpg=convert_jpg,
                    debug_mode=debug_mode,
                    status_callback=log
                )
            else:
                log('[!] No downloadable requested items were recovered from the direct URL(s).')

        browser.close()


# --- MINIMALIST BLACK & WHITE GUI WITH TABBED SUITE ---
def launch_gui() -> None:
    if not TKINTER_AVAILABLE:
        print("[!] Tkinter is not available on this system.")
        return

    root = tk.Tk()
    root.title("Vinted Downloading Suite")
    root.geometry("660x670")
    root.resizable(False, False)
    root.configure(bg="#121212")

    style = ttk.Style()
    style.theme_use("clam")

    BG_DARK = "#121212"
    ENTRY_BG = "#2a2a2a"
    TEXT_WHITE = "#ffffff"
    TEXT_MUTED = "#888888"

    style.configure("TFrame", background=BG_DARK)
    style.configure("TLabel", background=BG_DARK, foreground=TEXT_WHITE, font=("Segoe UI", 9))
    style.configure("Header.TLabel", background=BG_DARK, foreground=TEXT_WHITE, font=("Segoe UI", 12, "bold"))
    style.configure("TEntry", fieldbackground=ENTRY_BG, foreground=TEXT_WHITE, insertcolor=TEXT_WHITE, borderwidth=0)
    style.configure("TSpinbox", fieldbackground=ENTRY_BG, foreground=TEXT_WHITE, insertcolor=TEXT_WHITE, borderwidth=0)

    style.configure("TNotebook", background=BG_DARK, borderwidth=0)
    style.configure("TNotebook.Tab", background="#1e1e1e", foreground=TEXT_MUTED, padding=[12, 6], font=("Segoe UI", 9, "bold"))
    style.map("TNotebook.Tab", background=[("selected", "#2d2d2d")], foreground=[("selected", TEXT_WHITE)])

    style.configure("Main.TButton", background="#ffffff", foreground="#000000", font=("Segoe UI", 9, "bold"), borderwidth=0)
    style.map("Main.TButton", background=[("active", "#e0e0e0")])

    style.configure("Sec.TButton", background="#2d2d2d", foreground="#ffffff", font=("Segoe UI", 8), borderwidth=0)
    style.map("Sec.TButton", background=[("active", "#3d3d3d")])

    app_settings = load_app_settings()
    dir_value = str(app_settings.get("output_dir") or DEFAULT_OUTPUT_DIR)
    try:
        threads_value = max(1, min(DEFAULT_MAX_WORKERS, int(app_settings.get("max_workers", DEFAULT_MAX_WORKERS))))
    except (TypeError, ValueError):
        threads_value = DEFAULT_MAX_WORKERS
    try:
        id_probe_range_value = max(50, min(1000, int(app_settings.get("id_probe_range", DEFAULT_ID_PROBE_RANGE))))
    except (TypeError, ValueError):
        id_probe_range_value = DEFAULT_ID_PROBE_RANGE

    dir_var = tk.StringVar(value=os.path.abspath(dir_value))
    min_size_var = tk.IntVar(value=DEFAULT_MIN_IMAGE_SIZE)
    threads_var = tk.IntVar(value=threads_value)
    save_json_var = tk.BooleanVar(value=bool(app_settings.get("save_json", DEFAULT_SAVE_JSON)))
    save_photo_metadata_var = tk.BooleanVar(value=bool(app_settings.get("save_photo_metadata", DEFAULT_SAVE_PHOTO_METADATA)))
    
    convert_jpg_var = tk.BooleanVar(value=bool(app_settings.get("convert_jpg", DEFAULT_CONVERT_JPG)))
    enable_deep_scan_var = tk.BooleanVar(value=bool(app_settings.get("enable_deep_scan", DEFAULT_ENABLE_DEEP_SCAN)))
    id_probe_range_var = tk.IntVar(value=id_probe_range_value)
    debug_mode_var = tk.BooleanVar(value=bool(app_settings.get("debug_mode", DEFAULT_DEBUG_MODE)))

    main_container = ttk.Frame(root, padding="15")
    main_container.pack(fill=tk.BOTH, expand=True)

    ttk.Label(main_container, text="VINTED DOWNLOADING SUITE", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 10))

    notebook = ttk.Notebook(main_container)
    notebook.pack(fill=tk.BOTH, expand=False, pady=(0, 10))

    # --- TAB 1: UNIFIED DOWNLOADER ---
    tab_downloader = ttk.Frame(notebook, padding="15")
    notebook.add(tab_downloader, text=" Downloader ")

    ttk.Label(tab_downloader, text="Vinted Inputs (Username, Profile URL, Item URL or photo filename like 1787767452.webp - one per line):").pack(anchor=tk.W, pady=(0, 2))
    inputs_text = scrolledtext.ScrolledText(tab_downloader, height=6, bg="#2a2a2a", fg="#ffffff", insertbackground="white", font=("Consolas", 8), borderwidth=0)
    inputs_text.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

    ttk.Label(tab_downloader, text="Filter by Category / Keyword (Optional):").pack(anchor=tk.W, pady=(0, 2))
    cat_entry = ttk.Entry(tab_downloader, width=70)
    cat_entry.pack(fill=tk.X, pady=(0, 5))
    cat_entry.insert(0, str(app_settings.get("category_filter") or ""))

    # --- TAB 2: SETTINGS ---
    tab_settings = ttk.Frame(notebook, padding="15")
    notebook.add(tab_settings, text=" Settings ")

    ttk.Label(tab_settings, text="Output Directory:").pack(anchor=tk.W, pady=(0, 2))
    dir_frame = ttk.Frame(tab_settings)
    dir_frame.pack(fill=tk.X, pady=(0, 8))

    dir_entry = ttk.Entry(dir_frame, textvariable=dir_var)
    dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def browse_dir():
        chosen = filedialog.askdirectory()
        if chosen:
            dir_var.set(chosen)

    ttk.Button(dir_frame, text="Browse...", style="Sec.TButton", command=browse_dir).pack(side=tk.RIGHT, padx=(5, 0))

    settings_grid = ttk.Frame(tab_settings)
    settings_grid.pack(fill=tk.X, pady=(5, 5))

    ttk.Label(settings_grid, text="Number of Threads to use:").grid(row=0, column=0, sticky=tk.W, pady=2)
    ttk.Spinbox(settings_grid, from_=1, to=DEFAULT_MAX_WORKERS, textvariable=threads_var, width=8).grid(row=0, column=1, sticky=tk.W, padx=10, pady=2)
    ttk.Label(settings_grid, text=f"Detected usable threads: {DEFAULT_MAX_WORKERS}").grid(row=0, column=2, sticky=tk.W, padx=10, pady=2)

    ttk.Label(settings_grid, text="ID Probing Buffer (±):").grid(row=1, column=0, sticky=tk.W, pady=2)
    ttk.Spinbox(settings_grid, from_=50, to=1000, increment=50, textvariable=id_probe_range_var, width=8).grid(row=1, column=1, sticky=tk.W, padx=10, pady=2)

    # Session / Login Button
    btn_session = ttk.Button(
        tab_settings,
        text="Open Interactive Session / Login Window",
        style="Sec.TButton",
        command=lambda: threading.Thread(
            target=open_interactive_session_window,
            args=("vinted.de", update_log),
            daemon=True
        ).start()
    )
    btn_session.pack(anchor=tk.W, pady=(6, 6))

    tk.Checkbutton(tab_settings, text="Force HD & Convert Images to High-Quality JPG (WebP/PNG -> JPG)", variable=convert_jpg_var, bg=BG_DARK, fg=TEXT_WHITE, selectcolor="#2a2a2a", activebackground=BG_DARK, activeforeground=TEXT_WHITE).pack(anchor=tk.W, pady=(2, 0))
    tk.Checkbutton(tab_settings, text="Enable Deep Scan (Search Hidden/Sold/Archived Items via ID Probing)", variable=enable_deep_scan_var, bg=BG_DARK, fg=TEXT_WHITE, selectcolor="#2a2a2a", activebackground=BG_DARK, activeforeground=TEXT_WHITE).pack(anchor=tk.W, pady=(2, 0))
    tk.Checkbutton(tab_settings, text="Enable Debug Mode (Detailed background logs in console)", variable=debug_mode_var, bg=BG_DARK, fg=TEXT_WHITE, selectcolor="#2a2a2a", activebackground=BG_DARK, activeforeground=TEXT_WHITE).pack(anchor=tk.W, pady=(2, 0))
    tk.Checkbutton(tab_settings, text="Save 'info.json' Metadata Files", variable=save_json_var, bg=BG_DARK, fg=TEXT_WHITE, selectcolor="#2a2a2a", activebackground=BG_DARK, activeforeground=TEXT_WHITE).pack(anchor=tk.W, pady=(2, 0))
    tk.Checkbutton(tab_settings, text="Save 'photo_metadata.json' Files", variable=save_photo_metadata_var, bg=BG_DARK, fg=TEXT_WHITE, selectcolor="#2a2a2a", activebackground=BG_DARK, activeforeground=TEXT_WHITE).pack(anchor=tk.W, pady=(2, 0))

    # --- CONSOLE ---
    console_frame = ttk.Frame(main_container)
    console_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

    ttk.Label(console_frame, text="LOG CONSOLE").pack(anchor=tk.W, pady=(0, 2))
    log_text = tk.Text(console_frame, height=7, bg="#000000", fg="#ffffff", insertbackground="white", font=("Consolas", 8), borderwidth=1, relief="solid")
    log_text.pack(fill=tk.BOTH, expand=True)

    def update_log(msg: str) -> None:
        text = str(msg)
        if DEBUG_LOGGER.enabled:
            DEBUG_LOGGER.write(text)
        log_text.config(state=tk.NORMAL)
        log_text.insert(tk.END, text + "\n")
        log_text.see(tk.END)
        log_text.config(state=tk.DISABLED)
        root.update_idletasks()

    def current_settings() -> Dict[str, Any]:
        try:
            workers = max(1, min(DEFAULT_MAX_WORKERS, int(threads_var.get())))
        except (tk.TclError, TypeError, ValueError):
            workers = DEFAULT_MAX_WORKERS
        try:
            probe_range = max(50, min(1000, int(id_probe_range_var.get())))
        except (tk.TclError, TypeError, ValueError):
            probe_range = DEFAULT_ID_PROBE_RANGE
        return {
            "output_dir": dir_var.get().strip() or DEFAULT_OUTPUT_DIR,
            "max_workers": workers,
            "save_json": bool(save_json_var.get()),
            "save_photo_metadata": bool(save_photo_metadata_var.get()),
            "convert_jpg": bool(convert_jpg_var.get()),
            "enable_deep_scan": bool(enable_deep_scan_var.get()),
            "id_probe_range": probe_range,
            "debug_mode": bool(debug_mode_var.get()),
            "category_filter": cat_entry.get().strip(),
        }

    def persist_settings(*_args: Any) -> None:
        if not save_app_settings(current_settings()):
            update_log(f"[!] Could not save settings to '{SETTINGS_FILE}'.")
        if debug_mode_var.get():
            if not DEBUG_LOGGER.enabled:
                log_path = DEBUG_LOGGER.enable()
                if log_path:
                    update_log(f"[*] Debug log: {log_path}")
        else:
            DEBUG_LOGGER.close()

    for variable in (dir_var, threads_var, save_json_var, save_photo_metadata_var,
                     convert_jpg_var, enable_deep_scan_var, id_probe_range_var, debug_mode_var):
        variable.trace_add("write", persist_settings)
    cat_entry.bind("<KeyRelease>", persist_settings)
    if debug_mode_var.get():
        log_path = DEBUG_LOGGER.enable(new_file=True)
        if log_path:
            update_log(f"[*] Debug log: {log_path}")
    persist_settings()

    def close_window() -> None:
        DEBUG_LOGGER.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_window)

    def start_download() -> None:
        current_tab = notebook.index(notebook.select())
        
        if current_tab == 1:
            persist_settings()
            messagebox.showinfo("Settings", "Settings are saved automatically.")
            return

        raw_lines = inputs_text.get("1.0", tk.END).splitlines()
        clean_inputs = [line.strip() for line in raw_lines if line.strip()]

        if not clean_inputs:
            messagebox.showerror("Error", "Please enter at least one Username, Profile URL, Item URL, or photo filename clue!")
            return

        btn_start.config(state=tk.DISABLED)
        log_text.config(state=tk.NORMAL)
        log_text.delete(1.0, tk.END)
        log_text.config(state=tk.DISABLED)

        def run_thread() -> None:
            try:
                settings = current_settings()
                download_vinted_items(
                    raw_inputs=clean_inputs,
                    output_dir=settings["output_dir"],
                    category_filter=cat_entry.get().strip() or None,
                    headless=True,
                    max_workers=settings["max_workers"],
                    save_json=settings["save_json"],
                    save_photo_metadata=settings["save_photo_metadata"],
                    convert_jpg=settings["convert_jpg"],
                    enable_deep_scan=settings["enable_deep_scan"],
                    id_probe_range=settings["id_probe_range"],
                    debug_mode=settings["debug_mode"],
                    status_callback=update_log
                )
                messagebox.showinfo("Success", "Task completed successfully!")
            except Exception as ex:
                update_log(f"\n[!] Error: {ex}")
                messagebox.showerror("Error", str(ex))
            finally:
                btn_start.config(state=tk.NORMAL)

        threading.Thread(target=run_thread, daemon=True).start()

    btn_start = ttk.Button(main_container, text="START DOWNLOAD", style="Main.TButton", command=start_download)
    btn_start.pack(fill=tk.X, ipady=5)

    root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vinted Image Downloader - Universal Downloader")
    parser.add_argument("inputs", nargs="*", help="Usernames, Profile URLs, or Direct Item URLs")
    parser.add_argument("-c", "--category", default=None, help="Filter by category or keyword")
    parser.add_argument("-o", "--output", default=None, help="Output directory")
    parser.add_argument("--threads", type=int, default=None, help="Number of parallel download threads")
    parser.add_argument("--headless", action="store_true", help="Run browser in background")
    parser.add_argument("--deep-scan", dest="deep_scan", action="store_true", default=None, help="Enable deep scan for hidden/sold items")
    parser.add_argument("--no-deep-scan", dest="deep_scan", action="store_false", default=None, help="Disable deep scan")
    parser.add_argument("--debug", action="store_true", default=None, help="Enable debug logging")
    parser.add_argument("--no-debug", dest="debug", action="store_false", default=None, help="Disable debug logging")
    parser.add_argument("--cli", action="store_true", help="Force Command Line Interface mode")

    args = parser.parse_args()

    is_cli_run = bool(args.inputs or args.category or args.cli)

    if not is_cli_run and TKINTER_AVAILABLE:
        launch_gui()
    else:
        app_settings = load_app_settings()
        defaults = {
            "output_dir": DEFAULT_OUTPUT_DIR,
            "max_workers": DEFAULT_MAX_WORKERS,
            "save_json": DEFAULT_SAVE_JSON,
            "save_photo_metadata": DEFAULT_SAVE_PHOTO_METADATA,
            "convert_jpg": DEFAULT_CONVERT_JPG,
            "enable_deep_scan": DEFAULT_ENABLE_DEEP_SCAN,
            "id_probe_range": DEFAULT_ID_PROBE_RANGE,
            "debug_mode": DEFAULT_DEBUG_MODE,
        }
        for key, value in defaults.items():
            app_settings.setdefault(key, value)
        save_app_settings(app_settings)
        try:
            configured_workers = int(app_settings.get("max_workers", DEFAULT_MAX_WORKERS))
        except (TypeError, ValueError):
            configured_workers = DEFAULT_MAX_WORKERS
        max_workers = max(1, min(DEFAULT_MAX_WORKERS, args.threads if args.threads is not None else configured_workers))
        debug_mode = bool(app_settings.get("debug_mode", DEFAULT_DEBUG_MODE)) if args.debug is None else args.debug
        try:
            id_probe_range = max(50, min(1000, int(app_settings.get("id_probe_range", DEFAULT_ID_PROBE_RANGE))))
        except (TypeError, ValueError):
            id_probe_range = DEFAULT_ID_PROBE_RANGE
        if debug_mode:
            log_path = DEBUG_LOGGER.enable(new_file=True)
            if log_path:
                print(f"[*] Debug log: {log_path}")
        try:
            inputs = args.inputs
            if not inputs:
                user_inp = input("Enter Vinted Username, Profile URL, or Item URL: ").strip()
                if user_inp:
                    inputs = [user_inp]

            category = args.category
            if not category and len(sys.argv) <= 1:
                cat_input = input("Filter by category/keyword (Press ENTER for all items): ").strip()
                if cat_input:
                    category = cat_input

            if inputs:
                download_vinted_items(
                    raw_inputs=inputs,
                    output_dir=args.output or str(app_settings.get("output_dir") or DEFAULT_OUTPUT_DIR),
                    category_filter=category,
                    headless=args.headless,
                    max_workers=max_workers,
                    save_json=bool(app_settings.get("save_json", DEFAULT_SAVE_JSON)),
                    save_photo_metadata=bool(app_settings.get("save_photo_metadata", DEFAULT_SAVE_PHOTO_METADATA)),
                    convert_jpg=bool(app_settings.get("convert_jpg", DEFAULT_CONVERT_JPG)),
                    enable_deep_scan=(
                        bool(app_settings.get("enable_deep_scan", DEFAULT_ENABLE_DEEP_SCAN))
                        if args.deep_scan is None else args.deep_scan
                    ),
                    id_probe_range=id_probe_range,
                    debug_mode=debug_mode
                )
            else:
                print("[!] No inputs provided.")

        except Exception as e:
            print(f"\n[!] An error occurred during execution: {e}")
            DEBUG_LOGGER.write(f"[!] An error occurred during execution: {e}")

        finally:
            DEBUG_LOGGER.close()
            input("\nPress ENTER to exit...")