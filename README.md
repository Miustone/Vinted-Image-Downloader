# Vinted Image Downloader 📸

A powerful, fast, and automated desktop tool designed to bulk-download high-resolution photos and item metadata from Vinted seller profiles. Available as a standalone Windows executable or a flexible Command Line Interface (CLI) script.

---

## ✨ Features

- **Tabbed Monochrome Dark Mode GUI & CLI:** Features a minimalist desktop interface (Tkinter-based) with tabbed navigation (*User Profile Downloader*, *Direct URL Archive*, and *Settings*) alongside full CLI support.
- **Dual Mode Downloading (Profiles & Direct Links):** Seamlessly process entire user closets or target individual Vinted listing URLs—including active, sold, or archived items.
- **Maximum Quality Image Engine:** In-memory byte-level dimension verification ($1200\times1600\text{px}+$ / original resolution) that automatically scores photo candidates and upgrades existing lower-quality thumbnails ($600\times800\text{px}$) on disk.
- **Dynamic & Original Filename Extraction:** Preserves authentic uploaded filenames directly from Vinted API fields or `Content-Disposition` headers, with fallback photo-ID naming, duplicate collision handling, and OS-safe filename sanitization.
- **Clean & Comprehensive Item Metadata (`info.json`):** Generates structured JSON sidecar files with deep property resolution (`_first_nonempty_value`) to eliminate `null` values across product titles, descriptions, categories, brands, prices, sizes, condition states, favorites, view counts, and original creation dates.
- **Technical Photo & EXIF Metadata Export (`photo_metadata.json`):** Optionally captures detailed image technicals, including pixel dimensions, color modes, file sizes, HTTP diagnostic headers, and camera EXIF data.
- **🔒 Privacy-Conscious EXIF Processing:** Automatically strips sensitive GPS location data (`GPSInfo`) and private maker notes from photo metadata exports.
- **Smart Username & ID Auto-Lookup:** Supports flexible inputs including full profile URLs (`https://www.vinted.de/member/...`), handle syntax (`@username`), raw usernames, or numeric user IDs.
- **Preserved Upload Timestamps:** Folder naming scheme formatted as `YYYY-MM-DD_ItemID_Title`, applying exact item upload timestamps directly to Windows & Linux file attributes (`os.utime` + Win32 API).
- **Multi-Threaded Performance & Settings:** Fully configurable parallel download threads (1–16 workers), custom output directory paths, and minimum image dimension filters to skip small UI icons/logos.
- **Category & Keyword Closet Filtering:** Quickly filter profile downloads by specific keywords or categories (e.g., "Nike", "Dresses", "Jacket").

---

## 🚀 Getting Started

### Executable File (Recommended for Users)
1. Download the latest `VintedImageDownloader.exe` from the [Releases Page](https://github.com/Miustone/Vinted-Image-Downloader/releases).
2. Double-click `VintedImageDownloader.exe` to launch the **GUI**.
3. Paste a Vinted user profile URL, handle, or ID, select an output folder, and click **START DOWNLOAD**.

---


## 🐣 Beginner's Guide: How to Setup, Build & Run on Windows

This step-by-step guide is designed for complete beginners with **zero Python experience**. You do not need Linux or WSL — everything is done natively on Windows!

### Step 1: Install Python on Windows
1. Download **Python** from the official website: [python.org/downloads](https://www.python.org/downloads/)
2. Run the installer.
3. ⚠️ **CRITICAL STEP:** At the bottom of the first installation screen, check the box that says **"Add python.exe to PATH"** before clicking **Install Now**.

---

### Step 2: Download the Code
1. Click the green **Code** button at the top of this GitHub repository page and select **Download ZIP**.
2. Extract the downloaded ZIP file to a folder on your computer (e.g., `C:\Users\YourName\Documents\GitHub\Vinted-Image-Downloader`).

---

### Step 3: Open Command Prompt in the Project Folder
1. Open the folder where you extracted the files in Windows File Explorer.
2. Click on the address bar at the top of the File Explorer window.
3. Type `cmd` and press **Enter**.
4. A black Command Prompt window will open, navigated directly to your project folder.

---

### Step 4: Install Required Packages
You can install the required packages using the included `requirements.txt` file:

```cmd
python -m pip install -r requirements.txt
```

*Alternatively, you can install the packages directly:*

```cmd
python -m pip install playwright requests tqdm PyInstaller
```

> **Note:** You do **NOT** need to install additional browser drivers! The tool automatically connects to Google Chrome or Microsoft Edge already installed on your computer.

---

### Step 5: Option A — Run the Tool Directly via Python
You can start using the tool immediately without building an `.exe`:

```cmd
python vinted_image_downloader.py
```

- The script will prompt you to enter a Vinted user profile URL (e.g., `https://www.vinted.com/member/12345678-username`).
- Next, it will ask if you want to filter by category or keyword (e.g., `shoes`). Simply press **Enter** to download all items.

---

### Step 5: Option B — Build the Standalone `.exe` File
If you want to create a portable `.exe` file to share or run without opening Python:

1. In the same Command Prompt window, run:
   ```cmd
   python -m PyInstaller --onefile --name "VintedImageDownloader" vinted_image_downloader.py
   ```
2. Wait a minute for PyInstaller to finish building the executable.
3. Open the newly created `dist` folder inside your project directory.
4. You will find **`VintedImageDownloader.exe`** inside! You can move this file anywhere or share it.

---

## 💻 Advanced Command Line (CLI) Usage

For power users who prefer non-interactive automation or batch scripts:

```cmd
VintedImageDownloader.exe https://www.vinted.com/member/12345678-username -c "shoes" -o "MyDownloads" --headless
```

### Available Command Line Flags:
| Argument | Description | Example |
| :--- | :--- | :--- |
| `url` | Vinted profile URL | `https://www.vinted.com/member/...` |
| `-c`, `--category` | Filter by keyword, category, or brand | `-c "jackets"` |
| `-o`, `--output` | Custom download folder | `-o "C:\Pictures\Vinted"` |
| `--headless` | Run browser invisibly in background | `--headless` |

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