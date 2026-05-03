"""Petit utilitaire pour trouver l'URL de connexion de jle.com.

Lancez :  python find_login.py
puis copiez-moi la sortie.
"""
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0 Safari/537.36"
TARGETS = [
    "https://www.jle.com/fr",
    "https://www.jle.com/fr/revues/odf/numero.phtml",
]
PATTERN = re.compile(r"(login|connexion|connect|compte|sign[-_ ]?in|authent)", re.I)

s = requests.Session()
s.headers["User-Agent"] = UA

for url in TARGETS:
    print(f"\n=== {url} ===")
    try:
        r = s.get(url, timeout=30)
    except Exception as e:
        print("  erreur :", e)
        continue
    print(f"  HTTP {r.status_code}  ({len(r.content)} octets)")
    soup = BeautifulSoup(r.text, "lxml")
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = a["href"]
        if PATTERN.search(text) or PATTERN.search(href):
            print(f"  - {text!r:40s} -> {urljoin(url, href)}")
    for f in soup.find_all("form"):
        action = f.get("action") or "(vide)"
        method = (f.get("method") or "GET").upper()
        names = [i.get("name") for i in f.find_all("input") if i.get("name")]
        print(f"  FORM {method} action={action!r}  inputs={names}")
