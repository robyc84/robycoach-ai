import datetime
import json
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from google import genai

# ==================== CONFIGURAZIONE PAGINA ====================
st.set_page_config(
    page_title="RobyCoach AI - Dashboard", page_icon="🚴", layout="wide"
)

# ==================== RECUPERO SECRETS / WHITELIST ====================
try:
  UTENTI_ABILITATI = st.secrets.get("UTENTI_ABILITATI", [])
  GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
except Exception:
  UTENTI_ABILITATI = []
  GEMINI_API_KEY = ""

# ==================== PERSISTENZA LOGIN DA URL (QUERY PARAMS) ====================
query_params = st.query_params

if "authenticated" not in st.session_state:
  st.session_state["authenticated"] = False
if "athlete_id" not in st.session_state:
  st.session_state["athlete_id"] = query_params.get("athlete_id", "")
if "api_key" not in st.session_state:
  st.session_state["api_key"] = query_params.get("api_key", "")

if st.session_state["athlete_id"] and st.session_state["api_key"]:
  id_check = st.session_state["athlete_id"].strip()
  if not UTENTI_ABILITATI or id_check in UTENTI_ABILITATI:
    st.session_state["authenticated"] = True

# ==================== SCHERMATA 1: LOGIN DEDICATA ====================
if not st.session_state["authenticated"]:
  st.title("🚴 RobyCoach AI")
  st.subheader("🔑 Accesso al Sistema")

  with st.form("form_login"):
    athlete_id_input = st.text_input(
        "Athlete ID (es. i12345)", value=st.session_state["athlete_id"]
    )
    api_key_input = st.text_input(
        "API Key Intervals", value=st.session_state["api_key"], type="password"
    )
    btn_login = st.form_submit_button("🚀 Accedi alla Dashboard", type="primary")

    if btn_login:
      id_pulito = athlete_id_input.strip()
      key_pulita = api_key_input.strip()

      if not id_pulito or not key_pulita:
        st.error("⚠️ Inserisci sia l'Athlete ID sia l'API Key!")
      elif UTENTI_ABILITATI and id_pulito not in UTENTI_ABILITATI:
        st.error("🚫 Utente non autorizzato!")
      else:
        st.session_state["authenticated"] = True
        st.session_state["athlete_id"] = id_pulito
        st.session_state["api_key"] = key_pulita

        st.query_params["athlete_id"] = id_pulito
        st.query_params["api_key"] = key_pulita

        st.success("Accesso eseguito con successo!")
        st.rerun()

  st.stop()

# ==================== COSTANTI E CONFIGURAZIONE UTENTE CONNESSO ====================
ATHLETE_ID = st.session_state["athlete_id"]
API_KEY = st.session_state["api_key"]
AUTH = ("API_KEY", API_KEY)
BASE_URL = f"https://intervals.icu/api/v1/athlete/{ATHLETE_ID}"

DIR_GENERALI = os.path.join("Allenamenti", "Generali")
DIR_PERSONALE = os.path.join("Allenamenti", "Utenti", ATHLETE_ID)
DIR_CHAT = os.path.join(DIR_PERSONALE, "Chat")
FILE_PROFILO = os.path.join(DIR_PERSONALE, "profilo_atleta.txt")

os.makedirs(DIR_GENERALI, exist_ok=True)
os.makedirs(DIR_PERSONALE, exist_ok=True)
os.makedirs(DIR_CHAT, exist_ok=True)

TIPI_SPORT_INTERVALS = {
    "Ciclismo / Bici (Ride)": "Ride",
    "Rulli / Virtual Ride (VirtualRide)": "VirtualRide",
    "Mountain Bike (MountainBikeRide)": "MountainBikeRide",
    "Gravel (GravelRide)": "GravelRide",
    "Corsa (Run)": "Run",
    "Camminata / Trekking (Walk / Hike)": "Walk",
    "Nuoto (Swim)": "Swim",
    "Pesistica / Palestra (WeightTraining)": "WeightTraining",
    "Allenamento Generico (Workout)": "Workout",
}

GIORNI_SETTIMANA_ITA = [
    "Lunedì",
    "Martedì",
    "Mercoledì",
    "Giovedì",
    "Venerdì",
    "Sabato",
    "Domenica",
]


def estrai_secondi(valore):
  if isinstance(valore, (int, float)):
    return valore
  elif isinstance(valore, dict):
    for chiave in ["secs", "seconds", "time", "s", "dur"]:
      if chiave in valore and isinstance(valore[chiave], (int, float)):
        return valore[chiave]
  return 0


def genera_nome_univoco(directory, nome_desiderato, estensione_default=".zwo"):
  # Rimuove/Sostituisce i caratteri non validi nei file di sistema (es. '/' in '_')
  nome_pulito = re.sub(r'[\\/*?:"<>|]', "_", nome_desiderato)

  nome_base, estensione = os.path.splitext(nome_pulito)
  if not estensione:
    estensione = estensione_default

  percorso_completo = os.path.join(directory, f"{nome_base}{estensione}")
  if not os.path.exists(percorso_completo):
    return f"{nome_base}{estensione}"

  counter = 1
  while True:
    nuovo_nome = f"{nome_base}_{counter}{estensione}"
    percorso_tentativo = os.path.join(directory, nuovo_nome)
    if not os.path.exists(percorso_tentativo):
      return nuovo_nome
    counter += 1


def estrai_descrizione_da_zwo(contenuto_xml):
  """Estrae una descrizione sintetica leggibile in testo dal file XML .zwo"""
  try:
    root = ET.fromstring(contenuto_xml)
    desc_elem = root.find("description")
    if desc_elem is not None and desc_elem.text and desc_elem.text.strip():
      return desc_elem.text.strip()

    workout_node = root.find("workout")
    if workout_node is None:
      return ""

    passi = []
    for elem in workout_node:
      tag = elem.tag
      dur = elem.get("Duration", "0")
      p_on = elem.get("Power", elem.get("PowerOn", "0"))
      p_off = elem.get("PowerOff", "0")
      reps = elem.get("Repeat", "1")

      try:
        p_on_pct = int(float(p_on) * 100)
        p_off_pct = int(float(p_off) * 100)
      except ValueError:
        p_on_pct, p_off_pct = 0, 0

      if tag == "Warmup":
        passi.append(f"Warmup: {dur}s @ {p_on_pct}-{p_off_pct}% FTP")
      elif tag == "Cool-down" or tag == "Cooldown":
        passi.append(f"Cooldown: {dur}s @ {p_on_pct}-{p_off_pct}% FTP")
      elif tag == "SteadyState":
        cad = elem.get("Cadence")
        cad_str = f" {cad}rpm" if cad else ""
        passi.append(f"Steady: {dur}s @ {p_on_pct}% FTP{cad_str}")
      elif tag == "Ramp":
        passi.append(f"Ramp: {dur}s @ {p_on_pct}-{p_off_pct}% FTP")
      elif tag == "IntervalsT":
        on_dur = elem.get("OnDuration", dur)
        off_dur = elem.get("OffDuration", "0")
        passi.append(
            f"Rep: {reps}x({on_dur}s @ {p_on_pct}% / {off_dur}s @"
            f" {p_off_pct}%)"
        )

    return " | ".join(passi) if passi else "Scheda ZWO personalizzata"
  except Exception:
    return "Scheda ZWO personalizzata"


# ==================== FUNZIONI API INTERVALS ====================
def get_fitness_data():
  oggi = datetime.date.today()
  un_mese_fa = oggi - datetime.timedelta(days=30)
  url = f"{BASE_URL}/wellness?oldest={un_mese_fa.isoformat()}&newest={oggi.isoformat()}"
  res = requests.get(url, auth=AUTH)
  if res.status_code == 200 and res.json():
    last = res.json()[-1]
    raw_ctl = last.get("ctl", 0)
    raw_atl = last.get("atl", 0)

    ctl = (
        round(float(raw_ctl), 2)
        if isinstance(raw_ctl, (int, float))
        else "N/A"
    )
    atl = (
        round(float(raw_atl), 2)
        if isinstance(raw_atl, (int, float))
        else "N/A"
    )

    if isinstance(ctl, (int, float)) and isinstance(atl, (int, float)):
      tsb = round(ctl - atl, 2)
    else:
      tsb = "N/A"

    return ctl, atl, tsb
  return "N/A", "N/A", "N/A"


@st.cache_data(ttl=1800)
def get_wellness_history(giorni=60):
  oggi = datetime.date.today()
  inizio = oggi - datetime.timedelta(days=giorni)
  url = f"{BASE_URL}/wellness?oldest={inizio.isoformat()}&newest={oggi.isoformat()}"
  res = requests.get(url, auth=AUTH)
  if res.status_code == 200 and res.json():
    df = pd.DataFrame(res.json())
    if "id" in df.columns:
      df.rename(columns={"id": "date"}, inplace=True)
    df["ctl"] = pd.to_numeric(df.get("ctl"), errors="coerce").round(2)
    df["atl"] = pd.to_numeric(df.get("atl"), errors="coerce").round(2)
    df["tsb"] = (df["ctl"] - df["atl"]).round(2)
    return df
  return pd.DataFrame()


@st.cache_data(ttl=1800)
def get_intervals_profile(athlete_id, api_key):
  url = f"https://intervals.icu/api/v1/athlete/{athlete_id}"
  res = requests.get(url, auth=("API_KEY", api_key))
  if res.status_code == 200:
    data = res.json()

    ftp = data.get("icu_ftp")
    max_hr = data.get("icu_max_hr") or data.get("max_hr")
    resting_hr = data.get("icu_resting_hr") or data.get("resting_hr")
    weight = data.get("weight")

    sport_settings = data.get("sportSettings", [])
    if isinstance(sport_settings, list) and len(sport_settings) > 0:
      first_sport = (
          sport_settings[0] if isinstance(sport_settings[0], dict) else {}
      )
      if not ftp:
        ftp = first_sport.get("ftp")
      if not max_hr:
        max_hr = first_sport.get("max_hr")

    return {
        "name": data.get("name") or athlete_id,
        "ftp": ftp if ftp else "N/A",
        "weight": weight if weight else "N/A",
        "max_hr": max_hr if max_hr else "N/A",
        "resting_hr": resting_hr if resting_hr else "N/A",
    }
  return {}


def recupera_dettaglio_attivita(
    act_summary, auth_params, richiede_dettaglio_step=False
):
  act_id = act_summary.get("id")
  if not act_id or act_summary.get("type") == "Note":
    return None

  if richiede_dettaglio_step:
    try:
      resp_det = requests.get(
          f"https://intervals.icu/api/v1/activity/{act_id}?intervals=true",
          auth=auth_params,
          timeout=3,
      )
      act = resp_det.json() if resp_det.status_code == 200 else act_summary
    except Exception:
      act = act_summary
  else:
    act = act_summary

  data = act.get("start_date_local", "").split("T")[0]
  nome = act.get("name") or "Attività"
  tipo = act.get("type") or "Ride"
  durata_m = round(
      (
          act.get("moving_time")
          or act.get("elapsed_time")
          or act.get("icu_recording_time")
          or 0
      )
      / 60,
      1,
  )
  tss = (
      act.get("icu_training_load")
      or act.get("icu_load")
      or act.get("load")
      or 0
  )
  np_w = act.get("icu_weighted_avg_watts") or "N/A"
  avg_hr = act.get("average_heartrate", 0)

  kcal = act.get("calories") or act.get("icu_calories") or "N/A"
  joules_kj = (
      round(act.get("icu_joules") / 1000, 1) if act.get("icu_joules") else "N/A"
  )

  p_zones = [
      round(estrai_secondi(s) / 60, 1)
      for s in (act.get("icu_zone_times") or act.get("zone_times") or [])
  ]
  hr_zones = [
      round(estrai_secondi(s) / 60, 1)
      for s in (act.get("hr_zone_times") or act.get("icu_hr_zone_times") or [])
  ]

  dettaglio_step = ""
  workout_text = act.get("icu_workout_text") or act.get("description")

  if workout_text:
    dettaglio_step = workout_text.replace("\n", " | ").strip()

  # Se manca il testo diretto dall'attività, controlliamo l'evento accoppiato da calendario
  if not dettaglio_step and act.get("paired_event_id"):
    try:
      ev_id = act.get("paired_event_id")
      resp_ev = requests.get(
          f"https://intervals.icu/api/v1/events/{ev_id}",
          auth=auth_params,
          timeout=2,
      )
      if resp_ev.status_code == 200:
        ev_data = resp_ev.json()
        ev_desc = ev_data.get("description") or ev_data.get(
            "workout_doc", {}
        ).get("caption")
        if ev_desc:
          dettaglio_step = ev_desc.replace("\n", " | ").strip()
    except Exception:
      pass

  # Fallback da intervalli eseguiti se disponibili
  if (
      not dettaglio_step
      and "icu_intervals" in act
      and isinstance(act["icu_intervals"], list)
  ):
    passi = []
    for step in act["icu_intervals"]:
      dur_s = step.get("elapsed_time", 0)
      w_avg = step.get("average_watts", 0)
      label = step.get("label") or step.get("type") or "Step"
      if dur_s > 0:
        passi.append(f"{dur_s}s@{w_avg}W ({label})")
    if passi:
      dettaglio_step = " -> ".join(passi[:8])

  # Fallback dinamico basato sulle zone di potenza
  if not dettaglio_step:
    if p_zones and len(p_zones) >= 5:
      z4_min = p_zones[3]
      z5_plus = sum(p_zones[4:])

      if z4_min >= 8 and z4_min <= 15:
        dettaglio_step = (
            f"Lavoro Specifico / SFR - Tempo/Soglia (Z4: {z4_min:.1f}m)"
        )
      elif z5_plus > 5:
        dettaglio_step = (
            f"Workout Alta Intensità / VO2Max (Z5+: {z5_plus:.1f}m)"
        )
      elif p_zones[1] > 15:
        dettaglio_step = (
            f"Lavoro Aerobico / Fondo Z2 (Tempo Z2: {p_zones[1]:.1f}m)"
        )
      else:
        dettaglio_step = "Sessione Libera / Non specificata"
    else:
      dettaglio_step = "Sessione Libera / Non specificata"

  stringa_txt = (
      f"[{data}] {nome} ({tipo}) | Struttura Step: {dettaglio_step} | Durata:"
      f" {durata_m}m | TSS: {tss} | NP: {np_w}W | FC Med: {avg_hr} bpm | Kcal:"
      f" {kcal} | kJ: {joules_kj} | ZoneW(min): {p_zones} | ZoneHR(min):"
      f" {hr_zones}"
  )

  dizionario_dati = {
      "Data": data,
      "Nome Attività": nome,
      "Tipo": tipo,
      "Durata (min)": durata_m,
      "TSS / Load": tss,
      "NP (Watt)": np_w,
      "FC Media (bpm)": avg_hr if avg_hr else "N/A",
      "Kcal": kcal,
      "Lavoro (kJ)": joules_kj,
      "Zone Potenza (min)": str(p_zones) if p_zones else "N/A",
      "Zone FC (min)": str(hr_zones) if hr_zones else "N/A",
      "Struttura / Step Svolti": dettaglio_step,
  }

  return stringa_txt, dizionario_dati


@st.cache_data(ttl=1800)
def genera_report_txt_veloce(giorni, athlete_id, api_key):
  auth_params = ("API_KEY", api_key)
  base_url = f"https://intervals.icu/api/v1/athlete/{athlete_id}"
  oggi = datetime.date.today()
  data_inizio = oggi - datetime.timedelta(days=giorni)

  res = requests.get(
      f"{base_url}/activities",
      params={"oldest": data_inizio.isoformat(), "newest": oggi.isoformat()},
      auth=auth_params,
  )
  if res.status_code != 200:
    return None, [], f"Errore API ({res.status_code})"

  lista_attivita = res.json()
  resp_w = requests.get(
      f"{base_url}/wellness",
      params={"oldest": data_inizio.isoformat(), "newest": oggi.isoformat()},
      auth=auth_params,
  )
  wellness_data = resp_w.json() if resp_w.status_code == 200 else []

  ultimo_w = wellness_data[-1] if wellness_data else {}
  raw_ctl = ultimo_w.get("ctl", 0)
  raw_atl = ultimo_w.get("atl", 0)

  ctl_v = (
      round(float(raw_ctl), 2)
      if isinstance(raw_ctl, (int, float))
      else "N/A"
  )
  atl_v = (
      round(float(raw_atl), 2)
      if isinstance(raw_atl, (int, float))
      else "N/A"
  )
  tsb_v = (
      round(ctl_v - atl_v, 2)
      if isinstance(ctl_v, (int, float)) and isinstance(atl_v, (int, float))
      else "N/A"
  )

  giorno_settimana = GIORNI_SETTIMANA_ITA[oggi.weekday()]
  data_formattata = oggi.strftime("%d/%m/%Y")

  report_txt = [
      f"DATA ATTUALE: {giorno_settimana} {data_formattata}",
      f"STATO DI FORMA ATTUALE: CTL={ctl_v:.2f}, ATL={atl_v:.2f},"
      f" TSB={tsb_v:.2f}",
      "STORICO ULTIME ATTIVITÀ CON DETTAGLIO STEP:",
  ]

  lista_tabella = []
  num_totale = len(lista_attivita)

  with ThreadPoolExecutor(max_workers=10) as executor:
    futures = [
        executor.submit(
            recupera_dettaglio_attivita,
            act,
            auth_params,
            idx >= (num_totale - 3),
        )
        for idx, act in enumerate(lista_attivita)
    ]
    risultati = [f.result() for f in futures]

  for r in risultati:
    if r:
      txt, diz = r
      report_txt.append(txt)
      lista_tabella.append(diz)

  return "\n".join(report_txt), lista_tabella, None


def chiama_gemini_safe(client, prompt_o_chat):
  modelli_fallback = [
      "gemini-3.5-flash-lite",
      "gemini-3.1-flash-lite",
      "gemini-2.5-flash-lite",
      "gemini-2.5-flash",
      "gemini-3.8-flash",
  ]
  ultimo_errore = None

  for modello in modelli_fallback:
    try:
      return client.models.generate_content(
          model=modello, contents=prompt_o_chat
      )
    except Exception as e:
      ultimo_errore = e
      continue

  raise ultimo_errore


# ==================== HEADER HOME & TASTO LOGOUT ====================
col_title, col_logout = st.columns([3, 1])
with col_title:
  st.title("🚴 RobyCoach AI")
  st.caption(f"Connesso come: **{ATHLETE_ID}**")

with col_logout:
  st.write("")
  if st.button("🚪 Logout", type="secondary"):
    st.session_state["authenticated"] = False
    st.session_state["athlete_id"] = ""
    st.session_state["api_key"] = ""
    st.session_state["messages_history"] = []
    st.session_state["lista_attivita_inviate"] = []

    st.query_params.clear()
    st.rerun()

st.divider()

# ==================== STRUTTURA A MENU / TAB ====================
tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Stato di Forma & AI Coach",
    "📤 Programmazione & Gestione .ZWO",
    "⚙️ Profilo & Integrazioni",
    "📜 Storico Conversazioni",
])

# ==================== TAB 1: STATO DI FORMA & AI COACH ====================
with tab1:
  st.header("📊 Il Tuo Stato di Forma")
  ctl, atl, tsb = get_fitness_data()

  if ctl == "N/A":
    st.error("Impossibile recuperare i dati. Verifica le credenziali.")
  else:
    col1, col2, col3 = st.columns(3)

    val_ctl_str = f"{ctl:.2f}" if isinstance(ctl, (int, float)) else str(ctl)
    val_atl_str = f"{atl:.2f}" if isinstance(atl, (int, float)) else str(atl)
    val_tsb_str = f"{tsb:.2f}" if isinstance(tsb, (int, float)) else str(tsb)

    with col1:
      c_val, c_help = st.columns([3, 1])
      with c_val:
        st.metric("CTL (Fitness)", val_ctl_str)
      with c_help:
        with st.popover("❓"):
          st.markdown("""
                    **CTL (Chronic Training Load) - Forma Fisica**
                    * Media ponderata del carico degli **ultimi 42 giorni**.
                    * Rappresenta la tua base atletica e la resistenza accumulata nel tempo.

                    ---
                    **📊 Fasce di Riferimento CTL:**
                    * **< 30:** *Principiante / Ripresa.* Base aerobica ridotta o post infortunio/pausa.
                    * **30 - 60:** *Amatore Ricreativo.* Buona forma per uscite domenicali e rulli regolari.
                    * **60 - 90:** *Amatore Avanzato.* Solida base per granfondo, marathon MTB o uscite impegnative.
                    * **> 90:** *Agonista / Elite.* Carichi di allenamento elevati e capacità lavorativa notevole.
                    """)

    with col2:
      a_val, a_help = st.columns([3, 1])
      with a_val:
        st.metric("ATL (Fatica)", val_atl_str)
      with a_help:
        with st.popover("❓"):
          st.markdown("""
                    **ATL (Acute Training Load) - Fatica**
                    * Media ponderata del carico degli **ultimi 7 giorni**.
                    * Indica lo stress e la stanchezza a breve termine accumulati di recente.

                    ---
                    **📊 Fasce di Riferimento ATL:**
                    * **< 20:** *Fatica Molto Bassa.* Fase di scarico o riposo prolungato.
                    * **20 - 50:** *Fatica Moderata.* Carico di allenamento gestibile senza stress eccessivo.
                    * **50 - 90:** *Fatica Elevata.* Blocco di allenamento intenso; necessario monitorare il recupero.
                    * **> 90:** *Fatica Critica.* Rischio imminente di sovrallenamento se prolungata per più giorni.
                    """)

    with col3:
      t_val, t_help = st.columns([3, 1])
      with t_val:
        st.metric("TSB (Freschezza)", val_tsb_str)
      with t_help:
        with st.popover("❓"):
          st.markdown("""
                    **TSB (Training Stress Balance) - Freschezza / Form**
                    * Calcolato come: **CTL - ATL**
                    * Indica la prontezza del tuo corpo a performare oggi.
                    """)

    if isinstance(tsb, (int, float)):
      st.markdown("#### 🎯 Posizionamento Zona TSB (Freschezza)")

      st.markdown(
          "<div style='text-align: center; font-size: 13px; font-weight: 500;"
          " margin-top: 5px; margin-bottom: 15px;'>"
          "<span style='color: #EF5350;'>🔴 &lt;-30 Overtraining</span> &nbsp;|&nbsp; "
          "<span style='color: #66BB6A;'>🟢 -30..-10 Carico</span> &nbsp;|&nbsp; "
          "<span style='color: #42A5F5;'>🔵 -10..+5 Neutra</span> &nbsp;|&nbsp; "
          "<span style='color: #FFA726;'>🟠 +5..+20 Freschezza</span> &nbsp;|&nbsp; "
          "<span style='color: #AB47BC;'>🟣 &gt;+20 Riposo</span>"
          "</div>",
          unsafe_allow_html=True,
      )

      if tsb < -30:
        zona_sintetica = "🚨 Overload / Overtraining"
        colore_badge = "red"
        sintesi_status = (
            "⚠️ **Situazione:** La tua fatica recente supera nettamente la tua"
            " base di fitness.\n👉 **Raccomandazione:** **RIPOSO TOTALE o Scarico"
            " Attivo brevissimo in Z1 (< 50% FTP).** Evita assolutamente lavori"
            " ad alta intensità oggi per prevenire infortuni."
        )
      elif -30 <= tsb < -10:
        zona_sintetica = "🔥 Carico Ottimale"
        colore_badge = "green"
        sintesi_status = (
            "💪 **Situazione:** Stai stimolando adeguatamente gli adattamenti"
            " muscolari e la forma è in crescita.\n👉 **Raccomandazione:**"
            " **ALLENATI SU SPECIFICA.** Oggi puoi sostenere una sessione"
            " allenante dura (es. SFR/Salita, VO2Max o Medio)."
        )
      elif -10 <= tsb < 5:
        zona_sintetica = "⚖️ Mantenimento"
        colore_badge = "blue"
        sintesi_status = (
            "⚖️ **Situazione:** Carico e stanchezza sono in perfetto"
            " equilibrio.\n👉 **Raccomandazione:** **ALLENAMENTO MODERATO / Z2"
            " FONDO.** Ideale per un'uscita di resistenza aerobica o un lavoro"
            " di agilità/mantenimento senza affaticare troppo le gambe."
        )
      elif 5 <= tsb <= 20:
        zona_sintetica = "🚀 Freschezza / Gara"
        colore_badge = "orange"
        sintesi_status = (
            "⚡ **Situazione:** La fatica è scesa lasciando le gambe fresche e"
            " pronte alla massima prestazione.\n👉 **Raccomandazione:** **TEST"
            " FTP / GARA / INTENSITÀ MASSIMA.** Ideale per fare un test di"
            " potenza, sprint, segmenti Strava o un allenamento ad altissima"
            " intensità."
        )
      else:
        zona_sintetica = "💤 Perdita di Forma"
        colore_badge = "violet"
        sintesi_status = (
            "💤 **Situazione:** Ti stai riposando da diversi giorni e stai"
            " iniziando a perdere adattamento atletico.\n👉 **Raccomandazione:**"
            " **RIPRENDI AD ALLENARTI.** È momento di rimettere in moto le"
            " gambe con una sessione di riattivazione a ritmo Z2 o un blocco"
            " progressivo."
        )

      tsb_val = max(-40, min(30, tsb))
      fig_gauge = go.Figure(
          go.Indicator(
              mode="gauge+number",
              value=tsb_val,
              number={"valueformat": ".2f"},
              domain={"x": [0, 1], "y": [0, 1]},
              gauge={
                  "axis": {"range": [-40, 30], "tickwidth": 1},
                  "bar": {"color": "#111111", "thickness": 0.25},
                  "steps": [
                      {"range": [-40, -30], "color": "#EF5350"},
                      {"range": [-30, -10], "color": "#66BB6A"},
                      {"range": [-10, 5], "color": "#42A5F5"},
                      {"range": [5, 20], "color": "#FFA726"},
                      {"range": [20, 30], "color": "#AB47BC"},
                  ],
              },
          )
      )

      fig_gauge.update_layout(
          height=230, margin=dict(l=30, r=30, t=35, b=10), template="plotly_white"
      )
      st.plotly_chart(fig_gauge, use_container_width=True)

      st.caption(
          f"Zona Rilevata: **:{colore_badge}[{zona_sintetica}]** (Valore:"
          f" {tsb:.2f})"
      )

      st.info(f"📌 **Sintesi & Raccomandazione Giornaliera:**\n\n{sintesi_status}")

      with st.expander("📖 Legenda Dettagliata Zone TSB"):
        st.markdown("""
                | Valore TSB | Zona | Descrizione |
                |---|---|---|
                | **< -30** | 🚨 Overload / Overtraining | Fatica critica. Rischio infortuni o esaurimento. |
                | **-30 a -10** | 🔥 Carico Ottimale | Zona ideale per stimolare l'adattamento muscolare. |
                | **-10 a +5** | ⚖️ Mantenimento | Bilanciamento tra fatica e forma. |
                | **+5 a +20** | 🚀 Freschezza / Gara | Gambe riposate. Ideale per test, gare e uscite veloci. |
                | **> +20** | 💤 Perte di Forma | Riposo troppo lungo, perdita progressiva di CTL. |
                """)

    st.divider()
    st.subheader("📈 Andamento Storico di Forma e Fatica")
    giorni_grafico = st.selectbox(
        "Seleziona periodo grafico:",
        [30, 60, 90],
        index=1,
        key="select_days_graph",
    )

    df_wellness = get_wellness_history(giorni_grafico)
    if not df_wellness.empty and "date" in df_wellness.columns:
      fig = go.Figure()

      fig.add_trace(
          go.Scatter(
              x=df_wellness["date"],
              y=df_wellness["ctl"],
              mode="lines",
              name="CTL (Fitness)",
              line=dict(color="#29B6F6", width=3),
          )
      )

      fig.add_trace(
          go.Scatter(
              x=df_wellness["date"],
              y=df_wellness["atl"],
              mode="lines",
              name="ATL (Fatica)",
              line=dict(color="#FF7043", width=2, dash="dash"),
          )
      )

      fig.add_trace(
          go.Scatter(
              x=df_wellness["date"],
              y=df_wellness["tsb"],
              mode="lines",
              name="TSB (Freschezza)",
              line=dict(color="#66BB6A", width=2),
          )
      )

      fig.update_layout(
          margin=dict(l=20, r=20, t=30, b=20),
          legend=dict(
              orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1
          ),
          xaxis_title="Data",
          yaxis_title="Punti Load / Fitness",
          hovermode="x unified",
          template="plotly_white",
      )

      st.plotly_chart(fig, use_container_width=True)
    else:
      st.info("Dati storici per il grafico non disponibili.")

  st.divider()

  st.header("🤖 RobyCoach AI Advisor & Generator")
  giorni_analisi = st.slider(
      "Giorni di storico da analizzare per l'AI:", 7, 60, 30, step=1
  )

  if st.button("🧠 Analizza Stato di Forma", type="primary"):
    if not GEMINI_API_KEY:
      st.error(
          "⚠️ Chiave API Gemini non trovata nei Secrets. Assicurati che sia"
          " definita la variabile `GEMINI_API_KEY`."
      )
    else:
      with st.spinner("Analisi dello storico in corso..."):
        testo_storico, lista_tabella, err = genera_report_txt_veloce(
            giorni_analisi, ATHLETE_ID, API_KEY
        )

        if err:
          st.error(err)
        else:
          st.session_state["lista_attivita_inviate"] = lista_tabella

          profile_data = get_intervals_profile(ATHLETE_ID, API_KEY)
          note_salvate = ""
          if os.path.exists(FILE_PROFILO):
            with open(FILE_PROFILO, "r", encoding="utf-8") as f:
              note_salvate = f.read().strip()

          oggi_dt = datetime.date.today()
          giorno_ita = GIORNI_SETTIMANA_ITA[oggi_dt.weekday()]
          data_ita = oggi_dt.strftime("%d/%m/%Y")

          str_dati_atleta = f"""
OGGI È: {giorno_ita} {data_ita}

PARAMETRI FISICI SINCRO E PROFILO ATLETA:
- Nome/ID: {profile_data.get('name', ATHLETE_ID)}
- FTP Corrente: {profile_data.get('ftp', 'N/A')} W
- Frequenza Cardiaca Max: {profile_data.get('max_hr', 'N/A')} bpm
- Frequenza Cardiaca a Riposo: {profile_data.get('resting_hr', 'N/A')} bpm
- Peso (da sistema/note): {profile_data.get('weight', 'N/A')} kg
"""
          str_note = (
              f"\nNOTE E PREFERENZE PERSONALI DELL'ATLETA:\n{note_salvate}\n"
              if note_salvate
              else ""
          )

          prompt_iniziale = f"""
Sei RobyCoach AI, un preparatore atletico esperto specializzato in ciclismo e MTB.
{str_dati_atleta}
{str_note}

Ecco lo storico recente delle attività dell'atleta estratto da Intervals.icu (inclusivo di dettaglio step/intervalli delle ultime sessioni):
{testo_storico}

Sulla base dei parametri fisici (FTP, FC, Peso), delle note personali, dello storico e dei dettagli degli step svolti:
1. Fornisci un commento dettagliato sullo stato di forma corrente (CTL, ATL, TSB con precisione a 2 decimali).
2. Analizza l'andamento recente dei carichi, del tipo di stimoli energetici già effettuati (es. VO2Max, Soglia, Z2) e del recupero.
3. Fornisci raccomandazioni generali di preparazione per i prossimi giorni, indicando quali stimoli alternare rispetto alle ultime sessioni svolte.
"""

          try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = chiama_gemini_safe(client, prompt_iniziale)

            st.session_state["messages_history"] = [
                {"role": "user", "text": prompt_iniziale},
                {"role": "model", "text": response.text},
            ]
            st.rerun()

          except Exception as e:
            st.error(f"Errore durante la chiamata a Gemini: {e}")

  # --- TABELLA ATTIVITÀ ---
  if (
      "lista_attivita_inviate" in st.session_state
      and st.session_state["lista_attivita_inviate"]
  ):
    with st.expander(
        f"📋 Mostra Attività Prelevate da Intervals.icu ({len(st.session_state['lista_attivita_inviate'])} sessioni)",
        expanded=True,
    ):
      df_attivita = pd.DataFrame(st.session_state["lista_attivita_inviate"])
      st.dataframe(df_attivita, use_container_width=True, hide_index=True)

  if (
      "messages_history" in st.session_state
      and st.session_state["messages_history"]
  ):
    ultimo_msg_ai = [
        m["text"]
        for m in st.session_state["messages_history"]
        if m["role"] == "model"
    ][-1]

    st.markdown("### 📋 Consiglio e Risposta del Coach")
    testo_pulito = re.sub(
        r"```xml\s*.*?\s*```", "", ultimo_msg_ai, flags=re.DOTALL
    )
    st.write(testo_pulito.strip())

    xml_match = re.search(r"```xml\s*(.*?)\s*```", ultimo_msg_ai, re.DOTALL)

    if xml_match:
      codice_xml = xml_match.group(1).strip()
      nome_match = re.search(r"<name>(.*?)</name>", codice_xml)
      nome_suggerito = (
          nome_match.group(1).replace(" ", "_")
          if nome_match
          else f"Workout_AI_{datetime.date.today().strftime('%Y%m%d')}"
      )

      st.markdown("---")
      st.subheader("⚙️ Workout Proposto (.zwo)")
      with st.expander("🔍 Mostra Anteprima Codice XML .zwo"):
        st.code(codice_xml, language="xml")

      num_msg = len(st.session_state["messages_history"])

      col_nome, col_btn = st.columns([2, 1])
      with col_nome:
        nome_file_input = st.text_input(
            "Nome del file da salvare:",
            value=f"{nome_suggerito}.zwo",
            key=f"input_filename_{num_msg}",
        )

      with col_btn:
        st.write(" ")
        if st.button(
            "💾 Accetta e Salva in Libreria",
            type="primary",
            key=f"btn_save_{num_msg}",
        ):
          nome_finale = genera_nome_univoco(DIR_PERSONALE, nome_file_input)
          percorso_salvataggio = os.path.join(DIR_PERSONALE, nome_finale)

          with open(percorso_salvataggio, "w", encoding="utf-8") as f:
            f.write(codice_xml)

          st.success(
              f"✅ Salvato con successo in Libreria Personale: **{nome_finale}**!"
          )
          st.rerun()

    st.markdown("---")
    st.subheader("💬 Parla con RobyCoach AI")

    suggerimenti = [
        "Proponi un workout",
        "Generami un .zwo alternativo di tipo VO2Max",
        "Riduci la durata totale a 45 minuti",
        "Spiegami meglio come eseguire il riscaldamento",
        "Vorrei un allenamento per fare scarico attivo",
    ]

    cols_suggerimenti = st.columns(2)
    prompt_selezionato = None

    for i, sug in enumerate(suggerimenti):
      col = cols_suggerimenti[i % 2]
      if col.button(f"👉 {sug}", key=f"sug_{i}"):
        prompt_selezionato = sug

    user_input = st.chat_input("Scrivi un messaggio al Coach...")
    messaggio_da_inviare = prompt_selezionato or user_input

    if messaggio_da_inviare:
      oggi_dt = datetime.date.today()
      giorno_ita = GIORNI_SETTIMANA_ITA[oggi_dt.weekday()]
      data_ita = oggi_dt.strftime("%d/%m/%Y")

      prompt_completo = f"""
            [OGGI È: {giorno_ita} {data_ita}]
            {messaggio_da_inviare}

            IMPORTANTE: Se la richiesta comporta la generazione o modifica di un allenamento .zwo:
            - Controlla la struttura e gli step dell'ultimo allenamento svolto nello storico per NON riproporre due volte di fila la stessa scheda.
            - Fornisci la struttura in TESTO SEMPLICE (Sintassi Intervals.icu) per copia/incolla rapido da smartphone.
            - Genera l'XML .zwo rigorosamente all'interno del blocco Markdown ```xml ... ```.
            - Usa durate espressamente in SECONDI e potenze in decimali FTP senza il simbolo '%'.
            """

      st.session_state["messages_history"].append(
          {"role": "user", "text": prompt_completo}
      )
      contesto_chiamata = [
          m["text"] for m in st.session_state["messages_history"]
      ]

      with st.spinner("RobyCoach AI sta rispondendo..."):
        try:
          client = genai.Client(api_key=GEMINI_API_KEY)
          response = chiama_gemini_safe(client, contesto_chiamata)

          st.session_state["messages_history"].append(
              {"role": "model", "text": response.text}
          )
          st.rerun()

        except Exception as e:
          st.error(f"Errore durante la risposta del Coach: {e}")

    # ==================== SALVATAGGIO CHAT ====================
    st.markdown("---")
    with st.expander("💾 Salva la conversazione corrente"):
      col_titolo_chat, col_btn_chat = st.columns([2, 1])
      with col_titolo_chat:
        titolo_chat_input = st.text_input(
            "Titolo Conversazione:",
            value=f"Chat_{datetime.date.today().strftime('%Y%m%d')}",
            key="input_title_chat",
        )
      with col_btn_chat:
        st.write(" ")
        if st.button("💾 Salva Chat", type="secondary", key="btn_save_chat"):
          nome_file_chat = genera_nome_univoco(
              DIR_CHAT, titolo_chat_input, estensione_default=".json"
          )
          percorso_chat = os.path.join(DIR_CHAT, nome_file_chat)

          dati_salvataggio = {
              "titolo": titolo_chat_input,
              "data": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "messages_history": st.session_state["messages_history"],
              "lista_attivita": st.session_state.get(
                  "lista_attivita_inviate", []
              ),
          }

          with open(percorso_chat, "w", encoding="utf-8") as f:
            json.dump(dati_salvataggio, f, ensure_ascii=False, indent=2)

          st.success(
              f"✅ Conversazione salvata con successo come **{nome_file_chat}**!"
          )
          st.rerun()

# ==================== TAB 2: PROGRAMMAZIONE & GESTIONE .ZWO ====================
with tab2:
  st.header("📤 Carica e Programma Allenamenti")

  uploaded_file = st.file_uploader(
      "Aggiungi un nuovo file .zwo personale", type=["zwo"]
  )
  if uploaded_file is not None:
    nome_univoco_upload = genera_nome_univoco(
        DIR_PERSONALE, uploaded_file.name
    )
    percorso_salvataggio = os.path.join(DIR_PERSONALE, nome_univoco_upload)
    with open(percorso_salvataggio, "wb") as f:
      f.write(uploaded_file.getbuffer())
    st.success(f"✅ Salvato: **{nome_univoco_upload}**")
    st.rerun()

  col_date, col_sport = st.columns(2)
  with col_date:
    data_allenamento = st.date_input(
        "Data per programmazione", datetime.date.today()
    )
  with col_sport:
    sport_selezionato_label = st.selectbox(
        "Tipo di Sport", list(TIPI_SPORT_INTERVALS.keys()), index=0
    )
    sport_code = TIPI_SPORT_INTERVALS[sport_selezionato_label]

  tipo_libreria = st.radio(
      "Sorgente scheda:",
      ["Libreria Personale", "Schede Generali RobyCoach"],
      horizontal=True,
  )
  cartella_target = (
      DIR_PERSONALE if tipo_libreria == "Libreria Personale" else DIR_GENERALI
  )
  files_disponibili = [
      f for f in os.listdir(cartella_target) if f.endswith(".zwo")
  ]

  if files_disponibili:
    file_scelto = st.selectbox("Seleziona il workout", files_disponibili)
    nome_workout = os.path.splitext(file_scelto)[0]

    if st.button("🚀 Invia a Intervals.icu", type="primary"):
      percorso_file = os.path.join(cartella_target, file_scelto)
      with open(percorso_file, "r", encoding="utf-8") as f:
        contenuto_xml = f.read()

      descrizione_struttura = estrai_descrizione_da_zwo(contenuto_xml)

      payload = {
          "start_date_local": f"{data_allenamento.isoformat()}T00:00:00",
          "type": sport_code,
          "category": "WORKOUT",
          "name": nome_workout,
          "description": (
              f"RobyCoach App ({tipo_libreria}) | Struttura:"
              f" {descrizione_struttura}"
          ),
          "file_contents": contenuto_xml,
          "filename": file_scelto,
      }

      res = requests.post(f"{BASE_URL}/events", json=payload, auth=AUTH)
      if res.status_code in [200, 201]:
        st.success(
            f"✅ Programmato '{nome_workout}' per il"
            f" {data_allenamento.strftime('%d/%m/%Y')} con descrizione!"
        )
      else:
        st.error(f"❌ Errore ({res.status_code}): {res.text}")

  st.divider()

  st.header("🗑️ Gestione File Personali")
  files_del = [f for f in os.listdir(DIR_PERSONALE) if f.endswith(".zwo")]

  if files_del:
    file_da_eliminare = st.selectbox(
        "Scegli il file dalla tua Libreria Personale da rimuovere:",
        files_del,
        key="select_del_personale",
    )
    if st.button(
        f"🗑️ Elimina '{file_da_eliminare}'",
        type="secondary",
        key="btn_del_personale",
    ):
      percorso_da_eliminare = os.path.join(DIR_PERSONALE, file_da_eliminare)
      if os.path.exists(percorso_da_eliminare):
        os.remove(percorso_da_eliminare)
        st.success(
            f"🗑️ File **{file_da_eliminare}** eliminato dalla tua libreria!"
        )
        st.rerun()
  else:
    st.info("Nessun file .zwo personale presente da eliminare.")

# ==================== TAB 3: PROFILO & INTEGRAZIONI ====================
with tab3:
  st.header("⚙️ Profilo Atleta & Accorgimenti AI")

  data_profile = get_intervals_profile(ATHLETE_ID, API_KEY)

  st.subheader("📌 Dati Sincronizzati da Intervals.icu")
  c_p1, c_p2, c_p3 = st.columns(3)
  c_p1.text_input(
      "Nome / Atleta", value=str(data_profile.get("name", ATHLETE_ID)), disabled=True
  )
  c_p2.text_input(
      "FTP Corrente (W)", value=str(data_profile.get("ftp", "N/A")), disabled=True
  )
  c_p3.text_input(
      "Peso (kg)", value=str(data_profile.get("weight", "N/A")), disabled=True
  )

  c_p4, c_p5 = st.columns(2)
  c_p4.text_input(
      "Frequenza Cardiaca Max",
      value=str(data_profile.get("max_hr", "N/A")),
      disabled=True,
  )
  c_p5.text_input(
      "FC a Riposo",
      value=str(data_profile.get("resting_hr", "N/A")),
      disabled=True,
  )

  st.divider()

  st.subheader("📝 Accorgimenti & Preferenze per RobyCoach AI")
  st.caption(
      "Le note inserite qui vengono salvate permanentemente e usate dall'AI per"
      " personalizzare le schede (es. dolori muscolari, tipo rulli, cadenza"
      " preferita, giorni di riposo)."
  )

  note_attuali = ""
  if os.path.exists(FILE_PROFILO):
    with open(FILE_PROFILO, "r", encoding="utf-8") as f:
      note_attuali = f.read()

  testo_note = st.text_area(
      "Note personali per l'AI:",
      value=note_attuali,
      height=180,
      placeholder=(
          "Es: Ho un leggero fastidio al ginocchio sinistro, evita strappi"
          " violenti. Mezzi di allenamento: Rulli Elite Suito. Preferisco"
          " cadenze elevate (>90 rpm)."
      ),
  )

  if st.button("💾 Salva Preferenze Profilo", type="primary"):
    with open(FILE_PROFILO, "w", encoding="utf-8") as f:
      f.write(testo_note)
    st.success("✅ Accorgimenti salvati con successo per il tuo utente!")

# ==================== TAB 4: STORICO CONVERSAZIONI ====================
with tab4:
  st.header("📜 Conversazioni Salvate")

  files_chat = [f for f in os.listdir(DIR_CHAT) if f.endswith(".json")]

  if files_chat:
    chat_selezionata = st.selectbox(
        "Seleziona una conversazione salvata da consultare o riaprire:",
        files_chat,
    )

    percorso_chat_sel = os.path.join(DIR_CHAT, chat_selezionata)

    if os.path.exists(percorso_chat_sel):
      with open(percorso_chat_sel, "r", encoding="utf-8") as f:
        dati_chat = json.load(f)

      st.markdown(f"**Titolo:** {dati_chat.get('titolo', 'Senza Titolo')}")
      st.caption(f"Data salvataggio: {dati_chat.get('data', 'N/A')}")

      col_riapri, col_elimina = st.columns([2, 1])
      with col_riapri:
        if st.button("🔄 Riapri questa Chat nella Tab 1", type="primary"):
          st.session_state["messages_history"] = dati_chat.get(
              "messages_history", []
          )
          st.session_state["lista_attivita_inviate"] = dati_chat.get(
              "lista_attivita", []
          )
          st.success("Chat caricata con successo! Passa alla Tab 1.")
          st.rerun()

      with col_elimina:
        if st.button(f"🗑️ Elimina Chat", type="secondary"):
          os.remove(percorso_chat_sel)
          st.success("Conversazione eliminata!")
          st.rerun()

      st.divider()
      st.subheader("📖 Anteprima dei messaggi:")

      for msg in dati_chat.get("messages_history", []):
        ruolo = "👨‍💻 Utente / Coach" if msg["role"] == "user" else "🤖 RobyCoach AI"
        with st.chat_message(msg["role"]):
          st.write(f"**{ruolo}**")
          st.write(msg["text"])
  else:
    st.info("Nessuna conversazione salvata al momento.")