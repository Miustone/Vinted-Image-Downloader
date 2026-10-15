import os
import re
import sys
import time
import json
import argparse
import requests
import ctypes
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright
from tqdm import tqdm

# Tkinter import for GUI mode
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False

def sanitize_folder_name(name):
    """Removes invalid characters and line breaks for directory names."""
    if not name:
        return "unnamed_item"
    name = re.sub(r'[\r\n\t]+', ' ', str(name))
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip()

def parse_item_date(item):
    """Deeply searches Vinted item metadata for creation timestamps."""
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
        dt = datetime.fromtimestamp(ts)
    elif isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            pass

    if not dt:
        dt = datetime.now()

    return dt, dt.strftime("%Y-%m-%d")

def set_file_creation_time(filepath, dt):
    """Sets both modification and creation time for files under Windows."""
    try:
        timestamp = dt.timestamp()
        os.utime(filepath, (timestamp, timestamp))

        if os.name == 'nt':
            try:
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
    except Exception:
        pass

def get_highest_res_url(photo_dict):
    """Extracts or constructs the highest resolution URL available for a photo."""
    url = (
        photo_dict.get("full_size_url") or 
        photo_dict.get("high_resolution", {}).get("url") or 
        photo_dict.get("url")
    )
    if not url:
        return None
    
    url = re.sub(r'/(t\d+|f\d+|thumb)/', '/f800/', url)
    return url

def download_single_image(args):
    """Worker function for concurrent image downloads."""
    url, folder_path, filename, item_datetime = args
    filepath = os.path.join(folder_path, filename)
    
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        return True

    for attempt in range(3):
        try:
            response = requests.get(url, timeout=12)
            if response.status_code == 200:
                with open(filepath, "wb") as f:
                    f.write(response.content)
                if item_datetime:
                    set_file_creation_time(filepath, item_datetime)
                return True
        except Exception:
            time.sleep(1)
    return False

def save_item_metadata_json(item, folder_path, clean_username):
    """Saves item details into a structured info.json sidecar file."""
    json_path = os.path.join(folder_path, "info.json")
    
    price = item.get("price") or item.get("total_item_price")
    currency = item.get("currency", "EUR")
    if isinstance(price, dict):
        currency = price.get("currency_code", currency)
        price = price.get("amount")

    brand = item.get("brand_title") or item.get("brand", {}).get("title") if isinstance(item.get("brand"), dict) else item.get("brand")
    category = item.get("category", {}).get("title") if isinstance(item.get("category"), dict) else item.get("category")

    meta = {
        "id": item.get("id"),
        "title": item.get("title"),
        "description": item.get("description"),
        "price": price,
        "currency": currency,
        "brand": brand,
        "category": category,
        "size": item.get("size_title") or item.get("size"),
        "status": item.get("status"),
        "url": item.get("url") or f"https://www.vinted.de/items/{item.get('id')}",
        "user": clean_username,
        "created_at": item.get("created_at_ts") or item.get("created_at"),
        "favourite_count": item.get("favourite_count", 0),
        "view_count": item.get("view_count", 0)
    }

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

def resolve_user_to_url(user_input, domain="vinted.de"):
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

def extract_clean_username(profile_url, page_title, captured_items):
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

def matches_category(item, category_filter):
    if not category_filter:
        return True

    filter_lower = category_filter.lower().strip()
    title = str(item.get("title", "")).lower()
    brand = str(item.get("brand_title", "")).lower()
    
    category_data = item.get("category", {})
    category_title = str(category_data.get("title", "")).lower() if isinstance(category_data, dict) else ""
    
    return (filter_lower in title) or (filter_lower in category_title) or (filter_lower in brand)

def download_vinted_user(user_input, output_dir="downloads", category_filter=None, headless=False, max_workers=6, status_callback=None):
    def log(msg):
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
    log(f"[*] Downloading content for user: '{clean_username}'")

    download_tasks = []
    skipped_count = 0

    for item in filtered_items:
        item_id = item.get("id")
        dt_obj, date_str = parse_item_date(item)
        title = sanitize_folder_name(item.get("title", f"item_{item_id}"))
        
        folder_name = f"{date_str}_{item_id}_{title}"
        item_folder = os.path.join(output_dir, clean_username, folder_name)
        os.makedirs(item_folder, exist_ok=True)

        save_item_metadata_json(item, item_folder, clean_username)

        photos = item.get("photos", [])
        for idx, photo in enumerate(photos):
            img_url = get_highest_res_url(photo)
            if img_url:
                filename = f"image_{idx + 1}.jpg"
                filepath = os.path.join(item_folder, filename)
                
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    skipped_count += 1
                else:
                    download_tasks.append((img_url, item_folder, filename, dt_obj))

    if skipped_count > 0:
        log(f"[*] Skipping {skipped_count} images that were already downloaded.")

    if download_tasks:
        log(f"[*] Downloading {len(download_tasks)} images using {max_workers} threads...")
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

    log(f"\n[✔] Done! All files saved to: '{os.path.abspath(os.path.join(output_dir, clean_username))}'")

# --- MINIMALIST BLACK & WHITE GUI ---
def launch_gui():
    if not TKINTER_AVAILABLE:
        print("[!] Tkinter is not available on this system.")
        return

    root = tk.Tk()
    root.title("Vinted Image Downloader")
    root.geometry("560x490")
    root.resizable(False, False)
    root.configure(bg="#121212")

    style = ttk.Style()
    style.theme_use("clam")

    # Black & White Palette
    BG_DARK = "#121212"
    PANEL_DARK = "#1e1e1e"
    ENTRY_BG = "#2a2a2a"
    TEXT_WHITE = "#ffffff"
    TEXT_MUTED = "#888888"

    style.configure("TFrame", background=BG_DARK)
    style.configure("TLabel", background=BG_DARK, foreground=TEXT_WHITE, font=("Segoe UI", 9))
    style.configure("Muted.TLabel", background=BG_DARK, foreground=TEXT_MUTED, font=("Segoe UI", 8))
    style.configure("Header.TLabel", background=BG_DARK, foreground=TEXT_WHITE, font=("Segoe UI", 13, "bold"))
    
    style.configure("TEntry", fieldbackground=ENTRY_BG, foreground=TEXT_WHITE, insertcolor=TEXT_WHITE, borderwidth=0)
    
    # White Button with Black Text
    style.configure("Main.TButton", background="#ffffff", foreground="#000000", font=("Segoe UI", 9, "bold"), borderwidth=0)
    style.map("Main.TButton", background=[("active", "#e0e0e0")])

    # Secondary Dark Button for Browse
    style.configure("Sec.TButton", background="#2d2d2d", foreground="#ffffff", font=("Segoe UI", 9), borderwidth=0)
    style.map("Sec.TButton", background=[("active", "#3d3d3d")])

    frame = ttk.Frame(root, padding="20")
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame, text="VINTED IMAGE DOWNLOADER", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 15))

    ttk.Label(frame, text="Vinted Profile URL or Username:").pack(anchor=tk.W, pady=(0, 2))
    user_entry = ttk.Entry(frame, width=65)
    user_entry.pack(fill=tk.X, pady=(0, 10))

    ttk.Label(frame, text="Filter by Category / Keyword (Optional):").pack(anchor=tk.W, pady=(0, 2))
    cat_entry = ttk.Entry(frame, width=65)
    cat_entry.pack(fill=tk.X, pady=(0, 10))

    ttk.Label(frame, text="Output Directory:").pack(anchor=tk.W, pady=(0, 2))
    dir_frame = ttk.Frame(frame)
    dir_frame.pack(fill=tk.X, pady=(0, 15))
    
    dir_entry = ttk.Entry(dir_frame, width=50)
    dir_entry.insert(0, os.path.abspath("downloads"))
    dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def browse_dir():
        chosen = filedialog.askdirectory()
        if chosen:
            dir_entry.delete(0, tk.END)
            dir_entry.insert(0, chosen)

    ttk.Button(dir_frame, text="Browse...", style="Sec.TButton", command=browse_dir).pack(side=tk.RIGHT, padx=(5, 0))

    # Console Output Frame
    log_text = tk.Text(frame, height=10, width=60, bg="#000000", fg="#ffffff", insertbackground="white", font=("Consolas", 8), borderwidth=1, relief="solid")
    log_text.pack(fill=tk.BOTH, pady=(0, 15))

    def update_log(msg):
        log_text.config(state=tk.NORMAL)
        log_text.insert(tk.END, msg + "\n")
        log_text.see(tk.END)
        log_text.config(state=tk.DISABLED)
        root.update_idletasks()

    def start_download():
        user_val = user_entry.get().strip()
        if not user_val:
            messagebox.showerror("Error", "Please enter a Vinted profile or username!")
            return

        btn_start.config(state=tk.DISABLED)
        log_text.config(state=tk.NORMAL)
        log_text.delete(1.0, tk.END)
        log_text.config(state=tk.DISABLED)

        import threading
        def run_thread():
            try:
                download_vinted_user(
                    user_input=user_val,
                    output_dir=dir_entry.get().strip(),
                    category_filter=cat_entry.get().strip() or None,
                    headless=True,
                    status_callback=update_log
                )
                messagebox.showinfo("Success", "Download completed successfully!")
            except Exception as ex:
                update_log(f"\n[!] Error: {ex}")
                messagebox.showerror("Error", str(ex))
            finally:
                btn_start.config(state=tk.NORMAL)

        threading.Thread(target=run_thread, daemon=True).start()

    btn_start = ttk.Button(frame, text="START DOWNLOAD", style="Main.TButton", command=start_download)
    btn_start.pack(fill=tk.X, ipady=6)

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