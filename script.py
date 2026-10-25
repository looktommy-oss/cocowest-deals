import os
import re
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
        content = soup.find('div', class_='entry-content') or soup.find('article')
        if not content:
            return []
        
        items = []
        for img in content.find_all('img'):
            src = img.get('src', '')
            if 'uploads' in src or 'wp-content' in src:
                filename = src.split('/')[-1].split('?')[0]
                if not any(k in filename.lower() for k in ['logo', 'avatar', 'header', 'banner', 'gravatar', 'icon']):
                    items.append({'filename': filename, 'src': src, 'alt': img.get('alt', 'Deal Image')})
        return items
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return []

def main():
    print("Scanning CocoWest homepage...")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    home_resp = requests.get('https://cocowest.ca/', headers=headers)
    soup = BeautifulSoup(home_resp.text, 'html.parser')
    
    # Locate post articles/links on homepage
    articles = soup.find_all(['article', 'div'], class_=re.compile(r'post|entry'))
    
    monday_url = None
    weekend_url = None
    
    for art in articles:
        link = art.find('a', href=True)
        if not link:
            continue
        href = link['href']
        text = art.get_text().lower()
        
        if 'weekend' in text or 'weekend' in href:
            if not weekend_url:
                weekend_url = href
        elif 'sale-items-for' in href or 'costco-sale-items' in href or 'monday' in text:
            if not monday_url:
                monday_url = href

    if not monday_url or not weekend_url:
        print(f"Post matching results -> Monday: {monday_url}, Weekend: {weekend_url}")
        print("Could not automatically locate both posts.")
        return

    print(f"Found Monday Post: {monday_url}")
    print(f"Found Weekend Post: {weekend_url}")

    monday_items = {item['filename'] for item in get_post_images(monday_url)}
    weekend_items = get_post_images(weekend_url)

    new_deals = [item for item in weekend_items if item['filename'] not in monday_items]
    print(f"Found {len(new_deals)} new weekend deals!")

    # Generate index.html
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
    else:
        print("Email credentials omitted or incomplete, skipping email digest.")

if __name__ == '__main__':
    main()
