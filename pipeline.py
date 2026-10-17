#!/usr/bin/env python3
"""
NeuroStudi – pipeline di aggiornamento dei dati.

Cosa fa, in ordine:
  1. Scarica gli studi in reclutamento da ClinicalTrials.gov (API v2) e da CTIS (EU).
  2. Tiene solo gli studi con almeno un centro in Lombardia, Toscana o Lazio.
  3. Unisce i doppioni (stesso studio registrato su entrambi i registri).
  4. Verifica il macrogruppo sui codici MeSH e sulle condizioni dichiarate.
  5. Scrive titolo e sintesi in italiano con regole fisse (nessun costo) oppure,
     se richiesto, con un LLM gratuito eseguito in locale tramite Ollama.
  6. Tiene solo recapiti diretti dei gruppi di ricerca: scarta contatti centrali
     degli sponsor, numeri verdi, centralini e recapiti condivisi tra piu' centri.
  7. Applica i contatti dei centri partner (centri_partner.csv).
  8. Scrive trials.json, l'elenco dei centri senza recapito diretto
     (centri_senza_contatto.csv, utile per proporre l'adesione) e, se
     richiesto, inserisce i dati nell'app HTML.

Uso:
  pip install requests
  python pipeline.py --partner centri_partner.csv --html app.html

Con LLM locale gratuito (facoltativo):
  ollama pull qwen2.5:7b
  python pipeline.py --llm ollama --partner centri_partner.csv --html app.html
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger("neurostudi")

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

CTGOV_URL = "https://clinicaltrials.gov/api/v2/studies"
CTGOV_STUDY_PAGE = "https://clinicaltrials.gov/study/{id}"
CTIS_SEARCH_URL = "https://euclinicaltrials.eu/ctis-public-api/search"
CTIS_RETRIEVE_URL = "https://euclinicaltrials.eu/ctis-public-api/retrieve/{ct}"
CTIS_STUDY_PAGE = "https://euclinicaltrials.eu/search-for-clinical-trials/?lang=en&EUCT={ct}"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODELLO = os.environ.get("NEUROSTUDI_OLLAMA_MODEL", "qwen2.5:7b")

MACROGRUPPI = {
    "ictus": {
        "nome": "Ictus",
        "termini": ["stroke", "ischemic stroke", "intracerebral hemorrhage",
                    "subarachnoid hemorrhage", "transient ischemic attack"],
    },
    "sm": {
        "nome": "Sclerosi multipla",
        "termini": ["multiple sclerosis"],
    },
    "parkinson": {
        "nome": "Parkinson e parkinsonismi",
        "termini": ["Parkinson disease", "parkinsonism",
                    "multiple system atrophy", "progressive supranuclear palsy"],
    },
    "sla": {
        "nome": "SLA",
        "termini": ["amyotrophic lateral sclerosis", "motor neuron disease"],
    },
    "invecchiamento": {
        "nome": "Invecchiamento cerebrale e demenze",
        "termini": ["Alzheimer disease", "dementia", "mild cognitive impairment",
                    "cognitive decline", "frontotemporal dementia",
                    "Lewy body disease"],
    },
}

# Citta' (normalizzate) e denominazioni di regione/provincia usate nei registri.
REGIONI = {
    "Lombardia": {
        "nomi": {"lombardia", "lombardy", "mi", "bg", "bs", "pv", "mb", "va",
                 "co", "lc", "cr", "mn", "lo", "so"},
        "citta": {"milano", "milan", "bergamo", "brescia", "pavia", "monza",
                  "varese", "como", "lecco", "cremona", "mantova", "mantua",
                  "lodi", "sondrio", "legnano", "rozzano", "sesto san giovanni",
                  "gallarate", "busto arsizio", "seriate", "pieve emanuele",
                  "castellanza", "garbagnate milanese", "bollate", "vimercate",
                  "tradate", "gussago", "montescano", "lissone", "desio",
                  "cassano d'adda", "san donato milanese", "crema", "treviglio",
                  "saronno", "rho", "magenta", "cernusco sul naviglio",
                  "zingonia", "ponte san pietro", "vizzolo predabissi"},
    },
    "Toscana": {
        "nomi": {"toscana", "tuscany", "fi", "pi", "si", "li", "lu", "ar",
                 "po", "pt", "gr", "ms"},
        "citta": {"firenze", "florence", "pisa", "siena", "livorno", "lucca",
                  "arezzo", "prato", "pistoia", "grosseto", "massa", "carrara",
                  "empoli", "viareggio", "sesto fiorentino", "bagno a ripoli",
                  "pontedera", "cisanello", "careggi", "camaiore", "volterra",
                  "fiesole", "impruneta", "montevarchi", "poggibonsi"},
    },
    "Lazio": {
        "nomi": {"lazio", "latium", "rm", "lt", "fr", "vt", "ri"},
        "citta": {"roma", "rome", "latina", "frosinone", "viterbo", "rieti",
                  "tivoli", "cassino", "fiumicino", "ariccia", "albano laziale",
                  "colleferro", "civitavecchia", "monterotondo", "frascati",
                  "pomezia", "anzio", "velletri", "sora", "formia",
                  "guidonia montecelio", "marino"},
    },
}

STATI_SITO_ESCLUSI = {"COMPLETED", "WITHDRAWN", "TERMINATED", "ACTIVE_NOT_RECRUITING",
                      "SUSPENDED", "TEMPORARILY_NOT_AVAILABLE", "NO_LONGER_AVAILABLE"}

STATI_CTIS_ESCLUSI = ("ended", "withdrawn", "revoked", "expired", "halted",
                      "suspended", "cancel", "not authorised", "lapsed")

FASI = {"EARLY_PHASE1": "Fase 1 iniziale", "PHASE1": "Fase 1", "PHASE2": "Fase 2",
        "PHASE3": "Fase 3", "PHASE4": "Fase 4", "NA": None}

RE_EUCT = re.compile(r"\b20\d{2}-5\d{5}-\d{2}-\d{2}\b")


# ---------------------------------------------------------------------------
# Utilita'
# ---------------------------------------------------------------------------

def norm(testo: str | None) -> str:
    if not testo:
        return ""
    t = unicodedata.normalize("NFKD", testo)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def regione_da(citta: str | None, stato: str | None = None) -> str | None:
    c, s = norm(citta), norm(stato)
    for regione, dati in REGIONI.items():
        if c in dati["citta"] or s in dati["nomi"]:
            return regione
    return None


def eta_in_anni(valore: str | None) -> float | None:
    if not valore:
        return None
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(year|month|week|day)", valore, re.I)
    if not m:
        return None
    n, unita = float(m.group(1)), m.group(2).lower()
    return round(n / {"year": 1, "month": 12, "week": 52, "day": 365}[unita], 1)


def sessione() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "NeuroStudi/0.1 (aggiornamento registri)",
                      "Accept": "application/json"})
    return s


def richiesta(sess, metodo, url, tentativi=4, **kwargs):
    for i in range(tentativi):
        try:
            r = sess.request(metodo, url, timeout=60, **kwargs)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            attesa = 2 ** i
            log.warning("Richiesta fallita (%s), nuovo tentativo tra %ss: %s", url, attesa, e)
            time.sleep(attesa)
    raise RuntimeError(f"Impossibile completare la richiesta: {url}")


def impronta(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False)
                        .encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# ClinicalTrials.gov
# ---------------------------------------------------------------------------

def scarica_ctgov(sess, termine: str) -> list[dict]:
    studi, token = [], None
    while True:
        params = {"query.cond": termine, "query.locn": "Italy",
                  "filter.overallStatus": "RECRUITING,NOT_YET_RECRUITING",
                  "pageSize": 100, "format": "json"}
        if token:
            params["pageToken"] = token
        dati = richiesta(sess, "GET", CTGOV_URL, params=params)
        studi.extend(dati.get("studies", []))
        token = dati.get("nextPageToken")
        if not token:
            return studi
        time.sleep(0.3)


def converti_ctgov(raw: dict, macrogruppo: str) -> dict | None:
    ps = raw.get("protocolSection", {})
    der = raw.get("derivedSection", {})
    ident = ps.get("identificationModule", {})
    stato = ps.get("statusModule", {})
    design = ps.get("designModule", {})
    elig = ps.get("eligibilityModule", {})
    cl = ps.get("contactsLocationsModule", {})
    ai = ps.get("armsInterventionsModule", {})
    sponsor = ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
    nct = ident.get("nctId")
    if not nct:
        return None

    siti = []
    for loc in cl.get("locations", []):
        if norm(loc.get("country")) != "italy":
            continue
        if (loc.get("status") or "").upper() in STATI_SITO_ESCLUSI:
            continue
        regione = regione_da(loc.get("city"), loc.get("state"))
        if not regione:
            continue
        siti.append({
            "nome": loc.get("facility") or "Centro non specificato",
            "citta": loc.get("city"),
            "regione": regione,
            "partner": False,
            "contatto": None,
            "responsabile": None,
            "_contatti_grezzi": loc.get("contacts", []),
        })
    if not siti:
        return None

    secondari = [x.get("id", "") for x in ident.get("secondaryIdInfos", [])]
    euct = sorted({m for x in secondari for m in RE_EUCT.findall(x)})
    tipo = design.get("studyType", "")
    fasi = [FASI.get(f) for f in design.get("phases", []) if FASI.get(f)]
    cond = ps.get("conditionsModule", {}).get("conditions", [])
    mesh = [m.get("term", "") for m in der.get("conditionBrowseModule", {}).get("meshes", [])]
    mesh += [m.get("term", "") for m in der.get("conditionBrowseModule", {}).get("ancestors", [])]

    return {
        "id": nct,
        "id_secondari": euct,
        "fonti": ["ClinicalTrials.gov"],
        "url": CTGOV_STUDY_PAGE.format(id=nct),
        "titolo_originale": ident.get("briefTitle") or ident.get("officialTitle"),
        "macrogruppi": [macrogruppo],
        "condizioni": cond,
        "tipo": "osservazionale" if tipo == "OBSERVATIONAL" else "interventistico",
        "fase": " / ".join(fasi) or None,
        "stato": ("In reclutamento" if stato.get("overallStatus") == "RECRUITING"
                  else "Reclutamento in apertura"),
        "eta_min": eta_in_anni(elig.get("minimumAge")),
        "eta_max": eta_in_anni(elig.get("maximumAge")),
        "sponsor": sponsor.get("name"),
        "interventi": [i.get("name") for i in ai.get("interventions", []) if i.get("name")],
        "aggiornato_registro": stato.get("lastUpdatePostDateStruct", {}).get("date"),
        "descrizione_originale": (ps.get("descriptionModule", {}).get("briefSummary") or "")[:1500] or None,
        "siti": siti,
        "_sponsor_industria": sponsor.get("class") == "INDUSTRY",
        "_telefoni_centrali": [c.get("phone") for c in cl.get("centralContacts", []) if c.get("phone")],
        "_email_centrali": [c.get("email") for c in cl.get("centralContacts", []) if c.get("email")],
        "_fatti": {
            "mesh": mesh,
            "allocazione": design.get("designInfo", {}).get("allocation"),
            "mascheramento": design.get("designInfo", {}).get("maskingInfo", {}).get("masking"),
            "partecipanti": design.get("enrollmentInfo", {}).get("count"),
            "bracci": [{"tipo": a.get("type"), "interventi": a.get("interventionNames", [])}
                       for a in ai.get("armGroups", [])],
            "interventi": [{"tipo": i.get("type"), "nome": i.get("name")}
                           for i in ai.get("interventions", [])],
        },
    }


# ---------------------------------------------------------------------------
# CTIS (EU) – lo schema dell'API pubblica non e' documentato ufficialmente:
# l'adattatore e' volutamente tollerante e, se fallisce, la pipeline prosegue
# con i soli dati di ClinicalTrials.gov.
# ---------------------------------------------------------------------------

def _corpo_ricerca_ctis(termine: str, pagina: int) -> dict:
    return {"pagination": {"page": pagina, "size": 100},
            "sort": {"property": "decisionDate", "direction": "DESC"},
            "searchCriteria": {"medicalCondition": termine}}


def _siti_italiani_ctis(nodo, antenati=()) -> list[dict]:
    """Cerca ricorsivamente indirizzi con citta' nelle tre regioni."""
    trovati = []
    if isinstance(nodo, dict):
        chiavi = {k.lower(): k for k in nodo}
        if "city" in chiavi:
            citta = nodo[chiavi["city"]]
            regione = regione_da(citta) if isinstance(citta, str) else None
            if regione:
                nome = None
                for d in (nodo, *reversed(antenati)):
                    for k in ("name", "organisationname", "businessname", "sitename"):
                        kk = {x.lower(): x for x in d}.get(k)
                        if kk and isinstance(d[kk], str) and d[kk].strip():
                            nome = d[kk].strip()
                            break
                    if nome:
                        break
                trovati.append({"nome": nome or "Centro non specificato",
                                "citta": citta.title(), "regione": regione,
                                "partner": False, "contatto": None,
                                "responsabile": None})
        for v in nodo.values():
            trovati.extend(_siti_italiani_ctis(v, (*antenati, nodo)))
    elif isinstance(nodo, list):
        for v in nodo:
            trovati.extend(_siti_italiani_ctis(v, antenati))
    return trovati


def scarica_ctis(sess, termine: str, max_pagine: int = 10) -> list[dict]:
    risultati = []
    for pagina in range(1, max_pagine + 1):
        dati = richiesta(sess, "POST", CTIS_SEARCH_URL,
                         json=_corpo_ricerca_ctis(termine, pagina))
        elementi = dati.get("data") or []
        for el in elementi:
            stato = norm(str(el.get("ctStatus", "")))
            if any(x in stato for x in STATI_CTIS_ESCLUSI):
                continue
            if "ital" not in norm(json.dumps(el, ensure_ascii=False)):
                continue
            risultati.append(el)
        pag = dati.get("pagination") or {}
        if not elementi or not pag.get("nextPage", False):
            break
        time.sleep(0.5)
    return risultati


def converti_ctis(sess, el: dict, macrogruppo: str) -> dict | None:
    ct = el.get("ctNumber")
    if not ct:
        return None
    dettaglio = richiesta(sess, "GET", CTIS_RETRIEVE_URL.format(ct=ct))
    siti = _siti_italiani_ctis(dettaglio)
    visti, unici = set(), []
    for s in siti:
        k = (norm(s["nome"]), norm(s["citta"]))
        if k not in visti:
            visti.add(k)
            unici.append(s)
    if not unici:
        return None
    return {
        "id": ct,
        "id_secondari": [],
        "fonti": ["CTIS"],
        "url": CTIS_STUDY_PAGE.format(ct=ct),
        "titolo_originale": el.get("ctTitle") or ct,
        "macrogruppi": [macrogruppo],
        "condizioni": [el.get("conditions")] if isinstance(el.get("conditions"), str) else [],
        "tipo": "interventistico",          # CTIS registra solo sperimentazioni sui farmaci
        "fase": el.get("trialPhase") or None,
        "stato": "In corso (autorizzato UE)",
        "eta_min": None, "eta_max": None,   # ricavate dall'LLM dai criteri
        "sponsor": el.get("sponsor") or None,
        "interventi": [el["product"]] if isinstance(el.get("product"), str) else [],
        "aggiornato_registro": el.get("lastUpdated") or el.get("decisionDate"),
        "descrizione_originale": None,
        "siti": unici,
        "_sponsor_industria": False,
        "_fatti": {"mesh": []},
    }


# ---------------------------------------------------------------------------
# Unione e deduplicazione
# ---------------------------------------------------------------------------

def unisci(studi: list[dict]) -> list[dict]:
    per_id: dict[str, dict] = {}
    alias: dict[str, str] = {}
    for s in studi:
        chiave = alias.get(s["id"]) or next((alias[x] for x in s["id_secondari"] if x in alias), None)
        if chiave is None and s["id"] in per_id:
            chiave = s["id"]
        if chiave is None:
            per_id[s["id"]] = s
            alias[s["id"]] = s["id"]
            for x in s["id_secondari"]:
                alias[x] = s["id"]
            continue
        base = per_id[chiave]
        base["macrogruppi"] = sorted(set(base["macrogruppi"]) | set(s["macrogruppi"]))
        base["fonti"] = sorted(set(base["fonti"]) | set(s["fonti"]))
        if s["id"] != base["id"] and s["id"] not in base["id_secondari"]:
            base["id_secondari"].append(s["id"])
        noti = {(norm(x["nome"]), norm(x["citta"])) for x in base["siti"]}
        noti_citta = {norm(x["citta"]) for x in base["siti"]}
        for sito in s["siti"]:
            if (norm(sito["nome"]), norm(sito["citta"])) in noti:
                continue
            # dai registri UE lo stesso centro ha spesso un nome diverso:
            # aggiungiamo solo citta' non ancora coperte
            if "CTIS" in s["fonti"] and norm(sito["citta"]) in noti_citta:
                continue
            base["siti"].append(sito)
    return list(per_id.values())


# ---------------------------------------------------------------------------
# Classificazione e testi in italiano senza LLM
# ---------------------------------------------------------------------------

PATTERN_GRUPPI = {
    "ictus": r"\b(stroke|cerebral infarction|brain infarction|brain ischemia|cerebral ischemia|"
             r"ischemic attack|intracerebral hemorrhage|cerebral hemorrhage|intracranial hemorrhage|"
             r"subarachnoid hemorrhage|cerebrovascular)",
    "sm": r"\b(multiple sclerosis|clinically isolated syndrome)",
    "parkinson": r"\b(parkinson|multiple system atrophy|supranuclear palsy|corticobasal)",
    "sla": r"\b(amyotrophic lateral sclerosis|motor neuron disease|primary lateral sclerosis|"
           r"progressive muscular atrophy|\bals\b)",
    "invecchiamento": r"\b(alzheimer|dementia|cognitive impairment|cognitive decline|cognitive dysfunction|"
                      r"neurocognitive disorder|lewy bod|frontotemporal|cognitive aging|brain aging)",
}
ESCLUSIONI_GRUPPO = {"ictus": r"\b(heat stroke|heatstroke|sunstroke)"}

NOMI_BREVI = {"ictus": "ictus", "sm": "sclerosi multipla", "parkinson": "malattia di Parkinson",
              "sla": "SLA", "invecchiamento": "demenza o declino cognitivo"}

TIPI_INTERVENTO = {
    "DRUG": ("farmaco", "un farmaco"), "BIOLOGICAL": ("farmaco biologico", "un farmaco biologico"),
    "GENETIC": ("terapia genetica", "una terapia genetica"),
    "DEVICE": ("dispositivo medico", "un dispositivo medico"),
    "PROCEDURE": ("procedura", "una procedura"),
    "BEHAVIORAL": ("programma riabilitativo o comportamentale", "un programma riabilitativo o comportamentale"),
    "DIETARY_SUPPLEMENT": ("integratore", "un integratore"),
    "RADIATION": ("trattamento con radiazioni", "un trattamento con radiazioni"),
    "DIAGNOSTIC_TEST": ("esame diagnostico", "un esame diagnostico"),
    "COMBINATION_PRODUCT": ("prodotto combinato", "un prodotto combinato"),
    "OTHER": ("intervento", "un intervento"),
}
RE_PLACEBO = re.compile(r"\b(placebo|sham|vehicle)\b", re.I)


def gruppi_confermati(studio: dict) -> list[str]:
    testo = " | ".join([*studio.get("condizioni", []), *studio.get("_fatti", {}).get("mesh", [])])
    if not testo.strip():               # nessuna informazione: ci si fida della ricerca
        return studio["macrogruppi"]
    t = testo.lower()
    trovati = []
    for g, pat in PATTERN_GRUPPI.items():
        if re.search(pat, t) and not (g in ESCLUSIONI_GRUPPO and re.search(ESCLUSIONI_GRUPPO[g], t)
                                      and not re.search(pat, re.sub(ESCLUSIONI_GRUPPO[g], "", t))):
            trovati.append(g)
    return trovati


def placebo_da(studio: dict) -> bool | None:
    f = studio.get("_fatti", {})
    if studio["tipo"] == "osservazionale":
        return None
    if any(b.get("tipo") in ("PLACEBO_COMPARATOR", "SHAM_COMPARATOR") for b in f.get("bracci", [])):
        return True
    if any(RE_PLACEBO.search(i.get("nome") or "") for i in f.get("interventi", [])):
        return True
    return False if f.get("bracci") else None


def intervento_principale(studio: dict) -> tuple[str | None, str | None]:
    f = studio.get("_fatti", {})
    sperimentali = {n for b in f.get("bracci", []) if b.get("tipo") == "EXPERIMENTAL"
                    for n in b.get("interventi", [])}
    candidati = [i for i in f.get("interventi", []) if not RE_PLACEBO.search(i.get("nome") or "")]
    for i in candidati:
        if any((i.get("nome") or "") in n for n in sperimentali):
            return i.get("tipo"), i.get("nome")
    if candidati:
        return candidati[0].get("tipo"), candidati[0].get("nome")
    return None, None


def testi_da_regole(studio: dict) -> dict:
    gruppo = NOMI_BREVI[studio["macrogruppi"][0]]
    f = studio.get("_fatti", {})
    n = f.get("partecipanti")
    frase_n = f" In totale lo studio prevede circa {n} partecipanti." if isinstance(n, int) and n > 0 else ""

    if studio["tipo"] == "osservazionale":
        return {"titolo_it": f"Studio osservazionale su persone con {gruppo}",
                "sintesi_it": (f"Studio osservazionale su persone con {gruppo}: raccoglie dati nel tempo "
                               "(visite, esami, questionari) e non prevede trattamenti sperimentali." + frase_n),
                "intervento_it": None}

    tipo, nome = intervento_principale(studio)
    breve, con_articolo = TIPI_INTERVENTO.get(tipo or "OTHER", TIPI_INTERVENTO["OTHER"])
    if "CTIS" in studio["fonti"] and not studio.get("_fatti", {}).get("bracci"):
        breve, con_articolo = "farmaco", "un farmaco"
    nome_txt = f" ({nome})" if nome else ""
    fase = f"di {studio['fase'].lower()} " if studio.get("fase") else ""
    frasi = [f"Studio {fase}che valuta {con_articolo}{nome_txt} in persone con {gruppo}."]
    if f.get("allocazione") == "RANDOMIZED":
        frasi.append("I partecipanti sono assegnati a caso a gruppi diversi.")
    p = placebo_da(studio)
    if p:
        sham = any(b.get("tipo") == "SHAM_COMPARATOR" for b in f.get("bracci", []))
        frasi.append("Uno dei gruppi riceve una procedura simulata, senza effetto." if sham else
                     "Uno dei gruppi riceve un placebo, cioè un trattamento senza principio attivo.")
    m = f.get("mascheramento")
    if m in ("DOUBLE", "TRIPLE", "QUADRUPLE"):
        frasi.append("Né i partecipanti né i medici sanno a quale gruppo si appartiene (doppio cieco).")
    elif m == "SINGLE":
        frasi.append("I partecipanti non sanno a quale gruppo appartengono.")
    titolo = f"{breve[0].upper() + breve[1:]} per {gruppo}" + (f": {nome}" if nome else "")
    return {"titolo_it": titolo, "sintesi_it": " ".join(frasi) + frase_n,
            "intervento_it": f"{breve[0].upper() + breve[1:]}{nome_txt}"}


# ---------------------------------------------------------------------------
# LLM gratuito in locale (Ollama) – facoltativo
# ---------------------------------------------------------------------------

ISTRUZIONI_LLM = """Sei un redattore medico. Riscrivi in italiano semplice (livello B1) la
scheda di uno studio clinico per pazienti neurologici e familiari.
Regole: usa solo le informazioni fornite; non promettere benefici; non scrivere
mai che il trattamento cura o guarisce; se c'e' placebo dillo; non valutare chi
puo' partecipare. Rispondi SOLO con JSON:
{"titolo_it": "max 14 parole",
 "sintesi_it": "60-100 parole",
 "cosa_comporta_it": "max 50 parole su visite, durata, esami, oppure null se non indicati"}"""

PAROLE_VIETATE = re.compile(r"\b(guarisc\w*|guarigione|cura definitiva|garantit\w*|miracol\w*)\b", re.I)


def arricchisci_ollama(studi: list[dict], url: str, modello: str, cache_path: Path) -> None:
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    sess = requests.Session()
    for i, s in enumerate(studi, 1):
        dati = {"titolo": s["titolo_originale"], "descrizione": s.get("descrizione_originale"),
                "tipo": s["tipo"], "fase": s.get("fase"), "placebo": s.get("placebo"),
                "interventi": s.get("interventi"), "condizioni": s.get("condizioni")}
        chiave = f"{s['id']}:{impronta(dati)}:{modello}"
        out = cache.get(chiave)
        if out is None:
            if not s.get("descrizione_originale"):
                continue
            log.info("[%d/%d] Ollama: %s", i, len(studi), s["id"])
            try:
                r = sess.post(f"{url}/api/chat", timeout=300, json={
                    "model": modello, "stream": False, "format": "json",
                    "options": {"temperature": 0.2},
                    "messages": [{"role": "system", "content": ISTRUZIONI_LLM},
                                 {"role": "user", "content": json.dumps(dati, ensure_ascii=False)}]})
                r.raise_for_status()
                out = json.loads(r.json()["message"]["content"])
            except Exception as e:
                log.error("Ollama non riuscito per %s (resta il testo a regole): %s", s["id"], e)
                continue
            cache[chiave] = out
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
        testo = " ".join(str(out.get(k) or "") for k in ("titolo_it", "sintesi_it", "cosa_comporta_it"))
        sintesi = out.get("sintesi_it") or ""
        if PAROLE_VIETATE.search(testo) or not (150 <= len(sintesi) <= 1100):
            log.warning("Testo LLM scartato per %s (controlli di sicurezza)", s["id"])
            continue
        s["titolo_it"] = (out.get("titolo_it") or s["titolo_it"])[:140]
        s["sintesi_it"] = sintesi
        s["cosa_comporta_it"] = out.get("cosa_comporta_it") or None
        s["testo_generato"] = "llm_locale"


# ---------------------------------------------------------------------------
# Recapiti: solo contatti diretti del gruppo di ricerca
# ---------------------------------------------------------------------------

NOMI_GENERICI = re.compile(
    r"(site public contact|public contact|call cent|contact cent|information cent|medical information|"
    r"hotline|switchboard|centralino|\burp\b|reception|patient recruit|recruitment cent|clinical trials? "
    r"(information|registry|desk|support)|study contact|trial contact|\binc\b|\bltd\b|\bllc\b|\bgmbh\b|"
    r"\bs\.?p\.?a\.?\b|\bs\.?r\.?l\.?\b|pharmaceutical|pharma\b)", re.I)
EMAIL_GENERICHE = re.compile(
    r"^(info|urp|centralino|cup|protocollo|comunicazione|ufficio\.?stampa|segreteria\.?generale|"
    r"direzione|noreply|no-reply|contact|contatti|clinicaltrials?|medinfo|"
    r"medical\.?information|trialrecruitment)\b", re.I)
PAROLE_COMUNI_SPONSOR = {"university", "universita", "hospital", "ospedale", "ospedaliera", "azienda",
                         "fondazione", "istituto", "irccs", "foundation", "institute", "national",
                         "research", "ricerca", "italia", "italy", "group", "gruppo", "clinic",
                         "centre", "center", "centro", "sanitaria", "locale", "studi", "degli", "della"}


def tel_norm(t: str | None) -> str:
    if not t:
        return ""
    t = str(t).split(" int.")[0].split(" ext")[0]
    cifre = re.sub(r"[^\d+]", "", t)
    if cifre.startswith("0039"):
        cifre = "+39" + cifre[4:]
    if not cifre.startswith("+"):
        cifre = "+39" + cifre
    return cifre


def tel_ammesso(t: str) -> bool:
    if not t.startswith("+39"):
        return False                       # numeri esteri: tipicamente sponsor o CRO
    nazionale = t[3:]
    if nazionale.startswith(("800", "803", "199", "892")):
        return False                       # numeri verdi e a pagamento
    return 6 <= len(nazionale) <= 11


def _token_sponsor(nome: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", norm(nome)) if w not in PAROLE_COMUNI_SPONSOR}


def seleziona_contatti(studi: list[dict]) -> None:
    # recapiti usati da molti centri diversi = linee centralizzate (sponsor, CRO, centralini)
    centri_per_tel: dict[str, set] = {}
    centri_per_mail: dict[str, set] = {}
    for s in studi:
        for sito in s["siti"]:
            centro = norm(sito["nome"]) + "|" + norm(sito["citta"])
            for c in sito.get("_contatti_grezzi", []):
                if c.get("phone"):
                    centri_per_tel.setdefault(tel_norm(c["phone"]), set()).add(centro)
                if c.get("email"):
                    centri_per_mail.setdefault(norm(c["email"]), set()).add(centro)

    for s in studi:
        tel_centrali = {tel_norm(x) for x in s.get("_telefoni_centrali", [])}
        mail_centrali = {norm(x) for x in s.get("_email_centrali", [])}
        token_sp = _token_sponsor(s.get("sponsor")) if s.get("_sponsor_industria") else set()
        for sito in s["siti"]:
            grezzi = sito.pop("_contatti_grezzi", [])
            if sito.get("partner"):
                continue
            candidati = []
            for c in grezzi:
                ruolo = (c.get("role") or "").upper()
                nome = c.get("name") or ""
                if ruolo == "PRINCIPAL_INVESTIGATOR" and nome and not NOMI_GENERICI.search(nome):
                    sito["responsabile"] = sito.get("responsabile") or nome
                if NOMI_GENERICI.search(nome) or (token_sp and token_sp & _token_sponsor(nome)):
                    continue
                tel = tel_norm(c.get("phone"))
                if tel and (not tel_ammesso(tel) or tel in tel_centrali
                            or len(centri_per_tel.get(tel, ())) >= 3):
                    tel = ""
                mail = (c.get("email") or "").strip()
                if mail:
                    locale, _, dominio = norm(mail).partition("@")
                    if (EMAIL_GENERICHE.search(locale) or "pec." in dominio or norm(mail) in mail_centrali
                            or len(centri_per_mail.get(norm(mail), ())) >= 3
                            or (token_sp and any(t in dominio for t in token_sp))):
                        mail = ""
                if not (tel or mail):
                    continue
                priorita = {"PRINCIPAL_INVESTIGATOR": 0, "CONTACT": 1, "SUB_INVESTIGATOR": 2}.get(ruolo, 3)
                candidati.append((0 if tel else 1, priorita, {
                    "nome": nome or None,
                    "ruolo": {"PRINCIPAL_INVESTIGATOR": "Responsabile dello studio nel centro",
                              "SUB_INVESTIGATOR": "Medico sperimentatore",
                              "CONTACT": "Referente dello studio"}.get(ruolo, "Gruppo di ricerca"),
                    "telefono": (c.get("phone") or "").strip() if tel else None,
                    "email": mail or None,
                    "origine": "registro"}))
            sito["contatto"] = min(candidati, key=lambda x: x[:2])[2] if candidati else None
            sito.setdefault("responsabile", None)


# ---------------------------------------------------------------------------
# Centri partner e report
# ---------------------------------------------------------------------------

def applica_partner(studi: list[dict], csv_path: Path) -> None:
    """
    CSV (separatore ;) con intestazione:
    id_studio;citta;nome_centro;contatto_nome;contatto_ruolo;telefono;email
    """
    indice = {}
    for s in studi:
        for x in [s["id"], *s["id_secondari"]]:
            indice[x.strip().upper()] = s
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for riga in csv.DictReader(f, delimiter=";"):
            sid = (riga.get("id_studio") or "").strip().upper()
            studio = indice.get(sid)
            if not studio:
                log.warning("Partner: studio %s non trovato tra quelli in reclutamento", sid)
                continue
            contatto = {"nome": riga.get("contatto_nome") or None,
                        "ruolo": riga.get("contatto_ruolo") or "Team di ricerca",
                        "telefono": riga.get("telefono") or None,
                        "email": riga.get("email") or None,
                        "origine": "partner"}
            citta = norm(riga.get("citta"))
            sito = next((x for x in studio["siti"] if norm(x["citta"]) == citta), None)
            if sito is None:
                regione = regione_da(riga.get("citta"))
                if not regione:
                    log.warning("Partner: citta' %s fuori dalle tre regioni", riga.get("citta"))
                    continue
                sito = {"nome": riga.get("nome_centro") or "Centro partner",
                        "citta": (riga.get("citta") or "").strip(), "regione": regione,
                        "responsabile": None}
                studio["siti"].append(sito)
            sito.update({"partner": True, "contatto": contatto})
            if riga.get("nome_centro"):
                sito["nome"] = riga["nome_centro"].strip()


def scrivi_report_senza_contatto(studi: list[dict], path: Path) -> int:
    righe = [(s["id"], s["titolo_originale"], x["nome"], x["citta"], x["regione"],
              x.get("responsabile") or "", s["url"])
             for s in studi for x in s["siti"] if not x.get("contatto")]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id_studio", "titolo", "centro", "citta", "regione", "responsabile", "url_registro"])
        w.writerows(righe)
    return len(righe)



# ---------------------------------------------------------------------------
# Uscita
# ---------------------------------------------------------------------------

def inserisci_in_html(html_path: Path, dati: dict, uscita: Path) -> None:
    html = html_path.read_text(encoding="utf-8")
    blocco = json.dumps(dati, ensure_ascii=False).replace("</", "<\\/")
    nuovo, n = re.subn(
        r'(<script type="application/json" id="dati-studi">)(.*?)(</script>)',
        lambda m: m.group(1) + blocco + m.group(3), html, flags=re.S)
    if n != 1:
        raise SystemExit("Blocco <script id=\"dati-studi\"> non trovato nell'HTML")
    uscita.write_text(nuovo, encoding="utf-8")
    log.info("App con dati aggiornati scritta in %s", uscita)



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Aggiorna i dati di NeuroStudi")
    ap.add_argument("--out", default="trials.json")
    ap.add_argument("--partner", help="CSV dei centri partner")
    ap.add_argument("--html", help="app.html in cui inserire i dati")
    ap.add_argument("--llm", choices=["nessuno", "ollama"], default="nessuno",
                    help="testi a regole (default) o LLM gratuito locale con Ollama")
    ap.add_argument("--ollama-url", default=OLLAMA_URL)
    ap.add_argument("--ollama-modello", default=OLLAMA_MODELLO)
    ap.add_argument("--no-ctis", action="store_true", help="salta il registro UE")
    ap.add_argument("--cache", default=".cache_llm.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    sess = sessione()
    grezzi: list[dict] = []
    for chiave, mg in MACROGRUPPI.items():
        for termine in mg["termini"]:
            log.info("ClinicalTrials.gov: %s", termine)
            for raw in scarica_ctgov(sess, termine):
                s = converti_ctgov(raw, chiave)
                if s:
                    grezzi.append(s)
            if args.no_ctis:
                continue
            try:
                log.info("CTIS: %s", termine)
                for el in scarica_ctis(sess, termine):
                    s = converti_ctis(sess, el, chiave)
                    if s:
                        grezzi.append(s)
            except Exception as e:
                log.error("CTIS non disponibile per '%s' (si prosegue): %s", termine, e)

    studi = []
    for s in unisci(grezzi):
        confermati = gruppi_confermati(s)
        if not confermati:
            log.info("Escluso %s: le condizioni non rientrano nei macrogruppi (%s)",
                     s["id"], "; ".join(s.get("condizioni", []))[:120])
            continue
        s["macrogruppi"] = confermati
        s["placebo"] = placebo_da(s)
        s.update(testi_da_regole(s))
        s["cosa_comporta_it"] = None
        s["testo_generato"] = "regole"
        studi.append(s)
    log.info("Studi con centri in Lombardia, Toscana o Lazio: %d", len(studi))

    if args.llm == "ollama":
        arricchisci_ollama(studi, args.ollama_url, args.ollama_modello, Path(args.cache))

    if args.partner:
        applica_partner(studi, Path(args.partner))
    seleziona_contatti(studi)

    for s in studi:
        for k in [k for k in s if k.startswith("_")]:
            del s[k]
        s["siti"].sort(key=lambda x: (not x.get("partner"), x.get("contatto") is None,
                                      x["regione"], x["citta"] or ""))

    n_senza = scrivi_report_senza_contatto(studi, Path(args.out).with_name("centri_senza_contatto.csv"))
    n_siti = sum(len(s["siti"]) for s in studi)
    log.info("Centri con recapito diretto: %d su %d (gli altri sono in centri_senza_contatto.csv)",
             n_siti - n_senza, n_siti)

    dati = {"generato_il": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "dimostrativo": False,
            "fonti": ["ClinicalTrials.gov"] + ([] if args.no_ctis else ["CTIS"]),
            "studi": sorted(studi, key=lambda s: (not any(x.get("partner") for x in s["siti"]),
                                                   not any(x.get("contatto") for x in s["siti"]),
                                                   s["id"]))}
    Path(args.out).write_text(json.dumps(dati, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Scritto %s (%d studi)", args.out, len(studi))

    if args.html:
        p = Path(args.html)
        inserisci_in_html(p, dati, p.with_name(p.stem + "_aggiornata" + p.suffix))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
