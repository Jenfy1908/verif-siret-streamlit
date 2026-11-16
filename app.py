import time
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
        "Tranche effectif": tranche, "Année effectif": etab.get("anneeEffectifsEtablissement") or "",
        "Employeur": period.get("caractereEmployeurEtablissement") or "",
        "Date création": etab.get("dateCreationEtablissement") or "",
        "Siège": "Oui" if etab.get("etablissementSiege") else "Non",
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
    st.write("**Effectifs :** tranche 10–19 salariés et toutes les tranches supérieures")
    st.info("La tranche 10–19 inclut 10 salariés exactement. Les effectifs inconnus sont exportés séparément.")
    st.caption("La recherche peut être longue pour une métropole entière. Les effectifs publiés peuvent être anciens.")
    if st.button("🚀 Rechercher les établissements", disabled=not communes or not naf):
        bar = st.progress(0)
        try:
            establishments = fetch_establishments(naf, communes, bar)
            rows = [row_for(e) for e in establishments]
            st.session_state["recherche_resultats"] = (rows, communes, naf)
        except Exception as exc:
            st.error(f"Recherche interrompue : {exc}")
    if "recherche_resultats" in st.session_state:
        rows, searched_communes, searched_naf = st.session_state["recherche_resultats"]
        # Sirene 10+ workforce bands. 11=10-19; 12=20-49; 21=50-99.
        accepted = {"11", "12", "21", "22", "31", "32", "41", "42", "51", "52", "53"}
        known = [r for r in rows if str(r["Tranche effectif"]) in accepted]
        unknown = [r for r in rows if str(r["Tranche effectif"]) not in accepted]
        cols = list(row_for({}).keys())
        df_known = pd.DataFrame(known, columns=cols)
        df_unknown = pd.DataFrame(unknown, columns=cols)
        st.success(f"{len(rows)} établissements actifs employeurs NAF {searched_naf} : {len(known)} dans les tranches 10+, {len(unknown)} à effectif inconnu ou inférieur.")
        st.caption("L'onglet 'À vérifier' contient aussi les tranches connues inférieures à 10, à ne pas confondre avec les effectifs inconnus.")
        st.dataframe(df_known, use_container_width=True)
        params = pd.DataFrame([{"NAF": searched_naf, "Commune": c["nom"], "Code INSEE": c["code"]} for c in searched_communes])
        st.download_button("📥 Télécharger l'extraction Excel", excel_bytes({"Établissements 10+": df_known, "À vérifier": df_unknown, "Communes et paramètres": params}),
                           "extraction_insee.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
