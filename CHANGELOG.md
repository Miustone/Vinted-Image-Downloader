# Changelog

All notable changes to the Vinted Image Downloader project will be documented in this file.


## [1.6.0] - 2026-10-16

### 📁 Dynamic Filename Resolution
* **Original Filename Extraction:** Recursively inspects Vinted API metadata for explicit upload filenames (e.g., `original_filename`, `upload_file_name`, `client_file_name`, `source_filename`) to preserve original photo names whenever provided by Vinted[cite: 2].
* **HTTP Header Fallback:** Extracts filenames from `Content-Disposition` HTTP response headers as a secondary naming source[cite: 2].
* **CDN & ID Fallbacks:** Intelligently falls back to CDN URL path basenames, photo IDs (`photo_<id>.jpg`), or index-based names (`image_<index>.jpg`) when no original filename is present[cite: 2].
* **OS-Safe Naming & Collision Control:** Automatically sanitizes invalid file characters, resolves duplicate filename collisions with index suffixes, and guards against reserved Windows system names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`)[cite: 2].

### 📊 Photo Metadata & EXIF Export (`photo_metadata.json`)
* **Optional Extended Metadata Export:** Added a toggleable setting (`save_photo_metadata`) to export a detailed `photo_metadata.json` file per item folder[cite: 2].
* **In-Memory Image & EXIF Inspection (`_inspect_image_content`):** Analyzes downloaded image bytes to capture exact pixel dimensions ($W \times H$), image format, color mode, file size, and embedded EXIF tags (camera make, model, ISO, exposure, etc.) using Pillow or raw binary headers[cite: 2].
* **🔒 Privacy Protection:** Automatically filters out sensitive camera location data (`GPSInfo`) and private maker notes from EXIF exports to safeguard photographer privacy[cite: 2].
* **HTTP Diagnostic Tracking:** Records technical download metadata including final HTTP status codes, Content-Type, Content-Length, ETags, Last-Modified timestamps, and source URLs[cite: 2].

### 📝 Item Metadata Deep Parsing (`info.json`)
* **Deep Property Extraction:** Implemented recursive fallback lookups (`_first_nonempty_value`) across nested API schemas to ensure fields like `brand`, `category`, `description`, `size`, `condition/status`, `views`, and `favorites` are reliably populated even when API payloads vary[cite: 2].

### ⚡ Reliability & Atomic File Writes
* **Atomic File Updates:** Downloads images to isolated `.tmp` files before performing atomic replacements (`os.replace`), preventing corrupted or partial files if a download is interrupted[cite: 2].
* **Smart HD Re-Use:** Automatically verifies if an existing local file already satisfies high-resolution criteria ($1200 \times 1600\text{px}+$), bypassing unnecessary re-downloads while updating metadata[cite: 2].

### 🎨 GUI Improvements
* **Extended Settings Controls:** Added a new checkbutton in the *Settings* tab to toggle **`photo_metadata.json`** generation alongside **`info.json`** export[cite: 2].

---

## [1.5.0] - 2026-10-16

### 🚀 Major Improvements & Features
* **Unified Core Engine (`process_and_download_items`):** Consolidated the execution pipeline so both the **User Profile Downloader** and **Direct URL Archive** share identical processing logic, eliminating code redundancy and guaranteeing identical HD results.
* **Tabbed Dark Mode GUI:** Updated the Tkinter interface to a minimalist black & white dark theme featuring tabbed navigation (*User Profile Downloader*, *Direct URL Archive*, and *Settings*).
* **Centralized Settings:** Added global controls for output directory, parallel download thread count, minimum image size threshold, and optional metadata generation.

### 📸 Resolution Engine & Image Quality
* **Byte-Level Dimension Verification:** Implemented in-memory image header parsing to accurately analyze pixel dimensions ($W \times H$) and ensure genuine high-resolution ($1200 \times 1600\text{px}+$) assets are preferred over compressed preview thumbnails.
* **Automatic Quality Upgrades:** Automatically detects existing lower-resolution files (e.g., $600 \times 800\text{px}$) in target folders and replaces them whenever a higher-quality variant is retrieved.
* **Seller-Profile API Lookup:** Added an automated fallback mechanism for direct item URLs that lack full high-resolution metadata, querying the seller's profile API to fetch original HD photo links.

### 🛠 Fixes & Performance
* **Optimized Network Strategy:** Prioritizes signed original API URLs before attempting fallback CDN path mutations, significantly reducing redundant HTTP requests.
* **Preserved File Timestamps:** Accurate creation and modification timestamps are applied based on original Vinted item creation dates (compatible with Windows and Linux).

## [1.4.0] - 2026-10-15

### ✨ New Features
* **Seamless Headless Browser Execution:** The browser now runs entirely in the background without popping up any windows, offering a clean, distraction-free experience.
* **Minimalist Black & White GUI:** Added an ultra-clean, dark-themed graphical desktop interface when opening the application directly.
* **Metadata Export (`info.json`):** Generates a detailed JSON sidecar file for each item (title, price, brand, size, description, favorites, view count, and original product URL).
* **Smart Multi-Threaded Downloads:** Accelerated image downloads up to 4x using dynamic worker threads with automatic retry logic.
* **Skip / Resume Support:** Automatically detects existing images and skips already downloaded items to avoid redundant network usage.
* **Maximum Image Quality:** Automatically upgrades image URLs to the highest available resolution tier (`/f800/`).

### ⚡ Performance & Usability
* Full backward compatibility with Command Line Interface (CLI) parameters and automated execution.


## [1.3.1] - 2026-10-15
### 🐛 Fixed
* **Workflow:** Auto-Release on Push

## [1.3.0] - 2026-10-15

### ✨ Added
* **Username & ID Auto-Lookup:** You can now enter full profile URLs, handle names (`@username`), raw usernames, or numeric IDs.
* **Exact Item & Photo Timestamps:** Directories and files now reflect the actual creation date (`YYYY-MM-DD`) extracted from Vinted API photo metadata.
* **Windows Creation Date Preservation:** Preserved both creation and modification timestamps on Windows via kernel file attributes.
* **Clean Username Folder Structure:** Output folders are automatically named strictly after the extracted Vinted username instead of numeric IDs.

### 🐛 Fixed
* **Control Character & Line Break Sanitization (`WinError 123`):** Stripped invalid control characters, tabs, and newlines from item titles to prevent Windows directory creation crashes.
* **Local Browser Integration:** Seamlessly uses installed Google Chrome or Microsoft Edge instances without requiring additional driver downloads.
* **Infinite Scroll Improvements:** Enhanced automated page scrolling with dynamic wait times to consistently fetch large closets (1000+ items).

## [1.2.0] - 2026-10-14
### ✨ Added
* Added category and keyword filtering options.
* Added terminal progress bars for scroll capture and download operations.

## [1.1.0] - 2026-10-10
### ✨ Added
* Added automatic Cloudflare bypass using Playwright automation.
* Initial bulk image downloading functionality from Vinted API streams.