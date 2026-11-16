import time
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlencode, urljoin
from bs4 import BeautifulSoup
from io import BytesIO

import pandas as pd
import requests
import streamlit as st
from openpyxl.styles import PatternFill

st.set_page_config(page_title="Outils SIRET – API INSEE", page_icon="🏢")
st.title("🏢 Outils SIRET – API INSEE")

API_KEY = st.secrets["API_KEY"]
HEADERS = {"X-INSEE-Api-Key-Integration": API_KEY}
BASE = "https://api.insee.fr/api-sirene/3.11/siret"
GEO = "https://geo.api.gouv.fr"


def call_api(url, params=None, headers=None):
    for attempt in range(5):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=45)
        except requests.RequestException as exc:
            if attempt == 4:
                raise RuntimeError(f"Connexion impossible : {exc}") from exc
            time.sleep(2 * (attempt + 1))
            continue
        if response.status_code in (429, 502, 503, 504) and attempt < 4:
            time.sleep(3 * (attempt + 1))
            continue
        return response
    raise RuntimeError("Service temporairement indisponible")


def legal_name(unite):
    unite = unite or {}
    return (unite.get("denominationUniteLegale") or
            " ".join(filter(None, [unite.get("prenomUsuelUniteLegale"), unite.get("nomUniteLegale")])) or "")


def latest_period(etab):
    periods = etab.get("periodesEtablissement") or []
    if not periods:
        return {}
    return max(periods, key=lambda p: p.get("dateDebut") or "")


def excel_bytes(sheets, color_status=False):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)
            ws = writer.sheets[name[:31]]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                letter = col[0].column_letter
                width = min(55, max(13, max(len(str(c.value or "")) for c in col[:150]) + 2))
                ws.column_dimensions[letter].width = width
            if color_status and "Statut" in frame.columns:
                idx = list(frame.columns).index("Statut") + 1
                for cells in list(ws.iter_rows(min_row=2)):
                    statut = str(cells[idx - 1].value or "").lower()
                    shade = "C6EFCE" if statut == "actif" else "FFC7CE" if statut == "fermé" else "FFEB9C"
                    for cell in cells:
                        cell.fill = PatternFill("solid", fgColor=shade)
    return output.getvalue()


@st.cache_data(ttl=86400)
def metro_communes():
    r = call_api(f"{GEO}/epcis/243300316/communes", params={"fields": "nom,code,codeDepartement"})
    r.raise_for_status()
    return sorted(r.json(), key=lambda x: x["nom"])


@st.cache_data(ttl=86400)
def find_communes(name):
    r = call_api(f"{GEO}/communes", params={"nom": name, "fields": "nom,code,codeDepartement", "limit": 30})
    r.raise_for_status()
    return sorted(r.json(), key=lambda x: (x["nom"], x.get("codeDepartement", "")))


def fetch_establishments(naf, communes, progress):
    # Query on non-historic geographic field only; all other criteria are
    # checked against the latest establishment period after retrieval.
    # This avoids the Sirene query parser's historic-field syntax pitfalls.
    found = {}
    for i, commune in enumerate(communes):
        cursor = "*"
        while True:
            params = {"q": f"codeCommuneEtablissement:{commune['code']}",
                      "nombre": 1000, "curseur": cursor}
            r = call_api(BASE, params=params, headers=HEADERS)
            if r.status_code != 200:
                raise RuntimeError(f"INSEE {r.status_code} ({commune['nom']}) : {r.text[:350]}")
            payload = r.json()
            batch = payload.get("etablissements", [])
            for etab in batch:
                period = latest_period(etab)
                # Historical periods can differ from current status/activity.
                if period.get("etatAdministratifEtablissement") != "A":
                    continue
                if period.get("caractereEmployeurEtablissement") != "O":
                    continue
                if period.get("activitePrincipaleEtablissement", "").upper() != naf:
                    continue
                if (etab.get("adresseEtablissement") or {}).get("codeCommuneEtablissement") != commune["code"]:
                    continue
                found[etab["siret"]] = etab
            next_cursor = payload.get("header", {}).get("curseurSuivant")
            if not batch or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        progress.progress((i + 1) / len(communes), text=f"Communes analysées : {i+1}/{len(communes)}")
    return list(found.values())


EFFECTIF_LABELS = {
    "00": "0 salarié", "01": "1 à 2 salariés", "02": "3 à 5 salariés",
    "03": "6 à 9 salariés", "11": "10 à 19 salariés",
    "12": "20 à 49 salariés", "21": "50 à 99 salariés",
    "22": "100 à 199 salariés", "31": "200 à 249 salariés",
    "32": "250 à 499 salariés", "41": "500 à 999 salariés",
    "42": "1 000 à 1 999 salariés", "51": "2 000 à 4 999 salariés",
    "52": "5 000 à 9 999 salariés", "53": "10 000 salariés et plus",
}
SELECTABLE_BANDS = {k: EFFECTIF_LABELS[k] for k in ("02", "03", "11", "12")}


def row_for(etab):
    period = latest_period(etab)
    unite = etab.get("uniteLegale") or {}
    addr = etab.get("adresseEtablissement") or {}
    tranche = period.get("trancheEffectifsEtablissement") or etab.get("trancheEffectifsEtablissement") or ""
    return {
        "SIRET": etab.get("siret", ""), "SIREN": etab.get("siren", ""),
        "Raison sociale": legal_name(unite),
        "Enseigne": etab.get("enseigne1Etablissement") or "",
        "Adresse": " ".join(filter(None, [addr.get("numeroVoieEtablissement"), addr.get("typeVoieEtablissement"), addr.get("libelleVoieEtablissement")])),
        "Code postal": addr.get("codePostalEtablissement") or "",
        "Commune": addr.get("libelleCommuneEtablissement") or "",
        "Code commune": addr.get("codeCommuneEtablissement") or "",
        "Code NAF": period.get("activitePrincipaleEtablissement") or "",
        "Code tranche effectif": tranche,
        "Tranche effectif": EFFECTIF_LABELS.get(tranche, "Non renseigné" if not tranche or tranche == "NN" else f"Autre / non précisé ({tranche})"),
        "Année effectif": etab.get("anneeEffectifsEtablissement") or "",
        "Employeur": period.get("caractereEmployeurEtablissement") or "",
        "Date création": etab.get("dateCreationEtablissement") or "",
        "Siège": "Oui" if etab.get("etablissementSiege") else "Non",
    }


# Enrichissement expérimental depuis l'annuaire régional de l'Ordre.
# À activer seulement avec une autorisation adaptée à l'extraction et à la réutilisation.
ANNUAIRE = "https://annuaire.oecnouvelle-aquitaine.fr/societes/"
PHONE_RE = re.compile(r"(?<!\d)(?:\+33[\s.\-]?(?:\(0\))?|0)[1-9](?:[\s.\-]?\d{2}){4}(?!\d)")


def normalize_name(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", value.upper()).split())


def normalize_phone(value):
    digits = re.sub(r"\D", "", value)
    if digits.startswith("33") and len(digits) == 11:
        digits = "0" + digits[2:]
    return " ".join(digits[i:i+2] for i in range(0, 10, 2)) if len(digits) == 10 else ""


def annuaire_url(commune):
    return ANNUAIRE + "?" + urlencode({"type": "societes", "nom": "", "ville": commune["nom"], "dep": commune.get("codeDepartement", "")})


def read_annuaire_commune(commune):
    # Ne contourne ni restrictions, ni CAPTCHA, ni contrôle d'accès.
    url = annuaire_url(commune)
    response = requests.get(url, headers={"User-Agent": "OutilsSiret/1.0 (consultation annuaire professionnel)"}, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    candidates = []
    # L'annuaire peut changer de structure : extraction prudente, sans
    # attribuer un téléphone à un cabinet sans correspondance nominative.
    for node in soup.find_all(string=PHONE_RE):
        phone_match = PHONE_RE.search(str(node))
        if not phone_match:
            continue
        phone = normalize_phone(phone_match.group(0))
        if not phone:
            continue
        parent = node.parent
        for _ in range(5):
            if parent is None:
                break
            text = " ".join(parent.stripped_strings)
            if 15 <= len(text) <= 700 and len(PHONE_RE.findall(text)) == 1:
                candidates.append((normalize_name(text), phone))
                break
            parent = parent.parent
    return candidates, url


def extract_experts_from_detail(url):
    """Extraction prudente : uniquement les noms présents dans une rubrique explicite.

    Le balisage du site de l'Ordre n'est pas contractuel. Une absence de résultat
    signifie « non vérifié », et non « aucun expert-comptable ».
    """
    response = requests.get(url, headers={"User-Agent": "OutilsSiret/1.1 (consultation annuaire professionnel)"}, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    names = []
    # Ne pas deviner des personnes à partir de tous les noms propres de la page.
    # On exige un intitulé explicite et une liste structurée adjacente.
    for heading in soup.find_all(["h2", "h3", "h4", "strong"]):
        heading_text = normalize_name(heading.get_text(" ", strip=True))
        if not any(term in heading_text for term in ("EXPERTS COMPTABLES", "EXPERT COMPTABLES", "EXPERTS COMPTABLE", "EXPERT COMPTABLE")):
            continue
        block = heading.find_next_sibling(["ul", "ol", "div"])
        if block is None:
            continue
        if block.name in ("ul", "ol"):
            items = block.find_all("li", recursive=False)
        else:
            items = block.find_all(["li"], recursive=True)
        for item in items:
            name = " ".join(item.stripped_strings).strip(" -•:")
            if 5 <= len(name) <= 90 and not PHONE_RE.search(name) and "@" not in name:
                names.append(name)
    return list(dict.fromkeys(names))


def enrich_phones(df, communes, progress):
    enriched = df.copy()
    for col in ("Téléphone professionnel", "Source téléphone", "Statut rapprochement",
                "Lien annuaire", "Experts-comptables", "Source experts", "Statut experts"):
        enriched[col] = ""
    by_code = {c["code"]: c for c in communes}
    caches = {}
    for i, (idx, row) in enumerate(enriched.iterrows()):
        code = str(row.get("Code commune", ""))
        commune = by_code.get(code)
        if not commune:
            enriched.at[idx, "Statut rapprochement"] = "Commune non disponible"
            enriched.at[idx, "Statut experts"] = "Commune non disponible"
            continue
        if code not in caches:
            try:
                caches[code] = read_annuaire_commune(commune)
            except requests.RequestException:
                caches[code] = ([], annuaire_url(commune))
            time.sleep(1.0)
        entries, url = caches[code]
        enriched.at[idx, "Lien annuaire"] = url
        names = [normalize_name(row.get("Raison sociale", "")), normalize_name(row.get("Enseigne", ""))]
        names = [n for n in names if len(n) >= 5]
        matched = set()
        for text, phone in entries:
            if any(n in text for n in names):
                matched.add(phone)
        if len(matched) == 1:
            enriched.at[idx, "Téléphone professionnel"] = next(iter(matched))
            enriched.at[idx, "Source téléphone"] = "Annuaire OEC Nouvelle-Aquitaine"
            enriched.at[idx, "Statut rapprochement"] = "Nom concordant – à vérifier"
        elif len(matched) > 1:
            enriched.at[idx, "Statut rapprochement"] = "Plusieurs numéros possibles – à vérifier"
        else:
            enriched.at[idx, "Statut rapprochement"] = "Non trouvé / non vérifié"
        # Les résultats de commune ne prouvent pas le rattachement des personnes.
        # La récupération des experts nécessite un lien vers la fiche société
        # confirmé ; ne pas attribuer des noms issus d'autres cabinets.
        enriched.at[idx, "Statut experts"] = "Fiche société à vérifier manuellement"
        progress.progress((i+1)/len(enriched))
    return enriched


def experts_sheet(df):
    rows = []
    for _, row in df.iterrows():
        names = [n.strip() for n in str(row.get("Experts-comptables", "")).split(";") if n.strip()]
        for name in names:
            rows.append({"Expert-comptable": name, "Cabinet": row.get("Raison sociale", ""),
                         "SIRET": row.get("SIRET", ""), "Commune": row.get("Commune", ""),
                         "Téléphone du cabinet": row.get("Téléphone professionnel", ""),
                         "Lien annuaire": row.get("Lien annuaire", ""),
                         "Statut vérification": row.get("Statut experts", "")})
    return pd.DataFrame(rows, columns=["Expert-comptable", "Cabinet", "SIRET", "Commune",
                                       "Téléphone du cabinet", "Lien annuaire", "Statut vérification"])


verification, recherche = st.tabs(["✅ Vérification SIRET", "🔎 Recherche d'établissements"])

with verification:
    st.subheader("Vérifier un fichier de SIRET")
    uploaded = st.file_uploader("📂 Importer un CSV avec une colonne 'siret'", type=["csv"])
    if uploaded is not None:
        try:
            source = pd.read_csv(uploaded, dtype=str, sep=None, engine="python", encoding="utf-8-sig")
            source.columns = source.columns.str.strip().str.lower()
            if "siret" not in source.columns:
                st.error("Le CSV doit contenir une colonne 'siret'.")
            else:
                sirets = source["siret"].dropna().tolist()
                st.info(f"{len(sirets)} SIRET détectés")
                if st.button("🚀 Lancer la vérification"):
                    results = []
                    bar = st.progress(0)
                    for i, raw in enumerate(sirets):
                        siret = "".join(c for c in str(raw) if c.isdigit())
                        statut, raison, typ = "", "", ""
                        if len(siret) != 14:
                            statut = "SIRET invalide"
                        else:
                            try:
                                r = call_api(f"{BASE}/{siret}", headers=HEADERS)
                                if r.status_code == 200:
                                    etab = r.json().get("etablissement") or {}
                                    etat = latest_period(etab).get("etatAdministratifEtablissement")
                                    statut = {"A": "Actif", "F": "Fermé"}.get(etat, "Inconnu")
                                    raison = legal_name(etab.get("uniteLegale"))
                                    typ = "Siège social" if etab.get("etablissementSiege") else "Établissement secondaire"
                                elif r.status_code == 404:
                                    statut = "Inexistant"
                                else:
                                    statut = f"Erreur API ({r.status_code})"
                            except RuntimeError as exc:
                                statut = str(exc)
                        results.append({"SIRET": siret, "Raison sociale": raison,
                                        "Type établissement": typ, "Statut": statut})
                        bar.progress((i + 1) / len(sirets))
                    st.session_state["verification_results"] = pd.DataFrame(results)
        except Exception as exc:
            st.error(f"Lecture du CSV impossible : {exc}")
    if "verification_results" in st.session_state:
        df = st.session_state["verification_results"]
        st.dataframe(df, use_container_width=True)
        st.download_button("📥 Télécharger les résultats (Excel)",
                           excel_bytes({"Résultats": df}, color_status=True),
                           "resultats_siret.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with recherche:
    st.subheader("🔎 Recherche d'établissements")
    naf = st.text_input("Code NAF / APE", value="69.20Z").strip().upper()
    zone = st.radio("Zone géographique", ["Bordeaux Métropole (28 communes)", "Une commune française"], horizontal=True)
    communes = []
    try:
        if zone.startswith("Bordeaux"):
            communes = metro_communes()
            st.caption(f"{len(communes)} communes récupérées auprès de l'API Géo officielle.")
        else:
            ville = st.text_input("Nom de la commune", value="Bordeaux").strip()
            options = find_communes(ville) if len(ville) >= 2 else []
            if options:
                selected = st.selectbox("Choisir la commune", options,
                                        format_func=lambda c: f"{c['nom']} ({c.get('codeDepartement', '')}) – {c['code']}")
                communes = [selected]
            else:
                st.info("Saisis au moins deux lettres pour rechercher une commune.")
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        st.error(f"Impossible de récupérer les communes : {exc}")
    st.write("**Statut :** actif uniquement · **Employeur :** oui uniquement")
    selected_bands = st.multiselect(
        "Tranches d'effectifs à inclure (plusieurs choix possibles)",
        options=list(SELECTABLE_BANDS),
        default=list(SELECTABLE_BANDS),
        format_func=lambda code: f"{SELECTABLE_BANDS[code]} (code {code})",
        help="Les tranches correspondent aux catégories publiées par l'INSEE, pas à un effectif exact.",
    )
    include_unknown = st.checkbox("Exporter les effectifs inconnus dans un onglet séparé", value=True)
    st.caption("Les effectifs publiés peuvent être anciens ou absents. Les établissements hors tranches sélectionnées ne figurent pas dans les résultats principaux.")
    st.caption("La recherche peut être longue pour une métropole entière.")
    if st.button("🚀 Rechercher les établissements", disabled=not communes or not naf or not selected_bands):
        bar = st.progress(0)
        try:
            establishments = fetch_establishments(naf, communes, bar)
            rows = [row_for(e) for e in establishments]
            st.session_state["recherche_resultats"] = {
                "rows": rows,
                "communes": communes,
                "naf": naf,
                "bands": list(selected_bands),
                "include_unknown": include_unknown,
            }
        except Exception as exc:
            st.error(f"Recherche interrompue : {exc}")
    if "recherche_resultats" in st.session_state:
        result = st.session_state["recherche_resultats"]
        rows = result["rows"]
        accepted = set(result["bands"])
        known = [r for r in rows if str(r["Code tranche effectif"]) in accepted]
        unknown = [r for r in rows if str(r["Code tranche effectif"]) in ("", "NN")]
        excluded = [r for r in rows if str(r["Code tranche effectif"]) not in accepted and str(r["Code tranche effectif"]) not in ("", "NN")]
        cols = list(row_for({}).keys())
        df_known = pd.DataFrame(known, columns=cols)
        df_unknown = pd.DataFrame(unknown, columns=cols)
        df_excluded = pd.DataFrame(excluded, columns=cols)
        st.success(f"{len(rows)} établissements actifs employeurs NAF {result['naf']} : {len(known)} dans les tranches choisies, {len(unknown)} à effectif inconnu, {len(excluded)} dans d'autres tranches connues.")
        st.write("**Résultats – tranches sélectionnées**")
        st.dataframe(df_known, use_container_width=True)
        st.divider()
        st.subheader("☎️ Téléphones et experts-comptables – annuaire de l’Ordre")
        st.caption("Disponible pour les communes de Nouvelle-Aquitaine. L'annuaire n'est pas une API publique documentée : cette fonction expérimentale peut cesser de fonctionner. Un nom concordant ne garantit pas l'identité du SIRET.")
        permission = st.checkbox("Je confirme disposer de l'autorisation nécessaire pour interroger automatiquement l'annuaire et réutiliser les coordonnées", key="oec_permission")
        if st.button("🔎 Enrichir les coordonnées (et préparer les experts-comptables)", disabled=not permission or df_known.empty):
            with st.spinner("Consultation des pages publiques de l'annuaire…"):
                try:
                    bar_phone = st.progress(0)
                    st.session_state["oec_enriched"] = enrich_phones(df_known, result["communes"], bar_phone)
                    st.session_state["oec_signature"] = (result["naf"], tuple(sorted(result["bands"])), tuple(sorted(c["code"] for c in result["communes"])))
                except Exception as exc:
                    st.error(f"Enrichissement interrompu : {exc}")
        signature = (result["naf"], tuple(sorted(result["bands"])), tuple(sorted(c["code"] for c in result["communes"])))
        if st.session_state.get("oec_signature") == signature and "oec_enriched" in st.session_state:
            df_known = st.session_state["oec_enriched"]
            st.dataframe(df_known[["Raison sociale", "Commune", "Téléphone professionnel", "Experts-comptables", "Statut rapprochement", "Statut experts", "Lien annuaire"]], use_container_width=True)
            st.info("Les téléphones proposés doivent être contrôlés. Les noms des experts-comptables nécessitent une fiche société confirmée : aucun rattachement n’est inventé.")
            st.caption("Tu peux compléter les noms confirmés sur les fiches de l’Ordre dans la colonne Experts-comptables (séparer plusieurs noms par un point-virgule).")
            edited = st.data_editor(df_known, use_container_width=True, hide_index=True,
                                    disabled=[c for c in df_known.columns if c not in ("Experts-comptables", "Statut experts")],
                                    key="edit_experts")
            df_known = edited.copy()
            st.session_state["oec_enriched"] = df_known
        st.write("**Répartition par tranche sélectionnée**")
        summary = pd.DataFrame([
            {"Tranche d'effectif": SELECTABLE_BANDS[code], "Code INSEE": code,
             "Établissements": sum(r["Code tranche effectif"] == code for r in known)}
            for code in result["bands"]
        ])
        st.dataframe(summary, hide_index=True, use_container_width=True)
        params = pd.DataFrame([{
            "NAF": result["naf"], "Commune": c["nom"], "Code INSEE commune": c["code"],
            "Tranches retenues": ", ".join(result["bands"]), "Statut": "Actif", "Employeur": "Oui"
        } for c in result["communes"]])
        sheets = {"Tranches sélectionnées": df_known, "Experts-comptables": experts_sheet(df_known), "Répartition": summary}
        if result["include_unknown"]:
            sheets["Effectifs inconnus"] = df_unknown
        sheets["Autres tranches connues"] = df_excluded
        sheets["Communes et paramètres"] = params
        st.download_button("📥 Télécharger l'extraction Excel", excel_bytes(sheets),
                           "extraction_insee_par_effectifs.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
