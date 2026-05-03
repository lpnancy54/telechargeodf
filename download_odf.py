#!/usr/bin/env python3
"""Télécharge les PDFs de la revue ODF (jle.com) triés par année / numéro / date.

Mode recommandé : connectez-vous à jle.com dans Chrome, fermez Chrome,
puis lancez :

    pip install -r requirements.txt
    python download_odf.py --browser chrome

Le script reprend automatiquement les cookies de votre navigateur, donc
plus besoin d'identifiants en variable d'environnement.

Navigateurs supportés : chrome, firefox, edge, brave, opera, chromium, vivaldi.

Options utiles :

    --out downloads          dossier racine de sortie
    --delay 1.0              délai (s) entre requêtes pour être poli
    --year 2023              ne traite qu'une année
    --dry-run                liste sans télécharger
    --verbose                logs détaillés
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


def _verify_session(session: requests.Session) -> None:
    log.info("Vérification de la session…")
    r = session.get(INDEX_URL, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"L'index renvoie HTTP {r.status_code}.")
    body = r.text.lower()
    if any(k in body for k in ("déconnexion", "logout", "mon compte")):
        log.info("Session authentifiée détectée.")
    else:
        log.warning(
            "La page d'index est lisible mais aucune mention de "
            "déconnexion/compte — vous êtes peut-être déconnecté."
        )


def load_cookie_header(session: requests.Session, path: str) -> None:
    """Charge un en-tête Cookie brut (format 'name=value; name=value')."""
    log.info("Lecture de l'en-tête Cookie depuis %s", path)
    raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    n = 0
    for piece in raw.split(";"):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        session.cookies.set(name.strip(), value.strip(), domain=".jle.com")
        n += 1
    log.info("  %d cookies chargés.", n)
    if n == 0:
        raise RuntimeError(f"Aucun cookie lisible dans {path}.")
    _verify_session(session)


def load_cookies_file(session: requests.Session, path: str) -> None:
    """Charge un fichier cookies.txt au format Netscape."""
    from http.cookiejar import MozillaCookieJar

    log.info("Lecture des cookies depuis %s", path)
    jar = MozillaCookieJar()
    try:
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except Exception as e:
        raise RuntimeError(f"Impossible de lire {path} : {e}") from e

    n = 0
    for c in jar:
        if "jle.com" in (c.domain or ""):
            session.cookies.set_cookie(c)
            n += 1
    log.info("  %d cookies jle.com chargés.", n)
    if n == 0:
        raise RuntimeError(
            f"Aucun cookie pour jle.com dans {path}. "
            "Assurez-vous d'être connecté à jle.com avant l'export."
        )
    _verify_session(session)


def load_browser_cookies(session: requests.Session, browser: str) -> None:
    """Charge les cookies jle.com depuis un navigateur installé localement.

    Utilise browser_cookie3. Sous Windows, Chrome chiffre ses cookies avec
    DPAPI : il faut généralement fermer Chrome pour que la lecture passe.
    """
    try:
        import browser_cookie3 as bc3
    except ImportError as e:
        raise RuntimeError(
            "Le module 'browser_cookie3' n'est pas installé. "
            "Lancez :  pip install -r requirements.txt"
        ) from e

    loaders = {
        "chrome": bc3.chrome,
        "firefox": bc3.firefox,
        "edge": bc3.edge,
        "brave": bc3.brave,
        "opera": bc3.opera,
        "chromium": bc3.chromium,
        "vivaldi": bc3.vivaldi,
    }
    if browser not in loaders:
        raise RuntimeError(f"Navigateur non supporté : {browser}")

    log.info("Lecture des cookies %s pour jle.com…", browser)
    try:
        jar = loaders[browser](domain_name="jle.com")
    except Exception as e:
        raise RuntimeError(
            f"Impossible de lire les cookies de {browser} : {e}\n"
            "Astuce Windows : fermez complètement Chrome avant de relancer."
        ) from e

    n = 0
    for c in jar:
        session.cookies.set_cookie(c)
        n += 1
    log.info("  %d cookies chargés.", n)
    if n == 0:
        raise RuntimeError(
            "Aucun cookie jle.com trouvé. Connectez-vous à jle.com dans "
            f"{browser}, puis relancez le script."
        )
    _verify_session(session)


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
            # heuristique : un sommaire est typiquement /fr/revues/odf/sommaire.phtml?cle_parution=NNNN
            if "sommaire" not in full.lower():
                continue
            if full in seen:
                continue
            text = el.get_text(" ", strip=True)
            year = current_year or (YEAR_RE.search(text).group(0) if YEAR_RE.search(text) else "")
            # numéro = cle_parution dans l'URL
            cle = re.search(r"cle_parution=(\d+)", full)
            numero = cle.group(1) if cle else ""
            if not numero:
                num_match = NUM_RE.search(text)
                numero = num_match.group(1) if num_match else "inconnu"
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


ARTICLE_HINT = re.compile(r"(e-docs|article|cle_doc)", re.IGNORECASE)
SKIP_HINT = re.compile(
    r"(sommaire|numero|abonn|panier|login|logout|deconnex|déconnex|/aide|/contact|/cgu|/mentions)",
    re.IGNORECASE,
)


def list_articles(session: requests.Session, sommaire_url: str, delay: float) -> list[str]:
    """Liste les URLs d'articles depuis une page sommaire."""
    soup = get_soup(session, sommaire_url, delay)
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        full = urljoin(sommaire_url, a["href"]).split("#", 1)[0]
        if not full or "jle.com" not in urlparse(full).netloc:
            continue
        if full == sommaire_url:
            continue
        if SKIP_HINT.search(full):
            continue
        if not ARTICLE_HINT.search(full):
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


def extract_pdfs_from_article(
    session: requests.Session, article_url: str, delay: float
) -> tuple[str, str, list[str]]:
    """Renvoie (titre, date, liste d'URLs PDF) pour un article."""
    soup = get_soup(session, article_url, delay)
    title_el = soup.find(["h1", "h2"])
    title = title_el.get_text(" ", strip=True) if title_el else ""
    page_text = soup.get_text(" ", strip=True)
    m = DATE_RE.search(page_text)
    date_str = "-".join(m.groups()) if m else ""

    pdfs: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(article_url, href)
        if "jle.com" not in urlparse(full).netloc:
            continue
        # cas 1 : lien direct .pdf
        if ".pdf" in full.lower():
            pdfs.append(full)
            continue
        # cas 2 : lien dont le texte contient "PDF"
        text = a.get_text(" ", strip=True).lower()
        if "pdf" in text and "telecharg" in text or text == "pdf":
            pdfs.append(full)
    # dédup en gardant l'ordre
    seen: set[str] = set()
    uniq = [p for p in pdfs if not (p in seen or seen.add(p))]
    return title, date_str, uniq


def extract_pdfs(
    session: requests.Session, issue: Issue, delay: float
) -> list[PdfLink]:
    """Pour chaque article du sommaire, récupère les PDFs."""
    articles = list_articles(session, issue.url, delay)
    log.info("  %d articles dans le sommaire", len(articles))
    pdfs: list[PdfLink] = []
    seq = 0
    for art in articles:
        try:
            title, date_str, urls = extract_pdfs_from_article(session, art, delay)
        except Exception as e:
            log.warning("  article %s : %s", art, e)
            continue
        if not urls:
            log.debug("    aucun PDF dans %s", art)
            continue
        for url in urls:
            seq += 1
            pdfs.append(
                PdfLink(
                    url=url,
                    title=title or Path(urlparse(art).path).stem,
                    date=date_str or "sans-date",
                    seq=seq,
                )
            )
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
    p.add_argument("--limit", type=int, default=0, help="N'examiner que les N premiers numéros (0 = tous)")
    p.add_argument("--dry-run", action="store_true", help="Liste sans télécharger")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument(
        "--browser",
        default=None,
        help="Navigateur dans lequel récupérer les cookies de session "
             "(chrome, firefox, edge, brave, opera, chromium, vivaldi). "
             "Note : Chrome/Edge sous Windows refusent souvent la lecture "
             "depuis Chrome 127+ ; utilisez --cookies dans ce cas.",
    )
    p.add_argument(
        "--cookies",
        default=None,
        help="Chemin vers un fichier cookies.txt au format Netscape "
             "(exporté depuis le navigateur via une extension comme "
             "'Get cookies.txt LOCALLY').",
    )
    p.add_argument(
        "--cookie-header",
        default=None,
        help="Chemin vers un fichier contenant l'en-tête Cookie brut "
             "(format 'name1=value1; name2=value2'), copié depuis les "
             "DevTools du navigateur.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    session = make_session()
    try:
        if args.cookie_header:
            load_cookie_header(session, args.cookie_header)
        elif args.cookies:
            load_cookies_file(session, args.cookies)
        elif args.browser:
            load_browser_cookies(session, args.browser)
        else:
            log.error(
                "Indiquez --cookie-header <fichier>, --cookies <fichier> ou --browser <nom>."
            )
            return 2
    except Exception as e:
        log.error("Échec d'authentification : %s", e)
        return 3

    issues = list_issues(session, args.delay)
    if args.year:
        issues = [i for i in issues if i.year == args.year]
        log.info("Filtré sur %s : %d numéros", args.year, len(issues))
    if args.limit:
        issues = issues[: args.limit]
        log.info("Limité à %d numéros", len(issues))

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
