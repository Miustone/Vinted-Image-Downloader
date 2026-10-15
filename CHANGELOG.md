# Changelog

All notable changes to the Vinted Image Downloader project will be documented in this file.

## [1.3.1] - 2026-10-15
### 🐛 Fixed
* **Workflow Auto-Release on Push

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