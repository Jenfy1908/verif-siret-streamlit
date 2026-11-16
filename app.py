import time
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse
from io import BytesIO
from urllib.parse import urlencode

import pandas as pd
import requests
import streamlit as st
from openpyxl.styles import PatternFill, Font
from bs4 import BeautifulSoup

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
            if "Annuaire de l’Ordre" in frame.columns:
                link_idx = list(frame.columns).index("Annuaire de l’Ordre") + 1
                for cells in ws.iter_rows(min_row=2):
                    cell = cells[link_idx - 1]
                    if isinstance(cell.value, str) and cell.value.startswith("https://"):
                        cell.hyperlink = cell.value
                        cell.font = Font(color="0563C1", underline="single")
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


# Correspondances vérifiées manuellement dans l'annuaire régional.
# Indexation par SIRET pour éviter les homonymies et les changements de nom.
FICHES_VERIFIEES = {
    "42318378900035": "https://annuaire.oecnouvelle-aquitaine.fr/societes/conseil-audit-expertise-comptable/",
    "43444560700030": "https://annuaire.oecnouvelle-aquitaine.fr/societes/ciron-et-associes/",
    "46720011900064": "https://annuaire.oecnouvelle-aquitaine.fr/societes/audit-conseil-synthese-expertise/",
    "53865241300039": "https://annuaire.oecnouvelle-aquitaine.fr/societes/vincent-guillemin/",
    "53867183500048": "https://annuaire.oecnouvelle-aquitaine.fr/societes/gestion-finance-analyse-et-expertise/",
    "75301022200036": "https://annuaire.oecnouvelle-aquitaine.fr/societes/sodarex-rive-droite/",
    "79009473400023": "https://annuaire.oecnouvelle-aquitaine.fr/societes/cabinet-christophe-guerin/",
    "81793867300032": "https://annuaire.oecnouvelle-aquitaine.fr/societes/3g-guillemin-associes/",
    "90875988900011": "https://annuaire.oecnouvelle-aquitaine.fr/societes/expertise-law/",
}


ANNUAIRE_BASE = "https://annuaire.oecnouvelle-aquitaine.fr"


def normalize_name(value):
    """Normalise les noms et reconstitue les sigles (S.A.E.C. -> saec)."""
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    # Recompose uniquement les suites de lettres isolées, sans fusionner
    # des mots ordinaires : « S.A.E.C. » devient « saec ».
    value = re.sub(
        r"(?<![a-z0-9])(?:[a-z]\s*[.\-]\s*){2,}[a-z](?![a-z0-9])",
        lambda match: re.sub(r"[^a-z]", "", match.group()),
        value,
    )
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [w for w in value.split() if w not in {
        "societe", "sarl", "sas", "selarl", "selas", "eurl", "sa", "sc",
        "et", "de", "d", "l", "la", "le", "les"
    }]
    return " ".join(words)


def directory_name_variants(label):
    """Retire seulement le suffixe numérique WordPress des URL de fiches."""
    names = {normalize_name(label)}
    without_suffix = re.sub(r"-?\s+\d+$", "", str(label))
    if without_suffix != label:
        names.add(normalize_name(without_suffix))
    return {name for name in names if name}


def match_score(insee_name, directory_name):
    """Ne rapproche pas des noms commerciaux approximatifs sans preuve."""
    if insee_name == directory_name:
        return 1.0
    return SequenceMatcher(None, insee_name, directory_name).ratio()


def annuaire_search_url(ville, departement="33"):
    """Construit une recherche sans modifier l'orthographe fournie."""
    query = urlencode({"type": "societes", "nom": "", "ville": ville, "dep": departement})
    return f"{ANNUAIRE_BASE}/societes/?{query}"


def annuaire_villes(ville):
    """Variantes observées dans l'annuaire pour Villenave-d'Ornon."""
    base = str(ville or "").strip()
    normalized = normalize_name(base)
    if normalized == "villenave ornon":
        variants = ["Villenave D Ornon", "Villenave D'ornon", "Villenave D Ornon Cedex"]
    else:
        # Conserver la forme INSEE et la forme sans ponctuation.
        plain = unicodedata.normalize("NFKD", base)
        plain = "".join(c for c in plain if not unicodedata.combining(c))
        plain = re.sub(r"[-'’]", " ", plain)
        variants = [base, " ".join(plain.split()).title()]
    return list(dict.fromkeys(variants))


def annuaire_url(siret, ville):
    return FICHES_VERIFIEES.get(str(siret), annuaire_search_url(annuaire_villes(ville)[0]))


@st.cache_data(ttl=86400, show_spinner=False)
def annuaire_candidates_variant(ville, departement):
    """Lit les liens publics d'une seule variante, sans visiter les fiches."""
    url = annuaire_search_url(ville, departement)
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; DirectoryLinkFinder/1.0)"}, timeout=12)
        response.raise_for_status()
    except requests.RequestException as exc:
        return [], f"{ville} : {exc}"
    soup = BeautifulSoup(response.text, "html.parser")
    found = {}
    for link in soup.select("a[href]"):
        href = urljoin(ANNUAIRE_BASE, link.get("href", ""))
        parsed = urlparse(href)
        if parsed.netloc != urlparse(ANNUAIRE_BASE).netloc:
            continue
        if not re.fullmatch(r"/societes/[^/]+/", parsed.path):
            continue
        label = parsed.path.strip("/").split("/")[-1].replace("-", " ")
        found[href] = label
    return [(label, href) for href, label in found.items()], ""


@st.cache_data(ttl=86400, show_spinner=False)
def annuaire_candidates(ville, departement):
    """Fusionne les résultats de toutes les variantes, sans doublon d'URL."""
    found = {}
    errors = []
    for variant in annuaire_villes(ville):
        matches, error = annuaire_candidates_variant(variant, departement)
        if error:
            errors.append(error)
        for label, href in matches:
            found[href] = label
    warning = " ; ".join(errors)
    if not found and not warning:
        warning = f"Aucune fiche individuelle détectée pour {ville}."
    return [(label, href) for href, label in found.items()], warning


def match_annuaire(rows, commune):
    candidates, warning = annuaire_candidates(commune['nom'], commune.get('codeDepartement', '33'))
    confirmed = 0
    for row in rows:
        if row['Code commune'] != commune['code']:
            continue
        siret = str(row['SIRET'])
        if siret in FICHES_VERIFIEES:
            row['Annuaire de l’Ordre'] = FICHES_VERIFIEES[siret]
            row['Type de lien Ordre'] = 'Fiche directe (validée manuellement)'
            confirmed += 1
            continue
        names = [normalize_name(row.get("Raison sociale")), normalize_name(row.get("Enseigne"))]
        names = [name for name in names if len(name) >= 5]
        scored = []
        for label, href in candidates:
            variants = directory_name_variants(label)
            score = max((match_score(name, variant)
                         for name in names for variant in variants), default=0.0)
            scored.append((score, href))
        scored.sort(reverse=True)
        # Correspondance unique, exacte ou très proche ; sinon lien de recherche.
        unique = bool(scored) and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.12)
        if scored and unique and (scored[0][0] == 1.0 or scored[0][0] >= 0.94):
            row["Annuaire de l’Ordre"] = scored[0][1]
            row["Type de lien Ordre"] = "Fiche directe (nom à contrôler)"
            confirmed += 1
        else:
            row["Annuaire de l’Ordre"] = annuaire_search_url(annuaire_villes(commune["nom"])[0], commune.get("codeDepartement", "33"))
            row["Type de lien Ordre"] = "Recherche par commune"
    return confirmed, warning


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
        "Annuaire de l’Ordre": annuaire_url(etab.get("siret", ""), addr.get("libelleCommuneEtablissement") or ""),
        "Type de lien Ordre": "Fiche directe (validée manuellement)" if str(etab.get("siret", "")) in FICHES_VERIFIEES else "Recherche par commune",
    }


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
    st.caption("La recherche INSEE reste indépendante. Après les résultats, un bouton permet de rechercher des liens directs vers les fiches publiques de l’Ordre.")
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
        if st.button("🔗 Rechercher les fiches directes de l’Ordre", help="Recherche distincte de l’INSEE. Peut dépendre de la disponibilité de l’annuaire."):
            count = 0
            warnings = []
            with st.spinner("Recherche des liens dans l’annuaire régional…"):
                for commune in result["communes"]:
                    matches, warning = match_annuaire(rows, commune)
                    count += matches
                    if warning:
                        warnings.append(warning)
            st.session_state["recherche_resultats"]["rows"] = rows
            st.success(f"{count} lien(s) direct(s) identifiés, dont les correspondances déjà validées.")
            for warning in warnings[:5]:
                st.warning(warning)
            if len(warnings) > 5:
                st.warning(f"{len(warnings)-5} autres communes sans résultats exploitables.")
        with st.expander("🛠️ Diagnostic de l’annuaire"):
            if st.button("Lancer le diagnostic de l’annuaire"):
                for commune in result["communes"]:
                    matches, warning = annuaire_candidates(commune["nom"], commune.get("codeDepartement", "33"))
                    for variant in annuaire_villes(commune["nom"]):
                        subset, issue = annuaire_candidates_variant(variant, commune.get("codeDepartement", "33"))
                        st.caption(f"{variant} : {len(subset)} fiche(s) trouvée(s)" + (f" — {issue}" if issue else ""))
                    st.write(f"**{commune['nom']}** : {len(matches)} fiches individuelles uniques détectées")
                    if warning:
                        st.warning(warning)
                    st.dataframe(pd.DataFrame(matches, columns=["Nom extrait de l’URL", "Fiche directe"]),
                                 use_container_width=True)
        st.caption("Les liens proposés automatiquement reposent sur une forte similitude de nom, mais doivent être contrôlés. Les autres ouvrent la liste de la commune.")
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
        st.dataframe(df_known, use_container_width=True,
                     column_config={"Annuaire de l’Ordre": st.column_config.LinkColumn(
                         "Annuaire de l’Ordre", display_text="🔗 Ouvrir l’annuaire")})
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
        sheets = {"Tranches sélectionnées": df_known, "Répartition": summary}
        if result["include_unknown"]:
            sheets["Effectifs inconnus"] = df_unknown
        sheets["Autres tranches connues"] = df_excluded
        sheets["Communes et paramètres"] = params
        st.download_button("📥 Télécharger l'extraction Excel", excel_bytes(sheets),
                           "extraction_insee_par_effectifs.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
