import os
import re
import sys
import time
import argparse
import requests
import ctypes
from datetime import datetime
from playwright.sync_api import sync_playwright
from tqdm import tqdm

def sanitize_folder_name(name):
    """Removes invalid characters and line breaks for directory names."""
    if not name:
        return "unnamed_item"
    name = re.sub(r'[\r\n\t]+', ' ', name)
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip()

def parse_item_date(item):
    """
    Deeply searches Vinted item metadata for creation timestamps.
    Falls back to photo creation date or ID estimation if omitted.
    """
    ts = None

    # 1. Direct item creation / update timestamps
    for key in ["created_at_ts", "created_at", "updated_at_ts", "user_updated_at_ts"]:
        if item.get(key):
            ts = item.get(key)
            break

    # 2. Check nested photo object creation timestamps
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

    # Convert retrieved timestamp to datetime object
    dt = None
    if isinstance(ts, (int, float)):
        if ts > 10000000000:  # Convert milliseconds to seconds
            ts = ts / 1000
        dt = datetime.fromtimestamp(ts)
    elif isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            pass

    if not dt:
        dt = datetime.now()

    date_str = dt.strftime("%Y-%m-%d")
    return dt, date_str

def set_file_creation_time(filepath, dt):
    """Sets both modification and creation time for files under Windows."""
    try:
        timestamp = dt.timestamp()
        # Set Modification and Access time
        os.utime(filepath, (timestamp, timestamp))

        # Set Windows Creation Time via ctypes if running on Windows
        if os.name == 'nt':
            try:
                # Windows File Creation Time handle
                from ctypes import wintypes
                FILE_WRITE_ATTRIBUTES = 0x0100
                OPEN_EXISTING = 3
                
                # Convert timestamp to Windows FILETIME (100-nanosecond intervals since Jan 1, 1601)
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
    except Exception as e:
        pass

def download_image(url, folder_path, filename, item_datetime=None):
    """Downloads a single image file and sets its timestamps."""
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            filepath = os.path.join(folder_path, filename)
            with open(filepath, "wb") as f:
                f.write(response.content)
            
            if item_datetime:
                set_file_creation_time(filepath, item_datetime)
            return True
    except Exception as e:
        tqdm.write(f"   [!] Error downloading ({url}): {e}")
    return False

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
                    user_id = u.get("id")
                    login = u.get("login")
                    full_url = f"https://www.{domain}/member/{user_id}-{login}"
                    print(f"[*] Match found: {full_url}")
                    return full_url
            
            if users:
                user_id = users[0].get("id")
                login = users[0].get("login")
                full_url = f"https://www.{domain}/member/{user_id}-{login}"
                print(f"[*] Using closest match: {full_url}")
                return full_url

    except Exception as e:
        print(f"[!] API user lookup failed ({e}). Falling back to URL construction.")

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
        clean_name = re.sub(r'^\d+-', '', raw_name)
        return sanitize_folder_name(clean_name)

    return "vinted_user"

def matches_category(item, category_filter):
    if not category_filter:
        return True

    filter_lower = category_filter.lower().strip()
    title = str(item.get("title", "")).lower()
    brand = str(item.get("brand_title", "")).lower()
    
    category_data = item.get("category", {})
    category_title = ""
    if isinstance(category_data, dict):
        category_title = str(category_data.get("title", "")).lower()
    
    return (filter_lower in title) or (filter_lower in category_title) or (filter_lower in brand)

def download_vinted_user(user_input, output_dir="downloads", category_filter=None, headless=False):
    profile_url = resolve_user_to_url(user_input)
    captured_items = {}
    page_title = ""

    with sync_playwright() as p:
        print("[*] Launching installed browser (Chrome/Edge)...")
        browser = None
        for channel in ["chrome", "msedge"]:
            try:
                browser = p.chromium.launch(headless=headless, channel=channel)
                print(f"[*] Using installed browser: {channel.upper()}")
                break
            except Exception:
                continue

        if not browser:
            print("[*] Launching default browser...")
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

        print(f"[*] Opening profile: {profile_url}")
        page.goto(profile_url, wait_until="networkidle")
        page_title = page.title()

        try:
            page.click("#onetrust-accept-btn-handler", timeout=3000)
            print("[*] Cookie banner accepted.")
        except Exception:
            pass

        print("[*] Scrolling page automatically to load all items...")
        last_item_count = 0
        no_new_items_attempts = 0

        with tqdm(desc="[1/2] Fetching items", unit=" items", leave=True) as pbar:
            while True:
                page.evaluate("window.scrollBy(0, 800);")
                time.sleep(0.8)

                pbar.n = len(captured_items)
                pbar.refresh()

                current_count = len(captured_items)
                if current_count == last_item_count:
                    no_new_items_attempts += 1
                    if no_new_items_attempts % 5 == 0:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        time.sleep(2.5)
                    
                    if no_new_items_attempts >= 15:
                        break
                else:
                    no_new_items_attempts = 0
                    last_item_count = current_count

        print(f"\n[+] Total items found on profile: {len(captured_items)}")
        browser.close()

    if not captured_items:
        print("[!] No items found on this profile.")
        return

    all_items = list(captured_items.values())
    if category_filter:
        filtered_items = [item for item in all_items if matches_category(item, category_filter)]
        print(f"[*] Filter active: '{category_filter}' -> Matched {len(filtered_items)} of {len(all_items)} items.")
    else:
        filtered_items = all_items

    if not filtered_items:
        print("[!] No items matched your category filter.")
        return

    clean_username = extract_clean_username(profile_url, page_title, captured_items)
    print(f"[*] Downloading content for user: '{clean_username}'")

    for item in tqdm(filtered_items, desc="[2/2] Downloading images", unit=" items"):
        item_id = item.get("id")
        dt_obj, date_str = parse_item_date(item)
        
        title = sanitize_folder_name(item.get("title", f"item_{item_id}"))
        
        folder_name = f"{date_str}_{item_id}_{title}"
        item_folder = os.path.join(output_dir, clean_username, folder_name)
        os.makedirs(item_folder, exist_ok=True)

        photos = item.get("photos", [])
        for idx, photo in enumerate(photos):
            img_url = photo.get("full_size_url") or photo.get("url")
            if img_url:
                download_image(img_url, item_folder, f"image_{idx + 1}.jpg", item_datetime=dt_obj)

    print(f"\n[✔] Done! All images saved to: '{os.path.abspath(os.path.join(output_dir, clean_username))}'")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vinted Image Downloader - Bulk Image Downloader with Category Filter")
    parser.add_argument("user", nargs="?", help="URL or Username/ID of the Vinted profile")
    parser.add_argument("-c", "--category", default=None, help="Filter by category or keyword")
    parser.add_argument("-o", "--output", default="downloads", help="Output directory")
    parser.add_argument("--headless", action="store_true", help="Run browser in background")

    args = parser.parse_args()

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