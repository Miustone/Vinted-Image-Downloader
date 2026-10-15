# Vinted Image Downloader 📸

An easy-to-use bulk image downloading tool for Vinted user profiles, built with Python and Playwright. 

Similar to tools like `fansly-downloader-ng`, it automatically bypasses Cloudflare protections, scroll-loads a selected Vinted user's entire closet, filters items by category or keyword, and downloads all item images in full resolution using your locally installed Google Chrome or Microsoft Edge browser.

---

## ✨ Features

- **High-Resolution Downloads:** Saves full-size original photos directly from Vinted's CDN.
- **Anti-Bot Protection Bypass:** Uses locally installed browsers (Chrome/Edge) to seamlessly handle Cloudflare and session tokens.
- **Category & Keyword Filtering:** Option to download only specific items (e.g., `shoes`, `jackets`, `Nike`).
- **Progress Bars:** Clean console progress indicators showing scrolling and download progress using `tqdm`.
- **Cross-Domain Support:** Works across international Vinted domains (`.de`, `.com`, `.fr`, `.co.uk`, etc.).
- **Standalone Binary:** Can be bundled into a portable single-file `.exe` for Windows.

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

## 📜 License
This project is licensed under the [MIT License](LICENSE).

---

## ⚠️ Disclaimer
This tool is for educational and personal archival purposes only. Please respect Vinted's Terms of Service and rate limits.