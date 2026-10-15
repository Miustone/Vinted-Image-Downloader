import os
import re
import sys
import time
import argparse
import requests
from playwright.sync_api import sync_playwright
from tqdm import tqdm

def sanitize_folder_name(name):
    """Removes invalid characters for directory names."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def download_image(url, folder_path, filename):
    """Downloads a single image file."""
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            filepath = os.path.join(folder_path, filename)
            with open(filepath, "wb") as f:
                f.write(response.content)
            return True
    except Exception as e:
        tqdm.write(f"   [!] Error downloading ({url}): {e}")
    return False

def matches_category(item, category_filter):
    """Checks if the keyword matches title, brand, or category data."""
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

def download_vinted_user(profile_url, output_dir="downloads", category_filter=None, headless=False):
    captured_items = {}

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

        # Catch ANY JSON response containing an "items" array from Vinted API
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

        # Accept Cookie Banner if present
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
                # Automatisches geschmeidiges Scrollen nach unten
                page.evaluate("window.scrollBy(0, 800);")
                time.sleep(0.8)

                # Gelegentlich ganz nach unten springen, um Vinted's Infinite Scroll Trigger auszulösen
                pbar.n = len(captured_items)
                pbar.refresh()

                current_count = len(captured_items)
                if current_count == last_item_count:
                    no_new_items_attempts += 1
                    # Wenn kurz keine neuen Daten kommen, einen tiefen Scroll bis zum Seitenende erzwingen
                    if no_new_items_attempts % 5 == 0:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        time.sleep(2.5)
                    
                    # Erst wenn nach 15 Versuchen absolut keine neuen Artikel mehr dazukommen, beenden
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

    # --- APPLY CATEGORY / KEYWORD FILTER ---
    all_items = list(captured_items.values())
    if category_filter:
        filtered_items = [item for item in all_items if matches_category(item, category_filter)]
        print(f"[*] Filter active: '{category_filter}' -> Matched {len(filtered_items)} of {len(all_items)} items.")
    else:
        filtered_items = all_items

    if not filtered_items:
        print("[!] No items matched your category filter.")
        return

    # Extract username/ID for folder structure
    username_match = re.search(r'/member/(.+)', profile_url)
    username = username_match.group(1) if username_match else "vinted_user"

    # --- DOWNLOAD IMAGES ---
    for item in tqdm(filtered_items, desc="[2/2] Downloading images", unit=" items"):
        item_id = item.get("id")
        title = sanitize_folder_name(item.get("title", f"item_{item_id}"))
        item_folder = os.path.join(output_dir, username, f"{item_id}_{title}")
        os.makedirs(item_folder, exist_ok=True)

        photos = item.get("photos", [])
        for idx, photo in enumerate(photos):
            img_url = photo.get("full_size_url") or photo.get("url")
            if img_url:
                download_image(img_url, item_folder, f"image_{idx + 1}.jpg")

    print(f"\n[✔] Done! All images saved to: '{os.path.abspath(output_dir)}'")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vinted Bulk Image Downloader with Category Filter")
    parser.add_argument("url", nargs="?", help="URL of the Vinted user profile")
    parser.add_argument("-c", "--category", default=None, help="Filter by category or keyword")
    parser.add_argument("-o", "--output", default="downloads", help="Output directory")
    parser.add_argument("--headless", action="store_true", help="Run browser in background")

    args = parser.parse_args()

    try:
        url = args.url
        if not url:
            url = input("Enter Vinted profile URL: ").strip()

        category = args.category
        if not category and len(sys.argv) <= 1:
            cat_input = input("Filter by category/keyword (Press ENTER for all items): ").strip()
            if cat_input:
                category = cat_input

        if url:
            download_vinted_user(url, output_dir=args.output, category_filter=category, headless=args.headless)
        else:
            print("[!] No URL provided.")

    except Exception as e:
        print(f"\n[!] An error occurred during execution: {e}")

    finally:
        input("\nPress ENTER to exit...")