import os
import smtplib
import requests
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def get_post_images(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, 'html.parser')
        content = soup.find('div', class_='entry-content') or soup.find('article') or soup
        
        items = []
        for img in content.find_all('img'):
            src = img.get('src', '') or img.get('data-src', '')
            if 'uploads' in src or 'wp-content' in src:
                filename = src.split('/')[-1].split('?')[0]
                if not any(k in filename.lower() for k in ['logo', 'avatar', 'header', 'banner', 'gravatar', 'icon']):
                    items.append({'filename': filename, 'src': src, 'alt': img.get('alt', 'Deal Image')})
        return items
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return []

def main():
    print("Fetching CocoWest RSS feed...")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    feed_url = 'https://cocowest.ca/feed/'
    resp = requests.get(feed_url, headers=headers, timeout=15)
    
    soup = BeautifulSoup(resp.text, 'xml')
    items = soup.find_all('item')
    
    monday_url = None
    weekend_url = None
    
    for item in items:
        title = item.find('title').get_text().lower() if item.find('title') else ''
        link = item.find('link').get_text().strip() if item.find('link') else ''
        
        if 'weekend' in title or 'weekend' in link:
            if not weekend_url:
                weekend_url = link
        elif 'sale items' in title or 'costco' in title:
            if not monday_url and 'weekend' not in title and 'weekend' not in link:
                monday_url = link

    print(f"Parsed Monday URL: {monday_url}")
    print(f"Parsed Weekend URL: {weekend_url}")

    if not monday_url or not weekend_url:
        print("Could not locate both posts from feed.")
        return

    monday_items = {item['filename'] for item in get_post_images(monday_url)}
    weekend_items = get_post_images(weekend_url)

    print(f"Found {len(monday_items)} images on Monday post.")
    print(f"Found {len(weekend_items)} images on Weekend post.")

    new_deals = [item for item in weekend_items if item['filename'] not in monday_items]
    print(f"Found {len(new_deals)} new weekend deals!")

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

    print("Successfully wrote index.html!")

    # Email Digest
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
