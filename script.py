import os
import re
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def get_post_items(url):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"Failed to fetch {url}, status: {resp.status_code}")
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        content = soup.find('div', class_=re.compile(r'entry-content|post-content')) or soup.find('article') or soup
        
        items = []
        images = content.find_all('img')
        
        for img in images:
            alt = (img.get('alt') or '').strip()
            
            # Check every possible lazy-loading image attribute
            src = (
                img.get('src') or 
                img.get('data-src') or 
                img.get('data-lazy-src') or 
                img.get('data-orig-file') or ''
            )
            
            if not src and img.get('srcset'):
                src = img.get('srcset').split(',')[0].split(' ')[0]
            if not src and img.get('data-srcset'):
                src = img.get('data-srcset').split(',')[0].split(' ')[0]

            if src and ('uploads' in src or 'wp-content' in src):
                filename = src.split('/')[-1].split('?')[0]
                identifier = alt if alt else filename
                
                if identifier and not any(k in identifier.lower() for k in ['logo', 'avatar', 'gravatar', 'header', 'banner', 'icon', 'badge']):
                    items.append({
                        'id': identifier,
                        'src': src,
                        'alt': alt or 'Costco Deal'
                    })

        # Regex fallback if BeautifulSoup missed lazy-loaded images
        if not items:
            raw_urls = re.findall(r'https?://[^\s"\']+\.(?:jpg|jpeg|png|webp)', resp.text)
            for raw in raw_urls:
                if 'uploads' in raw or 'wp-content' in raw:
                    fname = raw.split('/')[-1].split('?')[0]
                    if not any(k in fname.lower() for k in ['logo', 'avatar', 'gravatar', 'header', 'banner', 'icon', 'badge']):
                        items.append({'id': fname, 'src': raw, 'alt': 'Costco Deal'})

        return items
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return []

def main():
    print("Fetching CocoWest homepage to locate posts...")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
    
    monday_url = None
    weekend_url = None
    
    try:
        home_resp = requests.get('https://cocowest.ca/', headers=headers, timeout=15)
        home_soup = BeautifulSoup(home_resp.text, 'html.parser')
        
        for a in home_soup.find_all('a', href=True):
            text = (a.get_text() or '').lower()
            href = (a['href'] or '').lower()
            
            if ('sale items for' in text or 'monday' in text or 'sale-items-for' in href or 'costco-flyer' in href) \
               and 'weekend' not in text and 'weekend' not in href and 'fan-blog' not in text:
                if not monday_url:
                    monday_url = a['href']
            elif 'weekend' in text or 'weekend' in href:
                if not weekend_url:
                    weekend_url = a['href']
    except Exception as e:
        print(f"Homepage fetch failed: {e}")

    # Fallbacks for current active post URLs
    if not monday_url:
        monday_url = "https://cocowest.ca/2026/10/costco-flyer-costco-sale-items-for-october-19-25-2026-2026-for-bc-ab-sk-mb/"
    if not weekend_url:
        weekend_url = "https://cocowest.ca/2026/10/weekend-update-costco-sale-items-for-october-23-25-2026-for-bc-ab-mb-sk/"

    print(f"Target Monday Post:  {monday_url}")
    print(f"Target Weekend Post: {weekend_url}")

    monday_data = get_post_items(monday_url)
    weekend_data = get_post_items(weekend_url)

    print(f"Extracted {len(monday_data)} images from Monday post.")
    print(f"Extracted {len(weekend_data)} images from Weekend post.")

    monday_ids = {item['id'] for item in monday_data}

    # Filter out items present on Monday
    new_deals = []
    seen = set()
    for item in weekend_data:
        if item['id'] not in monday_ids and item['id'] not in seen:
            new_deals.append(item)
            seen.add(item['id'])

    print(f"Found {len(new_deals)} exclusive new weekend deals!")

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
