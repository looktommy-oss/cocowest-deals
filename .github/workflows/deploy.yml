name: Update CocoWest New Deals

on:
  schedule:
    - cron: '0 17 * * 5,6'
  workflow_dispatch:
    inputs:
      force_notify:
        description: "Send the email even if this weekend post was already announced"
        type: boolean
        default: false

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: pip install cloudscraper beautifulsoup4

      - name: Run scraper script
        env:
          MAIL_USERNAME: ${{ secrets.MAIL_USERNAME }}
          MAIL_PASSWORD: ${{ secrets.MAIL_PASSWORD }}
          MAIL_TO: ${{ secrets.MAIL_TO }}
          FORCE_NOTIFY: ${{ inputs.force_notify && '1' || '0' }}
        run: python -u script.py

      - name: Verify output exists
        run: |
          if [ ! -f public/index.html ]; then
            echo "ERROR: public/index.html was not created by script.py"
            echo "Files in repo root:"
            ls -la
            echo "Last lines of script.py:"
            tail -n 5 script.py
            exit 1
          fi
          ls -la public

      - name: Deploy to GitHub Pages
        uses: peaceiris/actions-gh-pages@v4
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./public
          publish_branch: gh-pages
          force_orphan: true
