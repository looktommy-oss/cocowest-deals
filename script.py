import os
import re
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def get_post_images(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"Failed to fetch {url}, status code: {resp.status_code}")
            return []
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Get post content container
        content = soup.find('div', class_=re.compile(r'entry-content|post-content')) or soup.find('article') or soup
        
        items = []
        for img in content.find_all('img'):
            src = img.get('src', '') or img.get('data-src', '') or img.get('data-lazy-src', '')
            if 'uploads' in src or 'wp-content' in src:
                filename = src.split('/')[-1].split('?')[0]
                # Filter out standard UI elements
                if not any(k in filename.lower() for k in ['logo', 'avatar', 'header', 'banner', 'gravatar', 'icon', 'button', 'badge']):
                    items.append({'filename': filename, 'src': src, 'alt': img.get('alt', 'Deal Image')})
        return items
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return []

def main():
    print("Scraping CocoWest homepage...")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    
    # 1. Fetch homepage to get exact URLs
    home_url = 'https://cocowest.ca/'
    resp = requests.get(home_url, headers=headers, timeout=15)
    soup = BeautifulSoup(resp.text, 'html.parser')
    
    monday_url = None
    weekend_url = None

    # Scan all links on homepage
    for a in soup.find_all('a', href=True):
        href = a['href']
        href_lower = href.lower()

        # Identify Weekend Post
        if 'weekend-update' in href_lower or ('weekend' in href_lower and 'costco-sale-items' in href_lower):
            if not weekend_url:
                weekend_url = href

        # Identify Monday Post (Contains "costco-flyer" or "october-19-25" / "monday")
        elif ('costco-flyer' in href_lower or 'monday' in href_lower) and 'weekend' not in href_lower:
            if not monday_url:
                monday_url = href

    # Fallback to direct URLs if homepage layout shifts
    if not weekend_url:
        weekend_url = "https://cocowest.ca/2026/10/weekend-update-costco-sale-items-for-october-23-25-2026-for-bc-ab-mb-sk/"
    if not monday_url:
        monday_url = "https://cocowest.ca/2026/10/costco-flyer-costco-sale-items-for-october-19-25-2026-2026-for-bc-ab-sk-mb/"

    print(f"Target Monday Post:  {monday_url}")
    print(f"Target Weekend Post: {weekend_url}")

    monday_items = get_post_images(monday_url)
    weekend_items = get_post_images(weekend_url)

    monday_filenames = {item['filename'] for item in monday_items}
    
    print(f"Found {len(monday_items)} images on Monday post.")
    print(f"Found {len(weekend_items)} images on Weekend post.")

    # Compare image filenames
    new_deals = [item for item in weekend_items if item['filename'] not in monday_filenames]
    print(f"Found {len(new_deals)} exclusive weekend deals!")

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

    print("Successfully generated index.html!")

    # Optional Email Digest
    sender_email = os.environ.get("MAIL_USERNAME")
    receiver_email = os.environ.get("MAIL_TO")
    app_password = os.environ.get("MAIL_PASSWORD")

    if sender_email and receiver_email and app_password:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f"🛒 {len(new_deals)} New CocoWest Weekend Deals!"
        msg['From'] = sender_email
        msg['To'] = receiver_email
        msg.attach(MIMEText(html_page, 'html'))

        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(sender_email, app_password)
                server.sendmail(sender_email, receiver_email, msg.as_string())
            print("Successfully sent email digest!")
        except Exception as e:
            print(f"Failed to send email: {e}")

if __name__ == '__main__':
    main()
