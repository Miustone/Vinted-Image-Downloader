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
from urllib.parse import urlsplit, unquote

# Optional Pillow import: Used to inspect exact image dimensions if installed.
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
DEFAULT_MAX_WORKERS = 6
DEFAULT_MIN_IMAGE_SIZE = 300
DEFAULT_SAVE_JSON = False
DEFAULT_SAVE_PHOTO_METADATA = False


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
    """Returns all known image URLs in descending quality priority."""
    if isinstance(photo, str):
        candidates = [photo]
    elif isinstance(photo, dict):
        candidates = []

        # API fields for original/HD images take priority over thumbnail URLs.
        for key in ("full_size_url", "fullSizeUrl", "original_url", "originalUrl"):
            value = photo.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)

        for key in ("high_resolution", "full_size", "extra_large", "large"):
            value = photo.get(key)
            if isinstance(value, dict):
                value = value.get("url") or value.get("src")
            if isinstance(value, str) and value:
                candidates.append(value)

        # DOM fallback can return multiple real srcset URLs for the same photo.
        for key in ("urls", "url_options", "image_urls"):
            value = photo.get(key)
            if isinstance(value, list):
                candidates.extend(u for u in value if isinstance(u, str) and u)

        # Some API responses only provide a list of thumbnails.
        thumbnails = photo.get("thumbnails")
        if isinstance(thumbnails, list):
            thumbnails = sorted(
                (t for t in thumbnails if isinstance(t, dict) and t.get("url")),
                key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0),
                reverse=True,
            )
            candidates.extend(t["url"] for t in thumbnails)

        value = photo.get("url")
        if isinstance(value, str) and value:
            candidates.append(value)
    else:
        return []

    unique_urls = []
    for url in candidates:
        if not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        if url not in unique_urls:
            unique_urls.append(url)
    return unique_urls


def get_highest_res_url(photo: Any) -> Optional[str]:
    """Compatibility helper: returns the first/highest quality image URL found."""
    urls = get_image_urls(photo)
    return urls[0] if urls else None


_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff")


def _normalise_image_filename(value: Any, image_urls: Optional[List[str]] = None) -> Optional[str]:
    """Validates an image filename extracted from metadata or HTTP headers."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().strip('"\'')
    if not candidate:
        return None

    # If a URL was provided instead of a filename, parse and use its path component.
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
        # For explicit original filenames missing extensions, infer the extension from an image URL.
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

    # Prevents using reserved device names under Windows.
    stem = os.path.splitext(candidate)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
        candidate = "_" + candidate
    return candidate


def get_original_photo_filename(photo: Any, image_urls: Optional[List[str]] = None) -> Optional[Tuple[str, str]]:
    """Recursively searches for original upload filenames if provided by Vinted."""
    if not isinstance(photo, dict):
        return None

    # Only fields explicitly referencing original upload filenames take priority.
    # Keys are normalized (lowercase, alphanumeric only) to match normal_key().
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
    """Generates a fallback filename from CDN URL, photo ID, or item index."""
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
    """Prefers explicit original filenames, followed by photo metadata and CDN URLs."""
    found_name = get_original_photo_filename(photo, urls)
    if found_name:
        key = re.sub(r"[^a-z0-9]", "", found_name[1].lower())
        if key in {"originalfilename", "sourcefilename", "uploadfilename", "uploaderfilename", "clientfilename", "originalname"}:
            return found_name[0], "original_metadata", found_name[1]
        return found_name[0], "photo_metadata_filename", found_name[1]
    fallback_name, fallback_source = _filename_from_url(urls, photo, index)
    return fallback_name, fallback_source, None


def get_photo_filename(photo: Any, urls: List[str], index: int) -> str:
    """Compatibility helper for callers that only require the filename."""
    return get_photo_filename_and_source(photo, urls, index)[0]


def _json_safe(value: Any) -> Any:
    """Safely converts image/EXIF values into JSON-serializable types."""
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
    """Inspects pixel dimensions, image format, and available EXIF metadata from raw image bytes."""
    width, height = _get_image_dimensions(content)
    info: Dict[str, Any] = {
        "pixel_width": width or None,
        "pixel_height": height or None,
        "format": None,
        "mode": None,
        "exif": {},
    }
    if Image is None:
        # Without Pillow, pixel dimensions parsed from file signatures are preserved.
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
                    # GPSInfo is intentionally omitted to avoid leaking location privacy.
                    if tag_name == "GPSInfo":
                        continue
                    if isinstance(value, (str, int, float, bytes, tuple, list)):
                        exif_data[tag_name] = _json_safe(value)
                # Camera metadata (e.g. DateTimeOriginal/Make/Model) resides in a separate EXIF IFD.
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
    """Optionally parses a filename provided by the CDN Content-Disposition HTTP header."""
    if not value:
        return None
    # Supports both filename= and RFC 5987 filename*= syntax.
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
    """Writes raw Vinted metadata and technical download details to photo_metadata.json."""
    for folder_path, payload in metadata_by_folder.items():
        try:
            output_path = os.path.join(folder_path, "photo_metadata.json")
            with open(output_path, "w", encoding="utf-8") as handle:
                json.dump(_json_safe(payload), handle, indent=4, ensure_ascii=False)
        except (OSError, TypeError, ValueError):
            pass


def _image_url_candidates(url: str) -> List[str]:
    """Generates potential Vinted HD URL variants including the original /tc/ path."""
    if not url:
        return []
    if url.startswith("//"):
        url = "https:" + url

    candidates: List[str] = []

    def add(candidate: str) -> None:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    # Vinted photo URLs often follow the format: /t/<photo-id>/f800/<timestamp>.jpeg?s=...
    # Full-size API URLs frequently use /tc/<photo-id>/<filename>.
    # The signature parameter is preserved; fallback variants are tested if CDN rejects it.
    match_full = re.match(
        r"^(https?://[^/]+)/t/([^/]+)/(f\d+(?:x\d+)?|s\d+(?:x\d+)?|t\d+(?:x\d+)?|thumb|small|medium|large|extra_large)/(.+)$",
        url,
        flags=re.IGNORECASE,
    )
    if match_full:
        host, photo_id, _size_tag, filename_and_query = match_full.groups()
        add(f"{host}/tc/{photo_id}/{filename_and_query}")

    # If the source URL already uses /tc/, test the original URL directly.
    if re.search(r"/tc/", url, flags=re.IGNORECASE):
        add(url)

    # Test various Vinted CDN dimensions. The downloader compares pixel scores
    # rather than blindly accepting the first HTTP 200 response.
    size_match = re.search(
        r"/(f\d+(?:x\d+)?|s\d+(?:x\d+)?|t\d+(?:x\d+)?|thumb|small|medium|large|extra_large)/",
        url,
        flags=re.IGNORECASE,
    )
    if size_match:
        size_tag = size_match.group(1)
        for target in ("f2400", "f2000", "f1600", "f1200", "f1000", "f800", "extra_large", "large"):
            candidate = url.replace(f"/{size_tag}/", f"/{target}/", 1)
            add(candidate)

    add(url)
    return candidates


def _get_image_dimensions(content: bytes) -> Tuple[int, int]:
    """Parses standard image dimensions without mandatory dependencies; Pillow is preferred if available."""
    if Image is not None:
        try:
            with Image.open(BytesIO(content)) as image:
                return image.size
        except Exception:
            pass

    # PNG: Width/Height stored as big-endian 32-bit integers at offset 16.
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return (int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big"))

    # GIF: Width/Height stored as little-endian 16-bit integers at offset 6.
    if content.startswith((b"GIF87a", b"GIF89a")) and len(content) >= 10:
        return (int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little"))

    # JPEG: Search for Start-of-Frame markers containing width and height.
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

    # WebP VP8X: Canvas width/height stored as 24-bit values minus 1.
    if len(content) >= 30 and content.startswith(b"RIFF") and content[8:12] == b"WEBP" and content[12:16] == b"VP8X":
        width = 1 + int.from_bytes(content[24:27], "little")
        height = 1 + int.from_bytes(content[27:30], "little")
        return (width, height)

    return (0, 0)


def _image_quality_score(content: bytes) -> Tuple[int, int, int, int]:
    """Scores image quality by total pixels, max dimension, sum of dimensions, and file size."""
    width, height = _get_image_dimensions(content)
    return (width * height, max(width, height), width + height, len(content))


def _looks_like_image(content: bytes, content_type: str) -> bool:
    """Validates that response content is a genuine image rather than an HTML error page."""
    if content_type and "image/" in content_type.lower():
        return True
    return (
        content.startswith(b"\xff\xd8\xff")  # JPEG
        or content.startswith(b"\x89PNG\r\n\x1a\n")  # PNG
        or content.startswith((b"GIF87a", b"GIF89a"))  # GIF
        or (content.startswith(b"RIFF") and content[8:12] == b"WEBP")  # WebP
        or (len(content) > 12 and content[4:8] == b"ftyp" and content[8:12] in (b"avif", b"avis"))
    )


def download_single_image(args: Tuple[Any, str, str, datetime, Optional[Dict[str, Any]]]) -> bool:
    """Downloads the highest resolution image and collects HTTP/file metadata."""
    if len(args) >= 5:
        url_input, folder_path, filename, item_datetime, photo_record = args[:5]
    else:  # Backward compatibility for internal callers
        url_input, folder_path, filename, item_datetime = args[:4]
        photo_record = None
    filepath = os.path.join(folder_path, filename)
    os.makedirs(folder_path, exist_ok=True)

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

    # Existing high-resolution files are kept; technical metadata is re-inspected.
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        try:
            with open(filepath, "rb") as existing_file:
                existing_content = existing_file.read()
            existing_score = _image_quality_score(existing_content)
            best_content = existing_content
            best_score = existing_score
            if existing_score[0] >= 1_500_000 and existing_score[1] >= 1400:
                if photo_record is not None:
                    record_file(existing_content, "existing_hd_reused")
                return True
        except OSError:
            pass

    tried = set()

    def fetch_and_score(candidate_url: str) -> bool:
        nonlocal best_content, best_score, best_response_info
        if not candidate_url or candidate_url in tried:
            return best_score[0] >= 1_500_000 and best_score[1] >= 1400
        tried.add(candidate_url)
        try:
            response = requests.get(candidate_url, headers=headers, timeout=(4, 8))
            if response.status_code != 200 or len(response.content) <= 2000:
                return False
            content_type = response.headers.get("content-type", "")
            if not _looks_like_image(response.content, content_type):
                return False

            score = _image_quality_score(response.content)
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
            return False
        except Exception:
            return False

    # Try exact signed API/srcset URLs first; test CDN variants only if initial candidates are low resolution.
    good_image_found = False
    for candidate_url in exact_urls:
        if fetch_and_score(candidate_url):
            good_image_found = True
            break

    if not good_image_found:
        variant_count = 0
        for source_url in exact_urls:
            for candidate_url in _image_url_candidates(source_url):
                if candidate_url in tried:
                    continue
                variant_count += 1
                if fetch_and_score(candidate_url):
                    good_image_found = True
                    break
                if variant_count >= 8:
                    break
            if good_image_found or variant_count >= 8:
                break

    if best_content is None:
        if photo_record is not None:
            photo_record["download"] = {"status": download_status}
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
                    return True
                current_score = _image_quality_score(current_content)
            except OSError:
                pass

        if current_score >= best_score and os.path.exists(filepath):
            if photo_record is not None:
                record_file(best_content, "existing_better_file_kept")
            return True

        temp_path = filepath + ".tmp"
        with open(temp_path, "wb") as image_file:
            image_file.write(best_content)
        os.replace(temp_path, filepath)
        if item_datetime:
            set_file_creation_time(filepath, item_datetime)
        download_status = "downloaded_or_upgraded"
        if photo_record is not None:
            record_file(best_content, download_status)
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


def _first_nonempty_value(obj: Any, keys: List[str], max_depth: int = 8) -> Any:
    """Recursively searches for the first non-empty value matching specified keys in nested API payloads."""
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
            # Direct key matches first.
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
    """Normalizes common Vinted dictionary object values to text for info.json."""
    if isinstance(value, dict):
        for key in ("title", "name", "label", "value", "text", "login", "code"):
            if value.get(key) not in (None, ""):
                return value[key]
    return value


def save_item_metadata_json(item: Dict[str, Any], folder_path: str, clean_username: str, save_enabled: bool = True) -> None:
    """Saves complete item metadata to info.json, replacing null/empty values with clean fallbacks."""
    if not save_enabled:
        return

    json_path = os.path.join(folder_path, "info.json")

    # 1. Creation date using deep photo timestamp analysis
    dt_obj, date_str = parse_item_date(item)
    created_at_val = dt_obj.strftime("%Y-%m-%d %H:%M:%S") if dt_obj else date_str

    # 2. Price and currency
    price = item.get("price") or item.get("total_item_price")
    currency = item.get("currency") or item.get("currency_code") or "EUR"
    if isinstance(price, dict):
        currency = price.get("currency_code") or price.get("currency") or currency
        price = price.get("amount") or price.get("value")

    # 3. Brand
    brand = item.get("brand_title")
    if not brand:
        brand = _value_as_text(item.get("brand"))
    if not brand:
        brand = _first_nonempty_value(item, ["brand_title", "brand_name", "brand"])
    brand = _value_as_text(brand) or ""

    # 4. Category (including catalog/category_path fallbacks)
    category = item.get("category_title")
    if not category:
        category = _value_as_text(item.get("category"))
    if not category:
        category = _first_nonempty_value(item, ["category_title", "category_name", "category", "catalog", "path", "category_path"])
    category = _value_as_text(category) or ""

    # 5. Description
    description = item.get("description")
    if description in (None, ""):
        description = _first_nonempty_value(item, ["description", "description_text", "item_description", "body", "details"])
    description = description or ""

    # 6. Size and condition
    size = item.get("size_title") or item.get("size")
    if not size:
        size = _first_nonempty_value(item, ["size_title", "size_name", "size"])
    size = _value_as_text(size) or ""

    status = item.get("status")
    if not status:
        status = _first_nonempty_value(item, ["status", "status_title", "condition", "condition_title"])
    status = _value_as_text(status) or ""

    # 7. Counters and username
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
    """Recursively locates a complete item object in nested API payloads."""
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
            # Search preferred container keys first, then remaining object fields.
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
    """Locates all item objects in an API payload (e.g. profile page API responses)."""
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
    status_callback: Optional[Callable[[str], None]] = None
) -> None:
    """Centralized processing and download function including photo metadata export."""
    def log(msg: str) -> None:
        if status_callback:
            status_callback(msg)
        else:
            print(msg)

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

            filepath = os.path.join(item_folder, filename)
            legacy_path = os.path.join(item_folder, f"image_{idx + 1}.jpg")
            if filename != f"image_{idx + 1}.jpg" and os.path.isfile(legacy_path) and not os.path.exists(filepath):
                try:
                    os.replace(legacy_path, filepath)
                except OSError:
                    pass

            download_tasks.append((img_urls, item_folder, filename, dt_obj, photo_record))

    if download_tasks:
        log(f"[*] Checking/upgrading {len(download_tasks)} images for maximum available quality using {max_workers} threads...")
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
        # Some CDN responses suggest a filename via Content-Disposition headers. Use this as a secondary choice.
        # Perform renames after all parallel downloads complete to keep collisions deterministic.
        for folder_path, payload in metadata_by_folder.items():
            photos = payload.get("photos", [])
            reserved = {str(record.get("saved_filename", "")).casefold() for record in photos if record.get("saved_filename")}
            for record in photos:
                download_info = record.get("download") or {}
                header_name = download_info.get("content_disposition_filename")
                if not header_name or record.get("original_filename"):
                    continue
                old_name = record.get("saved_filename")
                if not old_name or old_name.casefold() == header_name.casefold():
                    if old_name:
                        record["saved_filename"] = old_name
                    continue
                # Release reservation for old filename before renaming.
                reserved.discard(old_name.casefold())
                desired_name = header_name
                root, extension = os.path.splitext(desired_name)
                suffix = 2
                while desired_name.casefold() in reserved or (
                    os.path.exists(os.path.join(folder_path, desired_name))
                    and desired_name.casefold() != old_name.casefold()
                ):
                    desired_name = f"{root}_{suffix}{extension}"
                    suffix += 1
                old_path = os.path.join(folder_path, old_name)
                new_path = os.path.join(folder_path, desired_name)
                if os.path.isfile(old_path) and old_path != new_path:
                    try:
                        if not os.path.exists(new_path):
                            os.replace(old_path, new_path)
                        record["saved_filename"] = os.path.basename(new_path if os.path.exists(new_path) else old_path)
                        record["filename_source"] = "http_content_disposition"
                    except OSError:
                        record["saved_filename"] = old_name
                        reserved.add(old_name.casefold())
                else:
                    record["saved_filename"] = old_name
                    reserved.add(old_name.casefold())
                reserved.add(str(record.get("saved_filename", "")).casefold())

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


# ================= USER PROFILE DOWNLOADER =================
def download_vinted_user(
    user_input: str,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    category_filter: Optional[str] = None,
    headless: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
    save_json: bool = DEFAULT_SAVE_JSON,
    save_photo_metadata: bool = DEFAULT_SAVE_PHOTO_METADATA,
    status_callback: Optional[Callable[[str], None]] = None
) -> None:
    def log(msg: str) -> None:
        if status_callback:
            status_callback(msg)
        else:
            print(msg)

    profile_url = resolve_user_to_url(user_input)
    captured_items = {}
    page_title = ""

    with sync_playwright() as p:
        log("[*] Launching browser...")
        browser = None
        for channel in ["chrome", "msedge"]:
            try:
                browser = p.chromium.launch(headless=headless, channel=channel)
                log(f"[*] Using installed browser: {channel.upper()}")
                break
            except Exception:
                continue

        if not browser:
            browser = p.chromium.launch(headless=headless)

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        def handle_response(response):
            try:
                if "json" in response.headers.get("content-type", ""):
                    data = response.json()
                    if isinstance(data, dict) and "items" in data:
                        for item in data["items"]:
                            if isinstance(item, dict) and item.get("id"):
                                captured_items[item["id"]] = item
            except Exception:
                pass

        page.on("response", handle_response)
        log(f"[*] Opening profile: {profile_url}")
        page.goto(profile_url, wait_until="networkidle")
        page_title = page.title()

        try:
            page.click("#onetrust-accept-btn-handler", timeout=3000)
        except Exception:
            pass

        log("[*] Scrolling to load items...")
        last_item_count = 0
        no_new_items_attempts = 0

        while True:
            page.evaluate("window.scrollBy(0, 800);")
            time.sleep(0.7)

            current_count = len(captured_items)
            if current_count == last_item_count:
                no_new_items_attempts += 1
                if no_new_items_attempts % 5 == 0:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(2.0)
                if no_new_items_attempts >= 12:
                    break
            else:
                no_new_items_attempts = 0
                last_item_count = current_count

        log(f"[+] Total items found on profile: {len(captured_items)}")
        browser.close()

    if not captured_items:
        log("[!] No items found on this profile.")
        return

    all_items = list(captured_items.values())
    if category_filter:
        filtered_items = [item for item in all_items if matches_category(item, category_filter)]
        log(f"[*] Filter active: '{category_filter}' -> Matched {len(filtered_items)} of {len(all_items)} items.")
    else:
        filtered_items = all_items

    if not filtered_items:
        log("[!] No items matched your filter.")
        return

    clean_username = extract_clean_username(profile_url, page_title, captured_items)
    user_target_dir = os.path.join(output_dir, clean_username)

    # Hand off to shared download engine
    process_and_download_items(
        items_list=filtered_items,
        target_dir=user_target_dir,
        owner_label=clean_username,
        max_workers=max_workers,
        save_json=save_json,
        save_photo_metadata=save_photo_metadata,
        status_callback=status_callback
    )


# ================= DIRECT URL / ARCHIVE DOWNLOADER =================
def download_direct_urls(
    url_list: List[str],
    output_dir: str = DEFAULT_OUTPUT_DIR,
    headless: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
    min_img_size: int = DEFAULT_MIN_IMAGE_SIZE,
    save_json: bool = DEFAULT_SAVE_JSON,
    save_photo_metadata: bool = DEFAULT_SAVE_PHOTO_METADATA,
    status_callback: Optional[Callable[[str], None]] = None
) -> None:
    def log(msg: str) -> None:
        if status_callback:
            status_callback(msg)
        else:
            print(msg)

    valid_urls = [u.strip() for u in url_list if u.strip().startswith("http")]
    if not valid_urls:
        log("[!] No valid URLs provided.")
        return

    log(f"[*] Processing {len(valid_urls)} direct item URLs...")
    archive_dir = os.path.join(output_dir, "Direct_Archive")

    captured_item_objects = []

    with sync_playwright() as p:
        log("[*] Launching browser for Direct URLs...")
        browser = None
        for channel in ["chrome", "msedge"]:
            try:
                browser = p.chromium.launch(headless=headless, channel=channel)
                break
            except Exception:
                continue
        if not browser:
            browser = p.chromium.launch(headless=headless)

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        api_captured_items = {}

        # Intercept API responses. Profile endpoints often return lists of items;
        # record all payloads to guarantee we find target items.
        def handle_response(response):
            try:
                if response.status != 200 or "json" not in response.headers.get("content-type", "").lower():
                    return
                if "/api/" not in response.url and "/items/" not in response.url:
                    return
                data = response.json()
                url_match = re.search(r"/items/(\d+)(?:[/?#]|$)", response.url)
                expected_id = url_match.group(1) if url_match else None

                # Direct item API: prioritize requested item payload.
                if expected_id:
                    item = _find_item_payload(data, expected_id)
                    if item and item.get("id"):
                        api_captured_items[str(item["id"])] = item
                        return

                # Profile API: buffer all objects to locate exact item ID.
                for item in _find_all_item_payloads(data):
                    if item.get("id"):
                        api_captured_items[str(item["id"])] = item
            except Exception:
                pass

        page.on("response", handle_response)

        for idx, item_url in enumerate(valid_urls, 1):
            log(f"[{idx}/{len(valid_urls)}] Opening: {item_url}")
            try:
                page.goto(item_url, wait_until="domcontentloaded", timeout=20000)
                try:
                    page.click("#onetrust-accept-btn-handler", timeout=1200)
                except Exception:
                    pass

                match = re.search(r'/items/(\d+)', item_url)
                item_id = str(match.group(1)) if match else None
                item_data = api_captured_items.get(item_id) if item_id else None

                # 1. Search embedded Next.js / Redux state tree immediately.
                if not item_data:
                    item_data = page.evaluate("""(wantedId) => {
                        try {
                            const roots = [];
                            const el = document.querySelector('#__NEXT_DATA__');
                            if (el) {
                                const parsed = JSON.parse(el.textContent || el.innerText || '{}');
                                roots.push(parsed.props?.pageProps, parsed.props?.initialReduxState, parsed);
                            }
                            for (const key of ['__INITIAL_STATE__', '__NEXT_DATA__', '__APOLLO_STATE__']) {
                                try { if (window[key] && typeof window[key] === 'object') roots.push(window[key]); } catch (e) {}
                            }
                            const seen = new Set();
                            const find = (obj, depth) => {
                                if (!obj || typeof obj !== 'object' || depth > 14 || seen.has(obj)) return null;
                                seen.add(obj);
                                if (!Array.isArray(obj) && Array.isArray(obj.photos) && obj.photos.length && obj.id != null &&
                                    (!wantedId || String(obj.id) === String(wantedId))) return obj;
                                const preferred = ['item', 'itemDetails', 'item_details', 'data', 'result', 'pageProps', 'initialReduxState', 'state'];
                                for (const key of preferred) {
                                    if (obj[key] && typeof obj[key] === 'object') {
                                        const hit = find(obj[key], depth + 1);
                                        if (hit) return hit;
                                    }
                                }
                                for (const value of Object.values(obj)) {
                                    if (value && typeof value === 'object') {
                                        const hit = find(value, depth + 1);
                                        if (hit) return hit;
                                    }
                                }
                                return null;
                            };
                            for (const root of roots) {
                                const hit = find(root, 0);
                                if (hit) return hit;
                            }
                        } catch (e) {}
                        return null;
                    }""", item_id)

                # 2. Wait briefly if item endpoint JSON response is pending.
                if not item_data and item_id:
                    deadline = time.monotonic() + 2.5
                    while time.monotonic() < deadline and item_id not in api_captured_items:
                        page.wait_for_timeout(150)
                    item_data = api_captured_items.get(item_id)

                # Final state check before falling back to DOM parsing.
                if not item_data:
                    item_data = page.evaluate("""(wantedId) => {
                        try {
                            const el = document.querySelector('#__NEXT_DATA__');
                            const parsed = el ? JSON.parse(el.textContent || el.innerText || '{}') : {};
                            const roots = [parsed.props?.pageProps, parsed.props?.initialReduxState, parsed];
                            const seen = new Set();
                            const find = (obj, depth) => {
                                if (!obj || typeof obj !== 'object' || depth > 14 || seen.has(obj)) return null;
                                seen.add(obj);
                                if (!Array.isArray(obj) && Array.isArray(obj.photos) && obj.photos.length && obj.id != null &&
                                    (!wantedId || String(obj.id) === String(wantedId))) return obj;
                                for (const value of Object.values(obj)) {
                                    if (value && typeof value === 'object') {
                                        const hit = find(value, depth + 1);
                                        if (hit) return hit;
                                    }
                                }
                                return null;
                            };
                            for (const root of roots) { const hit = find(root, 0); if (hit) return hit; }
                        } catch (e) {}
                        return null;
                    }""", item_id)

                # 3. Buffer DOM image sources. Fallback to seller profile API if item metadata is missing.
                extracted = None
                seller_profile_url = None
                if not item_data:
                    seller_profile_url = page.evaluate("""() => {
                        try {
                            const links = Array.from(document.querySelectorAll('a[href*="/member/"]'));
                            for (const a of links) {
                                const u = new URL(a.href, location.origin);
                                const memberTail = u.pathname.split('/')[2] || '';
                                if (u.pathname.split('/')[1] === 'member' && /^[0-9]+(?:-|$)/.test(memberTail)) return u.href;
                            }
                        } catch (e) {}
                        return null;
                    }""")

                    # Store DOM fallback in memory before navigating to seller profile.
                    extracted = page.evaluate(r"""(minSize) => {
                        try {
                            const getMeta = (prop) => document.querySelector(`meta[property="${prop}"], meta[name="${prop}"]`)?.getAttribute('content') || '';
                            const title = getMeta('og:title') || document.title || 'Untitled';
                            const groups = new Map();
                            const normalize = (u) => {
                                if (!u || typeof u !== 'string') return '';
                                u = u.trim();
                                return u.startsWith('//') ? 'https:' + u : u;
                            };
                            const keyFor = (u) => {
                                const m = u.match(/\/t\/([^/]+)\//i);
                                return m ? m[1] : u.split('?')[0];
                            };
                            const addUrls = (rawUrls) => {
                                const urls = [];
                                for (const raw of rawUrls || []) {
                                    const u = normalize(raw);
                                    if (u && u.includes('vinted') && !urls.includes(u)) urls.push(u);
                                }
                                if (!urls.length) return;
                                const key = keyFor(urls[0]);
                                if (!groups.has(key)) groups.set(key, { urls: [] });
                                const group = groups.get(key);
                                for (const u of urls) if (!group.urls.includes(u)) group.urls.push(u);
                            };
                            const srcsetUrls = (raw) => {
                                if (!raw) return [];
                                return raw.split(',').map(part => {
                                    const bits = part.trim().split(/\s+/);
                                    const descriptor = bits[1] || '';
                                    const number = parseFloat(descriptor) || 0;
                                    const weight = descriptor.endsWith('w') ? number : (descriptor.endsWith('x') ? number * 1000 : 0);
                                    return { url: bits[0] || '', weight };
                                }).filter(x => x.url).sort((a, b) => b.weight - a.weight).map(x => x.url);
                            };

                            document.querySelectorAll('img').forEach(img => {
                                const srcset = img.getAttribute('srcset') || img.getAttribute('data-srcset') || '';
                                const options = srcsetUrls(srcset);
                                const current = img.currentSrc || img.getAttribute('data-src') || img.getAttribute('src') || '';
                                if (current && !options.includes(current)) options.push(current);
                                const best = options[0] || current;
                                const w = img.naturalWidth || img.width || 0;
                                const h = img.naturalHeight || img.height || 0;
                                if (best && best.includes('vinted') && (w >= minSize || h >= minSize)) addUrls(options);
                            });

                            const ogImg = getMeta('og:image');
                            if (ogImg) addUrls([ogImg]);
                            const jsonLdScript = document.querySelector('script[type="application/ld+json"]');
                            if (jsonLdScript) {
                                try {
                                    const ldData = JSON.parse(jsonLdScript.textContent || jsonLdScript.innerText || '{}');
                                    const addLdImages = (value) => {
                                        if (typeof value === 'string') addUrls([value]);
                                        else if (Array.isArray(value)) value.forEach(addLdImages);
                                        else if (value && typeof value === 'object') {
                                            if (value.image) addLdImages(value.image);
                                            if (value.url && typeof value.url === 'string') addUrls([value.url]);
                                        }
                                    };
                                    addLdImages(ldData.image);
                                } catch (e) {}
                            }

                            return {
                                title: title,
                                price: getMeta('product:price:amount') || '0',
                                photos: Array.from(groups.values()).map(g => ({
                                    urls: g.urls,
                                    full_size_url: g.urls[0],
                                    url: g.urls[0]
                                }))
                            };
                        } catch(e) {
                            return null;
                        }
                    }""", min_img_size)

                    # Fetch exact item via seller profile API.
                    if item_id and seller_profile_url:
                        try:
                            log("[*] Full item metadata missing; checking seller profile API for original photo URLs...")
                            page.goto(seller_profile_url, wait_until="domcontentloaded", timeout=15000)
                            try:
                                page.click("#onetrust-accept-btn-handler", timeout=1000)
                            except Exception:
                                pass
                            page.wait_for_timeout(500)

                            no_growth = 0
                            last_count = len(api_captured_items)
                            for _ in range(12):
                                item_data = api_captured_items.get(str(item_id))
                                if item_data and item_data.get("photos"):
                                    break
                                page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 700));")
                                page.wait_for_timeout(500)
                                item_data = api_captured_items.get(str(item_id))
                                if item_data and item_data.get("photos"):
                                    break
                                current_count = len(api_captured_items)
                                if current_count > last_count:
                                    no_growth = 0
                                    last_count = current_count
                                else:
                                    no_growth += 1
                                if no_growth >= 4:
                                    break

                            if item_data and item_data.get("photos"):
                                log("[+] Found exact item in seller profile API; using original photo metadata.")
                        except Exception as profile_error:
                            log(f"[*] Seller-profile API lookup unavailable; using page fallback ({profile_error}).")

                    # Fall back to HTML image sources only if API object remains unavailable.
                    if not item_data:
                        log("[!] No full item object found in API/page state or seller profile; using HTML image sources as fallback.")
                        log("[*] Extracting item photos from DOM (all srcset variants)...")
                        if extracted and extracted.get("photos"):
                            item_data = {
                                "id": item_id if item_id else "archived",
                                "title": extracted["title"],
                                "price": extracted["price"],
                                "photos": extracted["photos"],
                                "url": item_url,
                                "status": "sold_or_archived"
                            }

                if item_data and item_data.get("photos"):
                    log(f"[+] Captured item photo metadata: {len(item_data.get('photos', []))} photo entries.")
                    captured_item_objects.append(item_data)
                else:
                    log(f"[!] Could not fetch item data: {item_url}")

            except Exception as e:
                log(f"[!] Error processing {item_url}: {e}")

        browser.close()

    if not captured_item_objects:
        log("[!] No valid items were captured from the provided URLs.")
        return

    # Hand off to shared download engine
    process_and_download_items(
        items_list=captured_item_objects,
        target_dir=archive_dir,
        owner_label="direct_archive",
        max_workers=max_workers,
        save_json=save_json,
        save_photo_metadata=save_photo_metadata,
        status_callback=status_callback
    )


# --- MINIMALIST BLACK & WHITE GUI WITH TABBED SUITE ---
def launch_gui() -> None:
    if not TKINTER_AVAILABLE:
        print("[!] Tkinter is not available on this system.")
        return

    root = tk.Tk()
    root.title("Vinted Downloading Suite")
    root.geometry("640x580")
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

    dir_var = tk.StringVar(value=os.path.abspath(DEFAULT_OUTPUT_DIR))
    min_size_var = tk.IntVar(value=DEFAULT_MIN_IMAGE_SIZE)
    threads_var = tk.IntVar(value=DEFAULT_MAX_WORKERS)
    save_json_var = tk.BooleanVar(value=DEFAULT_SAVE_JSON)
    save_photo_metadata_var = tk.BooleanVar(value=DEFAULT_SAVE_PHOTO_METADATA)

    main_container = ttk.Frame(root, padding="15")
    main_container.pack(fill=tk.BOTH, expand=True)

    ttk.Label(main_container, text="VINTED DOWNLOADING SUITE", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 10))

    notebook = ttk.Notebook(main_container)
    notebook.pack(fill=tk.BOTH, expand=False, pady=(0, 10))

    tab_profile = ttk.Frame(notebook, padding="15")
    notebook.add(tab_profile, text=" User Profile Downloader ")

    ttk.Label(tab_profile, text="Vinted Profile URL or Username:").pack(anchor=tk.W, pady=(0, 2))
    user_entry = ttk.Entry(tab_profile, width=70)
    user_entry.pack(fill=tk.X, pady=(0, 8))

    ttk.Label(tab_profile, text="Filter by Category / Keyword (Optional):").pack(anchor=tk.W, pady=(0, 2))
    cat_entry = ttk.Entry(tab_profile, width=70)
    cat_entry.pack(fill=tk.X, pady=(0, 12))

    tab_archive = ttk.Frame(notebook, padding="15")
    notebook.add(tab_archive, text=" Direct URL Archive ")

    ttk.Label(tab_archive, text="Direct Item URLs (One per line, sold/active/archived):").pack(anchor=tk.W, pady=(0, 2))
    urls_text = scrolledtext.ScrolledText(tab_archive, height=5, bg="#2a2a2a", fg="#ffffff", insertbackground="white", font=("Consolas", 8), borderwidth=0)
    urls_text.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

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

    ttk.Label(tab_settings, text="Minimum Image Size Filter (px, removes logos/icons):").pack(anchor=tk.W, pady=(5, 2))
    ttk.Entry(tab_settings, textvariable=min_size_var, width=15).pack(anchor=tk.W, pady=(0, 8))

    ttk.Label(tab_settings, text="Parallel Download Threads:").pack(anchor=tk.W, pady=(5, 2))
    ttk.Spinbox(tab_settings, from_=1, to=16, textvariable=threads_var, width=13).pack(anchor=tk.W, pady=(0, 8))

    tk.Checkbutton(tab_settings, text="Save 'info.json' Metadata Files", variable=save_json_var, bg=BG_DARK, fg=TEXT_WHITE, selectcolor="#2a2a2a", activebackground=BG_DARK, activeforeground=TEXT_WHITE).pack(anchor=tk.W, pady=(5, 0))
    tk.Checkbutton(tab_settings, text="Save 'photo_metadata.json' Files", variable=save_photo_metadata_var, bg=BG_DARK, fg=TEXT_WHITE, selectcolor="#2a2a2a", activebackground=BG_DARK, activeforeground=TEXT_WHITE).pack(anchor=tk.W, pady=(5, 0))

    console_frame = ttk.Frame(main_container)
    console_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

    ttk.Label(console_frame, text="LOG CONSOLE").pack(anchor=tk.W, pady=(0, 2))
    log_text = tk.Text(console_frame, height=7, bg="#000000", fg="#ffffff", insertbackground="white", font=("Consolas", 8), borderwidth=1, relief="solid")
    log_text.pack(fill=tk.BOTH, expand=True)

    def update_log(msg: str) -> None:
        log_text.config(state=tk.NORMAL)
        log_text.insert(tk.END, msg + "\n")
        log_text.see(tk.END)
        log_text.config(state=tk.DISABLED)
        root.update_idletasks()

    def start_download() -> None:
        current_tab = notebook.index(notebook.select())
        
        btn_start.config(state=tk.DISABLED)
        log_text.config(state=tk.NORMAL)
        log_text.delete(1.0, tk.END)
        log_text.config(state=tk.DISABLED)

        def run_thread() -> None:
            try:
                if current_tab == 0:
                    user_val = user_entry.get().strip()
                    if not user_val:
                        messagebox.showerror("Error", "Please enter a Vinted profile or username!")
                        return
                    download_vinted_user(
                        user_input=user_val,
                        output_dir=dir_var.get().strip(),
                        category_filter=cat_entry.get().strip() or None,
                        headless=True,
                        max_workers=threads_var.get(),
                        save_json=save_json_var.get(),
                        save_photo_metadata=save_photo_metadata_var.get(),
                        status_callback=update_log
                    )
                elif current_tab == 1:
                    raw_urls = urls_text.get("1.0", tk.END).splitlines()
                    url_list = [u.strip() for u in raw_urls if u.strip()]
                    if not url_list:
                        messagebox.showerror("Error", "Please enter at least one Vinted URL!")
                        return
                    download_direct_urls(
                        url_list=url_list,
                        output_dir=dir_var.get().strip(),
                        headless=True,
                        max_workers=threads_var.get(),
                        min_img_size=min_size_var.get(),
                        save_json=save_json_var.get(),
                        save_photo_metadata=save_photo_metadata_var.get(),
                        status_callback=update_log
                    )
                elif current_tab == 2:
                    messagebox.showinfo("Settings", "Settings saved successfully!")
                    return

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
    parser = argparse.ArgumentParser(description="Vinted Image Downloader - Bulk Downloader with GUI and CLI support")
    parser.add_argument("user", nargs="?", help="URL or Username/ID of the Vinted profile")
    parser.add_argument("-c", "--category", default=None, help="Filter by category or keyword")
    parser.add_argument("-o", "--output", default="downloads", help="Output directory")
    parser.add_argument("--headless", action="store_true", help="Run browser in background")
    parser.add_argument("--cli", action="store_true", help="Force Command Line Interface mode")

    args = parser.parse_args()

    is_cli_run = bool(args.user or args.category or args.cli)

    if not is_cli_run and TKINTER_AVAILABLE:
        launch_gui()
    else:
        try:
            user_input = args.user
            if not user_input:
                user_input = input("Enter Vinted profile URL or Username: ").strip()

            category = args.category
            if not category and len(sys.argv) <= 1:
                cat_input = input("Filter by category/keyword (Press ENTER for all items): ").strip()
                if cat_input:
                    category = cat_input

            if user_input:
                download_vinted_user(user_input, output_dir=args.output, category_filter=category, headless=args.headless)
            else:
                print("[!] No user or URL provided.")

        except Exception as e:
            print(f"\n[!] An error occurred during execution: {e}")

        finally:
            input("\nPress ENTER to exit...")