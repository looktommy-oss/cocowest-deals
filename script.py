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
        print(f"Fetching {url} -> Status: {resp.status_code}")
        if resp.status_code != 200:
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        content = soup.find('div', class_=re.compile(r'entry-content|post-content')) or soup.find('article') or soup
        
        items = []
        seen_urls = set()

        def process_img(img_tag):
            alt = (img_tag.get('alt') or '').strip()
            
            # Scan ALL attributes of the image tag for any URL containing uploads or wp-content
            candidate_urls = []
            for attr_name, attr_val in img_tag.attrs.items():
                if isinstance(attr_val, str):
                    if 'uploads' in attr_val or 'wp-content' in attr_val:
                        candidate_urls.append(attr_val)
                elif isinstance(attr_val, list):
                    for val in attr_val:
                        if isinstance(val, str) and ('uploads' in val or 'wp-content' in val):
                            candidate_urls.append(val)
            
            for url_str in candidate_urls:
                # Handle comma-separated srcset values if present
                for part in url_str.split(','):
                    clean_url = part.strip().split(' ')[0].split('?')[0]
                    if ('uploads' in clean_url or 'wp-content' in clean_url) and not clean_url.endswith('.svg'):
                        filename = clean_url.split('/')[-1].lower()
                        rejected_keywords = ['logo', 'avatar', 'gravatar', 'header', 'banner', 'icon', 'badge', 'social', 'button', 'cocowest']
                        
                        if not any(k in filename for k in rejected_keywords):
                            if clean_url not in seen_urls:
                                seen_urls.add(clean_url)
                                # Prefer alt text if it describes the product, otherwise fall back to filename
                                identifier = alt if (alt and len(alt) > 3) else filename
                                items.append({
                                    'id': identifier,
                                    'src': clean_url,
                                    'alt': alt or 'Costco Deal'
                                })

        # 1. Process all img tags inside the article content
        for img in content.find_all('img'):
            process_img(img)

        # 2. Process all noscript tags (where WordPress lazy-load stores original images)
        for noscript in content.find_all('noscript'):
            ns_soup = BeautifulSoup(noscript.text, 'html.parser')
            for img in ns_soup.find_all('img'):
                process_img(img)

        print(f"Successfully extracted {len(items)} valid deal images from {url}")
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

    # Filter out items present on Monday
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
