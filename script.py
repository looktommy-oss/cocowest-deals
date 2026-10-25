import os
import re
import smtplib
import cloudscraper
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def get_post_items(url):
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )
    try:
        resp = scraper.get(url, timeout=20)
        print(f"Fetching {url} -> Status: {resp.status_code}, Length: {len(resp.text)} bytes")
        if resp.status_code != 200:
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Let's check all img tags on the entire page
        all_imgs = soup.find_all('img')
        print(f"Total <img> tags found on page: {len(all_imgs)}")
        
        items = []
        seen_urls = set()

        for i, img in enumerate(all_imgs):
            alt = (img.get('alt') or '').strip()
            src = (
                img.get('data-src') or 
                img.get('data-lazy-src') or 
                img.get('data-orig-file') or 
                img.get('src') or ''
            )
            if not src or 'data:image' in src:
                srcset = img.get('srcset') or img.get('data-srcset') or ''
                if srcset:
                    src = srcset.split(',')[0].split(' ')[0]

            if not src:
                continue

            clean_src = src.split('?')[0]
            filename = clean_src.split('/')[-1].lower()

            # Print first 5 images to inspect their structure
            if i < 5:
                print(f"  [Img {i}] src: {clean_src} | alt: {alt}")

            if ('uploads' in clean_src or 'wp-content' in clean_src) and not clean_src.endswith('.svg'):
                # Check filter keywords
                rejected_keywords = ['logo', 'avatar', 'gravatar', 'header', 'banner', 'icon', 'badge', 'social', 'button']
                matched_keyword = next((k for k in rejected_keywords if k in filename), None)
                
                if matched_keyword:
                    # print(f"    -> Rejected by keyword '{matched_keyword}': {filename}")
                    continue

                if clean_src not in seen_urls:
                    seen_urls.add(clean_src)
                    items.append({
                        'id': alt if alt else filename,
                        'src': clean_src,
                        'alt': alt or 'Costco Deal'
                    })

        print(f"Successfully extracted {len(items)} valid deal images after filtering.")
        return items
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return []

def main():
    scraper = cloudscraper.create_scraper()
    
    monday_url = "https://cocowest.ca/2026/10/costco-flyer-costco-sale-items-for-october-19-25-2026-2026-for-bc-ab-sk-mb/"
    weekend_url = "https://cocowest.ca/2026/10/weekend-update-costco-sale-items-for-october-23-25-2026-for-bc-ab-mb-sk/"

    print(f"Target Monday Post:  {monday_url}")
    print(f"Target Weekend Post: {weekend_url}")

    monday_data = get_post_items(monday_url)
    weekend_data = get_post_items(weekend_url)

    monday_ids = {item['id'] for item in monday_data}

    new_deals = []
    seen = set()
    for item in weekend_data:
        if item['id'] not in monday_ids and item['id'] not in seen:
            new_deals.append(item)
            seen.add(item['id'])

    print(f"🎉 Found {len(new_deals)} exclusive new weekend deals!")

    # Build HTML Page
    html_page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>CocoWest New Weekend Deals</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f4f6f8; margin: 0; padding: 20px; color: #333; }}
        .container {{ max-width: 800px; margin: auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); }}
        h1 {{ color: #28a745; text-align: center; margin-bottom: 5px; }}
        p.subtitle {{ text-align: center; color: #666; margin-top: 0; margin-bottom: 30px; }}
        .deal-card {{ margin-bottom: 30px; border: 1px solid #e1e4e8; border-radius: 8px; padding: 15px; background: #fafbfc; text-align: center; }}
        .deal-card img {{ max-width: 100%; height: auto; border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🆕 New CocoWest Weekend Deals</h1>
        <p class="subtitle">Showing {len(new_deals)} exclusive items not featured on Monday.</p>
"""
    for deal in new_deals:
        html_page += f"""
        <div class="deal-card">
            <img src="{deal['src']}" alt="{deal['alt']}" loading="lazy">
        </div>
        """
        
    html_page += """
    </div>
</body>
</html>
"""

    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html_page)

    print("Successfully created index.html!")

if __name__ == '__main__':
    main()
