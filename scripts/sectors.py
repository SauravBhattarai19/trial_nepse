"""Sector of every ordinary share, kept in data/sectors.csv (symbol,sector,source).

  python scripts/sectors.py            # CI: verify/fill sectors from ShareSansar
  python scripts/sectors.py --offline  # just (re)write the file from the seed

Where sectors come from
  * SEED below: a hand-made starting map (NEPSE's own sector names), so the
    site works before the first scrape. Symbols not in it start as
    "Unclassified".
  * In CI, every row whose source is not yet "sharesansar" is looked up on
    ShareSansar's company page (/company/<symbol>), which shows the sector
    next to a "Sector" label. Only an answer matching a known NEPSE sector
    name is accepted, so a page redesign can't write garbage; anything else
    keeps the seed. Verified rows are not fetched again, and new listings are
    picked up automatically. Edit the CSV by hand to override (set source to
    "sharesansar" or "manual" so CI leaves it alone).

Mutual funds and debentures are not listed: securities.py already types them.
"""
import csv
import html as htmllib
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from securities import EQUITY, load_companies  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "sectors.csv"
BASE = "https://www.sharesansar.com"
UNCLASSIFIED = "Unclassified"
DELAY = 0.3
MAX_LOOKUPS = 400

SECTORS = [
    "Commercial Banks", "Development Banks", "Finance", "Microfinance",
    "Life Insurance", "Non Life Insurance", "Hydro Power", "Investment",
    "Manufacturing And Processing", "Hotels And Tourism", "Trading", "Others",
]
NON_EQUITY = {"mutualfund", "corporatedebentures", "debenture", "promotershare", "preferenceshare", "governmentbonds"}


def _key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower().replace("&", "and"))


CANON = {_key(s): s for s in SECTORS}
CANON.update({
    _key("Hydropower"): "Hydro Power",
    _key("Micro Finance"): "Microfinance",
    _key("Microfinance Institutions"): "Microfinance",
    _key("Non-Life Insurance"): "Non Life Insurance",
    _key("Nonlife Insurance"): "Non Life Insurance",
    _key("Manufacturing & Processing"): "Manufacturing And Processing",
    _key("Hotels & Tourism"): "Hotels And Tourism",
    _key("Hotel And Tourism"): "Hotels And Tourism",
    _key("Tradings"): "Trading",
    _key("Other"): "Others",
})


def canonical(name: str):
    return CANON.get(_key(name))


_B, _D, _F, _M = "Commercial Banks", "Development Banks", "Finance", "Microfinance"
_LI, _NL, _H, _I = "Life Insurance", "Non Life Insurance", "Hydro Power", "Investment"
_MP, _HT, _T, _O = "Manufacturing And Processing", "Hotels And Tourism", "Trading", "Others"
SEED = {
    # commercial banks (incl. merged/delisted ones still in the data)
    **dict.fromkeys("ADBL BOKL CBL CCBL CZBIL EBL GBIME HBL KBL LBL LSL MBL MEGA NABIL NBB NBL NCCB NIB "
                    "NICA NIMB NIMBPO NMB PCBL PRVU SANIMA SBI SBL SCB SRBL".split(), _B),
    **dict.fromkeys("CORBL DDBL EDBL GBBL GRDBL JBBL KSBBL LBBL MDB MLBL MNBBL NABBC SABBL SADBL "
                    "SAPDBL SHINE SINDU".split(), _D),
    **dict.fromkeys("BFC CFCL CMB GFCL GMFIL GUFL ICFC JFL MFIL MPFL NFS PFL PROFL RLFL SFCL SIFC".split(), _F),
    **dict.fromkeys("ACLBSL ALBSL ANLB AVYAN CBBL CYCL DLBS FMDBL FOWAD GBLBS GILB GLBSL GMFBS HLBSL ILBS "
                    "JBLB JSLBB KMCDB LLBS MATRI MERO MLBBL MLBS MLBSL MSLB NADEP NESDO NICLBSL NMBMF "
                    "NMFBS NMLBBL NUBL RSDC SHLB SKBBL SLBBL SLBBLP SLBSL SMATA SMB SMFBS SMPDA SWASTIK "
                    "SWBBL SWMF ULBSL UNLB USLB VLBS WNLB".split(), _M),
    **dict.fromkeys("ALICL CLI GLICL GMLI HLI ILI JLI LICN NLIC NLICL PLI PLIC PMLI RLI RNLI SJLIC SLI "
                    "SLICL SNLI SRLI ULI".split(), _LI),
    **dict.fromkeys("HEI HEIP HRL IGI NICL NIL NLG NRIC PRIN RBCL RBCLPO SALICO SGIC SICL SPIL UAIL".split(), _NL),
    **dict.fromkeys("AHL AHPC AKJCL AKPL API BARUN BEDC BHDC BHL BHPL BJHL BNHC BPCL CHCL CHL CKHL DHPL "
                    "DOLTI DORDI EHPL GHL GLH GVL HDHPC HHL HIMSTAR HPPL HURJA IHL JOSHI KBSH KKHC KPCL "
                    "LEC MABEL MAKAR MANDU MBJC MCHL MEHL MEL MEN MHCL MHL MHNL MKHC MKHL MKJC MMKJL "
                    "MSHL NGPL NHDL NHPC NYADI PHCL PMHPL PPCL PPL PURE RADHI RAWA RFPL RHGCL RHPC RHPL "
                    "RIDI RURU SAHAS SANVI SGHC SHEL SHPC SIKLES SJCL SKHEL SKHL SMH SMHL SMJC SPC SPDL "
                    "SPHL SPL SSHL TAMOR TPC TSHL TVCL UHEWA ULHC UMHL UMRH UNHPL UPCL UPPER USHEC USHL "
                    "VLUCL".split(), _H),
    **dict.fromkeys("CHDC CIT ENL HATHY HIDCL HIDCLP NIFRA NRN".split(), _I),
    **dict.fromkeys("BNT GCIL HDL OMPL RSML SARBTM SHIVM SONA UNL".split(), _MP),
    **dict.fromkeys("BANDIPUR CGH CITY KDL OHL SHL TRH".split(), _HT),
    **dict.fromkeys("BBC STC".split(), _T),
    **dict.fromkeys("MKCL NRM NTC NWCL TTL".split(), _O),
}


def load_sectors() -> dict:
    """symbol -> sector for ordinary shares (file first, seed as fallback)."""
    out = dict(SEED)
    if OUT_PATH.exists():
        with OUT_PATH.open(newline="") as f:
            for row in csv.DictReader(f):
                out[row["symbol"]] = row["sector"]
    return out


def fetch(url: str) -> str:
    # curl rather than requests: ShareSansar's WAF lets curl through (see fetch_nepse_index.py)
    r = subprocess.run(["curl", "-s", "--fail", "--max-time", "30", "-A", "Mozilla/5.0", url],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def scrape_sector(symbol: str):
    page = fetch(f"{BASE}/company/{symbol.lower()}")
    if not page:
        return None
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    lines = [htmllib.unescape(t).strip() for t in re.sub(r"<[^>]+>", "\n", text).splitlines()]
    lines = [t for t in lines if t]
    for i, line in enumerate(lines):
        if not re.match(r"^sector\b", line, re.I):
            continue
        for cand in [line] + lines[i + 1:i + 4]:
            value = re.sub(r"^sector\s*:?\s*", "", cand, flags=re.I).strip(" :-")
            if _key(value) in NON_EQUITY:
                return None
            if canonical(value):
                return canonical(value)
    return None


def main(offline: bool):
    rows = {}
    if OUT_PATH.exists():
        with OUT_PATH.open(newline="") as f:
            rows = {r["symbol"]: r for r in csv.DictReader(f)}
    equities = [sym for sym, kind, _ in load_companies() if kind == EQUITY]
    for sym in equities:
        rows.setdefault(sym, {"symbol": sym, "sector": SEED.get(sym, UNCLASSIFIED), "source": "seed"})

    if not offline:
        todo = [s for s in equities if rows[s]["source"] not in ("sharesansar", "manual")][:MAX_LOOKUPS]
        found = changed = 0
        for sym in todo:
            time.sleep(DELAY)
            sector = scrape_sector(sym)
            if not sector:
                continue
            found += 1
            if sector != rows[sym]["sector"]:
                print(f"  {sym}: {rows[sym]['sector']} -> {sector}")
                changed += 1
            rows[sym] = {"symbol": sym, "sector": sector, "source": "sharesansar"}
        print(f"Looked up {len(todo)} symbols on ShareSansar: {found} found, {changed} changed.")

    with OUT_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "sector", "source"])
        w.writeheader()
        for sym in sorted(rows):
            w.writerow(rows[sym])
    counts = {}
    for r in rows.values():
        counts[r["sector"]] = counts.get(r["sector"], 0) + 1
    print(f"Wrote {OUT_PATH}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main(offline="--offline" in sys.argv)
