#!/usr/bin/env python3
"""Télécharge les PDFs de la revue ODF (jle.com) triés par année / numéro / date.

Usage rapide :

    pip install requests beautifulsoup4 lxml
    export JLE_USER='votre_login'
    export JLE_PASS='votre_motdepasse'
    python download_odf.py

Options utiles :

    --out downloads          dossier racine de sortie
    --delay 1.0              délai (s) entre requêtes pour être poli
    --year 2023              ne traite qu'une année
    --dry-run                liste sans télécharger
    --verbose                logs détaillés

Si jle.com modifie son formulaire de connexion, ajustez LOGIN_URL et le
dictionnaire de payload dans `login()`.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.jle.com"
INDEX_URL = f"{BASE}/fr/revues/odf/numero.phtml"
LOGIN_URL = f"{BASE}/fr/login"  # à ajuster si besoin

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)

log = logging.getLogger("odf")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        }
    )
    return s


def login(session: requests.Session, user: str, password: str) -> None:
    """Authentifie la session. Adaptez les noms de champs si jle.com change."""
    log.info("Récupération du formulaire de connexion…")
    r = session.get(LOGIN_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    form = soup.find("form")
    if form is None:
        raise RuntimeError(
            f"Aucun formulaire trouvé sur {LOGIN_URL}. "
            "Inspectez la page et ajustez LOGIN_URL / login() en conséquence."
        )

    action = urljoin(LOGIN_URL, form.get("action") or LOGIN_URL)
    payload: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        payload[name] = inp.get("value", "")

    # heuristique : champs habituels
    for k in list(payload):
        kl = k.lower()
        if any(t in kl for t in ("login", "user", "email", "mail")):
            payload[k] = user
        elif "pass" in kl or "pwd" in kl or "mdp" in kl:
            payload[k] = password

    log.info("Envoi des identifiants…")
    r = session.post(action, data=payload, timeout=30, allow_redirects=True)
    r.raise_for_status()

    # Vérification simple
    if "logout" not in r.text.lower() and "déconnexion" not in r.text.lower():
        log.warning(
            "Connexion incertaine : la page ne mentionne pas de déconnexion. "
            "Le script va continuer mais certains PDFs peuvent être inaccessibles."
        )
    else:
        log.info("Authentification réussie.")


def get_soup(session: requests.Session, url: str, delay: float) -> BeautifulSoup:
    log.debug("GET %s", url)
    r = session.get(url, timeout=30)
    r.raise_for_status()
    time.sleep(delay)
    return BeautifulSoup(r.text, "lxml")


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    year: str
    numero: str
    url: str


@dataclass
class PdfLink:
    url: str
    title: str
    date: str  # date d'apparition / publication si disponible
    seq: int   # ordre d'apparition dans le numéro


YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
NUM_RE = re.compile(r"n[°ºo]\s*([\w\-/]+)", re.IGNORECASE)


def list_issues(session: requests.Session, delay: float) -> list[Issue]:
    """Découvre tous les numéros depuis la page d'index.

    La structure exacte de jle.com peut varier ; on est tolérant : on collecte
    tous les liens internes qui pointent vers une page « sommaire » de numéro.
    """
    soup = get_soup(session, INDEX_URL, delay)
    issues: list[Issue] = []
    seen: set[str] = set()
    current_year = ""

    for el in soup.descendants:
        if getattr(el, "name", None) is None:
            continue
        # tente de capter une année dans des titres
        if el.name in ("h1", "h2", "h3", "h4", "strong", "b"):
            m = YEAR_RE.search(el.get_text(" ", strip=True))
            if m:
                current_year = m.group(0)
        if el.name == "a":
            href = el.get("href") or ""
            if not href:
                continue
            full = urljoin(INDEX_URL, href)
            if urlparse(full).netloc and "jle.com" not in urlparse(full).netloc:
                continue
            # heuristique : un sommaire a souvent "sommaire" ou "numero" dans l'URL
            if not re.search(r"(sommaire|numero|issue)", full, re.IGNORECASE):
                continue
            if full in seen:
                continue
            text = el.get_text(" ", strip=True)
            year = current_year or (YEAR_RE.search(text).group(0) if YEAR_RE.search(text) else "")
            num_match = NUM_RE.search(text)
            numero = num_match.group(1) if num_match else ""
            if not numero:
                # fallback : dernier segment "numérique" de l'URL
                tail = re.findall(r"\d+", full)
                numero = tail[-1] if tail else "inconnu"
            seen.add(full)
            issues.append(Issue(year=year or "inconnu", numero=numero, url=full))

    log.info("Numéros découverts : %d", len(issues))
    return issues


DATE_RE = re.compile(
    r"(\d{1,2})[\s/-]+"
    r"(janv|févr|fevr|mars|avr|mai|juin|juil|aoû|aou|sept|oct|nov|déc|dec)\w*"
    r"[\s/-]+(\d{4})",
    re.IGNORECASE,
)


def extract_pdfs(
    session: requests.Session, issue: Issue, delay: float
) -> list[PdfLink]:
    soup = get_soup(session, issue.url, delay)
    pdfs: list[PdfLink] = []
    seq = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" not in href.lower():
            continue
        full = urljoin(issue.url, href)
        if urlparse(full).netloc and "jle.com" not in urlparse(full).netloc:
            continue
        title = a.get_text(" ", strip=True) or Path(urlparse(full).path).name
        # date : on regarde le contexte de l'élément parent
        ctx = a.find_parent(["article", "li", "div", "tr", "p"])
        date_str = ""
        if ctx:
            m = DATE_RE.search(ctx.get_text(" ", strip=True))
            if m:
                date_str = "-".join(m.groups())
        seq += 1
        pdfs.append(PdfLink(url=full, title=title, date=date_str or "sans-date", seq=seq))
    return pdfs


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(text: str, maxlen: int = 80) -> str:
    s = SAFE_RE.sub("-", text).strip("-")
    return (s[:maxlen] or "fichier").rstrip("-")


def download(
    session: requests.Session,
    pdf: PdfLink,
    issue: Issue,
    out_root: Path,
    delay: float,
    dry_run: bool,
) -> bool:
    folder = out_root / slugify(issue.year) / slugify(f"n{issue.numero}") / slugify(pdf.date)
    name = f"{pdf.seq:02d}_{slugify(pdf.title)}.pdf"
    dest = folder / name

    if dest.exists() and dest.stat().st_size > 0:
        log.debug("Déjà présent : %s", dest)
        return False

    log.info("→ %s", dest.relative_to(out_root))
    if dry_run:
        return True

    folder.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.part")
    with session.get(pdf.url, stream=True, timeout=120) as r:
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        if "pdf" not in ctype.lower() and "octet-stream" not in ctype.lower():
            log.warning("Pas un PDF (%s) : %s", ctype, pdf.url)
            return False
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)
    tmp.rename(dest)
    time.sleep(delay)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="downloads", help="Dossier de sortie (défaut: downloads)")
    p.add_argument("--delay", type=float, default=1.0, help="Délai entre requêtes en secondes")
    p.add_argument("--year", help="Filtre : ne traite que cette année")
    p.add_argument("--dry-run", action="store_true", help="Liste sans télécharger")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    user = os.environ.get("JLE_USER")
    password = os.environ.get("JLE_PASS")
    if not user or not password:
        log.error("Définissez JLE_USER et JLE_PASS dans l'environnement.")
        return 2

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    session = make_session()
    try:
        login(session, user, password)
    except Exception as e:
        log.error("Échec d'authentification : %s", e)
        return 3

    issues = list_issues(session, args.delay)
    if args.year:
        issues = [i for i in issues if i.year == args.year]
        log.info("Filtré sur %s : %d numéros", args.year, len(issues))

    total_dl = 0
    for issue in issues:
        log.info("=== %s n°%s ===", issue.year, issue.numero)
        try:
            pdfs = extract_pdfs(session, issue, args.delay)
        except Exception as e:
            log.error("Erreur sur %s : %s", issue.url, e)
            continue
        log.info("  %d PDFs", len(pdfs))
        for pdf in pdfs:
            try:
                if download(session, pdf, issue, out_root, args.delay, args.dry_run):
                    total_dl += 1
            except Exception as e:
                log.error("  échec %s : %s", pdf.url, e)

    log.info("Terminé. %d fichiers %s.", total_dl, "listés" if args.dry_run else "téléchargés")
    return 0


if __name__ == "__main__":
    sys.exit(main())
