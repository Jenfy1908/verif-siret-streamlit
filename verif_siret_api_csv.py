import streamlit as st
import pandas as pd
import requests
import time

# ----------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------
API_KEY = "e8711ff2-f448-457b-b11f-f2f448d57b4e"
HEADERS = {"X-INSEE-Api-Key-Integration": API_KEY}
API_URL = "https://api.insee.fr/api-sirene/3.11/siret/"

st.set_page_config(page_title="Vérification SIRET", page_icon="🏢", layout="centered")

st.title("🏢 Vérification automatique de SIRET")
st.write("Chargez votre fichier **liste_siret.csv** puis lancez la vérification.")

uploaded_file = st.file_uploader("📂 Importer votre fichier CSV (colonne: siret)", type=["csv"])

if uploaded_file:
    df = pd.read_csv(uploaded_file, dtype=str)
    if "siret" not in df.columns:
        st.error("Le fichier doit contenir une colonne 'siret'.")
    else:
        siret_list = df["siret"].dropna().astype(str).tolist()
        st.success(f"{len(siret_list)} SIRET détectés ✅")

        if st.button("🚀 Lancer la vérification"):
            results = []
            progress_bar = st.progress(0)
            status = st.empty()

            for i, siret in enumerate(siret_list, start=1):
                siret = siret.strip()
                url = f"{API_URL}{siret}"

                while True:
                    response = requests.get(url, headers=HEADERS)
                    code = response.status_code

                    if code == 200:
                        data = response.json()
                        etat = data.get("etablissement", {}).get("periodesEtablissement", [{}])[0].get("etatAdministratifEtablissement", "INCONNU")
                        if etat == "A":
                            validite = "valide (actif)"
                        elif etat == "F":
                            validite = "fermé"
                        else:
                            validite = f"inconnu ({etat})"

                    elif code == 404:
                        validite = "inexistant"

                    elif code == 429:
                        status.warning("⚠️ Trop de requêtes — pause 15s...")
                        time.sleep(15)
                        continue

                    else:
                        validite = f"erreur ({code})"

                    results.append({"siret": siret, "validite": validite})
                    progress_bar.progress(i / len(siret_list))
                    status.text(f"{i}/{len(siret_list)} → {siret} → {validite}")
                    break

                time.sleep(0.3)

            df_result = pd.DataFrame(results)
            st.success("✅ Vérification terminée !")
            st.dataframe(df_result)

            csv = df_result.to_csv(index=False, sep=";").encode("utf-8")
            st.download_button(
                label="📥 Télécharger le fichier résultat",
                data=csv,
                file_name="resultats_siret.csv",
                mime="text/csv"
            )

else:
    st.info("Veuillez importer un fichier CSV pour commencer.")
