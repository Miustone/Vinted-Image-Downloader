# Vinted Image Downloader 📸

A powerful, fast, and automated desktop tool designed to bulk-download high-resolution photos and item metadata from Vinted seller profiles. Available as a standalone Windows executable or a flexible Command Line Interface (CLI) script.

---

## ✨ Features

* **Minimalist Monochrome GUI:** Comes with a built-in Dark Mode desktop interface (Tkinter-based) that launches automatically when double-clicking the `.exe`.
* **Seamless Headless Execution:** Runs silently in the background without popping up browser windows, offering a clean, distraction-free workflow.
* **Smart Username & ID Auto-Lookup:** Flexible input support! Enter full profile URLs (`https://www.vinted.de/member/...`), handles (`@username`), raw usernames (`username`), or numeric IDs.
* **Multi-Threaded Fast Downloads:** Downloads images concurrently up to 4x faster with dynamic worker pools and retry logic.
* **Skip / Resume Capability:** Automatically skips previously downloaded items to prevent bandwidth waste and duplicate downloads.
* **Highest Quality Image Tier (`/f800/`):** Automatically upgrades image URLs to fetch original, uncompressed photo resolutions.
* **Rich Metadata Export (`info.json`):** Generates a structured JSON sidecar file for each item containing product titles, prices, descriptions, brands, sizes, favorite counts, view counts, and original Vinted URLs.
* **Date & Timestamp Preservation:**
  * Folder naming scheme prefixing creation dates: `YYYY-MM-DD_ItemID_Title`.
  * Preserves original upload timestamps directly on Windows files (`os.utime` + Windows File Creation API).
* **Category & Keyword Filtering:** Easily filter a closet by specific keywords or categories (e.g., "Nike", "Dresses", "Jacket").
* **Standalone Windows `.exe`:** Runs natively on Windows using local system browsers (Chrome/Edge) without requiring manual Python or browser driver installations.

---

## 🚀 Getting Started

### Option A: Executable File (Recommended for Users)
1. Download the latest `VintedImageDownloader.exe` from the [Releases Page](https://github.com/Miustone/Vinted-Image-Downloader/releases).
2. Double-click `VintedImageDownloader.exe` to launch the **GUI**.
3. Paste a Vinted user profile URL, handle, or ID, select an output folder, and click **START DOWNLOAD**.

---

### Option B: Python CLI (Recommended for Developers)

#### 1. Requirements
* Python 3.10+
* Local installation of Google Chrome or Microsoft Edge

#### 2. Installation
Clone the repository and install the dependencies:
```bash
git clone https://github.com/Miustone/Vinted-Image-Downloader.git
cd Vinted-Image-Downloader
pip install -r requirements.txt
```

#### 3. Usage
Run in interactive CLI mode:
```bash
python vinted_image_downloader.py
```

Or pass direct arguments:
```bash
# Basic profile download
python vinted_image_downloader.py sa14.10.01

# Download with category filter and custom output directory
python vinted_image_downloader.py "https://www.vinted.de/member/12345678-username" -c "Nike" -o "my_downloads"
```

---

## 📂 Output Folder Structure

Downloads are organized cleanly inside the chosen output directory:

```text
downloads/
└── username/
    └── 2026-10-15_123456789_nike_vintage_jacket/
        ├── image_1.jpg
        ├── image_2.jpg
        ├── image_3.jpg
        └── info.json
```

---

## 📄 License

Distributed under the [MIT License](LICENSE).

---

## ⚖️ Disclaimer & Legal Notice

This software is an independent open-source tool designed for **personal backup, private archival, and educational purposes only**. 

* **No Affiliation:** This project is not affiliated, associated, authorized, endorsed by, or in any way officially connected with Vinted or any of its subsidiaries or affiliates.
* **Terms of Service:** Users are solely responsible for ensuring their usage complies with Vinted's Terms of Service, rate limits, and applicable local privacy/copyright laws (e.g., GDPR).
* **Limitation of Liability:** The author accepts no liability for any misuse of this tool, including account suspensions, IP rate limiting, or unauthorized distribution of third-party content.