#!/usr/bin/env python3
"""
Signal · refresh.py
Alimente data.json depuis HubSpot, puis (option) commit + push sur GitHub Pages.

Principe : l'API Sequences ne renvoie AUCUNE métrique de performance.
On reconstruit donc tout depuis l'objet d'engagement EMAIL, qui porte hs_sequence_id.

Prérequis
  export HUBSPOT_TOKEN="pat-eu1-..."   # token de l'application privée
  pip install requests

Portées de l'application privée — LECTURE SEULE, aucune écriture
  sales-email-read              objets EMAIL (envois, ouvertures, clics, réponses)
  crm.objects.contacts.read     membres des listes statiques
  crm.lists.read                filtre d'appartenance aux listes
  crm.objects.deals.read        simulations du pipe_courtage_ae
  crm.objects.meetings.read     RDV
  crm.objects.owners.read       noms des propriétaires
"""
import os, json, time, datetime as dt
import requests

TOKEN = os.environ["HUBSPOT_TOKEN"]
BASE = "https://api.hubapi.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# Fenêtre d'analyse
WEEKS_BACK = 13
SINCE = dt.datetime.now(dt.timezone.utc) - dt.timedelta(weeks=WEEKS_BACK)
SINCE_MS = int(SINCE.timestamp() * 1000)

# Whitelist optionnelle : ne suivre que ces séquences (par nom exact ou id).
# Laisser vide = toutes les séquences du portail.
ONLY_SEQUENCES = []


# ---------------------------------------------------------------- conversions
# HubSpot n'est pas cohérent sur les types renvoyés par l'API : un compteur
# peut arriver en "1", en "1.0" ou en None, un booléen en "true" ou en True,
# une date en millisecondes ou en ISO 8601. Ces trois helpers absorbent les
# variantes plutôt que de faire échouer tout le refresh sur un cas limite.

def num(v):
    """Entier tolérant. int('1.0') lève ValueError, d'où le passage par float."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def is_true(v):
    """Booléen tolérant : accepte True, 'true', 'True', 1, '1'."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1")


def to_dt(v):
    """Date tolérante : millisecondes epoch ou chaîne ISO 8601. None si illisible."""
    if v in (None, ""):
        return None
    try:
        return dt.datetime.fromtimestamp(float(v) / 1000, dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None



def get(path, **params):
    r = requests.get(BASE + path, headers=H, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def post(path, body):
    for attempt in range(5):
        r = requests.post(BASE + path, headers=H, json=body, timeout=45)
        if r.status_code == 429:                 # rate limit HubSpot
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()





# --------------------------------------------------------- emails engagement
EMAIL_PROPS = [
    "hs_sequence_id", "hs_email_status", "hs_email_direction",
    "hs_email_open_count", "hs_email_click_count", "hs_email_reply_count",
    "hs_email_subject", "hs_timestamp", "hubspot_owner_id",
    "hs_createdate",
]


def search_emails(sequence_id):
    """Tous les emails envoyés par une séquence sur la fenêtre."""
    out, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "hs_sequence_id", "operator": "EQ", "value": str(sequence_id)},
                {"propertyName": "hs_timestamp", "operator": "GTE", "value": str(SINCE_MS)},
            ]}],
            "properties": EMAIL_PROPS,
            "limit": 200,
            "sorts": [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}],
        }
        if after:
            body["after"] = after
        d = post("/crm/v3/objects/emails/search", body)
        out += d.get("results", [])
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
        # CRM Search plafonne à 10 000 résultats par requête :
        # si tu dépasses, découpe la fenêtre en tranches de dates.
    return out


def owners_map():
    d = get("/crm/v3/owners/", limit=200)
    return {o["id"]: (f'{o.get("firstName","")} {o.get("lastName","")}'.strip() or o.get("email", "?"))
            for o in d.get("results", [])}






# ============================================================================
# EXTENSIONS · objectifs hétérogènes par campagne
# ============================================================================
# La config des objectifs ne vient PAS de HubSpot : c'est une décision métier.
# Elle vit dans objectives.json, versionné à côté du code, et refresh.py la
# fusionne dans data.json. Conséquence : changer une cible = 1 ligne de JSON,
# sans toucher au collecteur ni au dashboard.
#
# objectives.json
# {
#   "104885900": {
#     "primary": "meetings", "primary_label": "RDV pris", "target": 120,
#     "secondary": "link_clicks", "secondary_label": "Clics lien simulation",
#     "secondary_target": 600,
#     "tracked_link": {
#       "label": "Simulation assurance emprunteur",
#       "url": "nopillo.com/go/assurance-emprunteur",
#       "contact_property": "ae_clic_lien_date",
#       "method": "RELAY_PAGE", "quality": "MESURE"
#     }
#   }
# }

MEETING_ATTRIB_DAYS = 30   # fenêtre d'attribution enrôlement → RDV








# ============================================================================
# VUE DE STOCK · pipeline courtage AE
# ============================================================================
# On photographie l'état courant : COUNT par dealstage, pas d'historique de
# passage. hs_v2_date_entered_current_stage donne l'ancienneté réelle dans
# l'étape (createdate ne convient pas : la reprise du 06/08 l'a écrasée).
#
# ATTENTION RGPD : dealname contient nom + email du client
# ("Courtage AE – prenom.nom@domaine.fr NOM"). Ce script ne l'exporte JAMAIS
# dans data.json. Si tu ajoutes dealname, le repo doit passer en privé.

PIPELINE_AE = "3817233652"
DORMANT_DAYS = 30

AE_STAGES = [
    # (stageId, clé, libellé, probabilité, fermée, gagnée)
    ("5363445962", "optimization_activated", "Optimisation activée", 10, False, False),
    ("5363445963", "simulation_started", "Simulation démarrée", 40, False, False),
    ("simulation_completed", "simulation_completed", "Simulation terminée", 60, False, False),
    ("simulation_ready", "simulation_ready", "Simulation prête", 60, False, False),
    ("5363445965", "offer_viewed", "Offre consultée", 70, False, False),
    ("5363445966", "offer_accepted", "Offre acceptée", 80, False, False),
    ("5363445967", "process_started", "Dossier lancé", 80, False, False),
    ("process_completed", "process_completed", "Dossier finalisé", 100, True, True),
    ("5363445968", "optimization_declined", "Optimisation refusée", 0, True, False),
]


def fetch_ae_pipeline():
    """Photo de l'état courant du pipeline courtage AE."""
    now = dt.datetime.now(dt.timezone.utc)
    out, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE_AE},
            ]}],
            # dealname est récupéré uniquement pour filtrer les tests,
            # il n'est jamais écrit dans data.json.
            "properties": ["dealstage", "dealname", "createdate", "hs_is_closed",
                           "hs_v2_date_entered_current_stage", "amount"],
            "limit": 200,
        }
        if after:
            body["after"] = after
        d = post("/crm/v3/objects/deals/search", body)
        out += d.get("results", [])
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break

    counts, deals = {}, []
    for r in out:
        p = r["properties"]
        stage = p.get("dealstage")
        name = (p.get("dealname") or "")
        is_test = "TEST" in name.upper()

        counts[stage] = counts.get(stage, 0) + 1     # décompte exhaustif
        if is_true(p.get("hs_is_closed")):
            continue

        entered = p.get("hs_v2_date_entered_current_stage") or p.get("createdate")
        e = to_dt(entered)
        days = (now - e).days if e else None
        deals.append(dict(id=r["id"], stage=stage, days_in_stage=days,
                          is_test=is_test))       # pas de dealname : RGPD

    stages = [dict(id=sid, key=k, label=l, probability=pr, closed=c, won=w,
                   count=counts.get(sid, 0))
              for sid, k, l, pr, c, w in AE_STAGES]

    return dict(
        id=PIPELINE_AE, key="pipe_courtage_ae", label="Pipeline courtage AE",
        snapshot_at=now.isoformat(),
        stages=stages,
        open_total=sum(s["count"] for s in stages if not s["closed"]),
        won_total=sum(s["count"] for s in stages if s["won"]),
        lost_total=sum(s["count"] for s in stages if s["closed"] and not s["won"]),
        deals=deals, deals_loaded=len(deals),
        deals_expected=sum(s["count"] for s in stages if not s["closed"]),
        anomalies=detect_anomalies(stages, deals),
    )


def detect_anomalies(stages, deals):
    """Contrôles rejoués à chaque refresh : le dashboard signale sa propre dette."""
    a = []
    won = next((s for s in stages if s["won"]), None)
    if won and won["count"] == 0:
        unused = [s["key"] for s in stages if s["count"] == 0 and not s["closed"]]
        a.append(dict(level="high", title="Aucun dossier finalisé",
                      body=f"L'étape gagnée {won['key']} contient 0 dossier. "
                           f"Étapes jamais utilisées : {', '.join(unused) or 'aucune'}. "
                           "Vérifier que le flow n8n écrit bien les étapes terminales."))
    tests = [d for d in deals if d["is_test"]]
    if tests:
        a.append(dict(level="med", title="Dossier de test en production",
                      body=f"{len(tests)} dossier(s) de test dans le pipeline, exclus des décomptes."))
    stale = [d for d in deals if not d["is_test"] and (d["days_in_stage"] or 0) > DORMANT_DAYS]
    if stale:
        a.append(dict(level="med", title="Dossiers dormants",
                      body=f"{len(stale)} dossier(s) sans changement d'étape depuis plus de "
                           f"{DORMANT_DAYS} jours. Le plus ancien : "
                           f"{max(d['days_in_stage'] for d in stale)} jours."))
    return a


# ============================================================================
# COHORTES · une cellule = une liste statique + la séquence qui lui a été envoyée
# ============================================================================
# Principe : la liste statique fige le dénominateur (qui était ciblé), les objets
# EMAIL donnent les faits (ce qui a réellement été envoyé et engagé). On ne se
# sert JAMAIS de hs_latest_sequence_enrolled : cette propriété ne garde que la
# dernière séquence, mesuré à 22 % de perte sur un cas réel (160 enrôlés → 124).

def load_cohorts_config():
    with open("cohorts.json", encoding="utf-8") as f:
        return json.load(f)


def list_members(list_id):
    """IDs des contacts d'une liste. Statique = figée, donc dénominateur stable."""
    ids, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "hs_crm_search.ilsListIds", "operator": "IN",
                 "values": [str(list_id)]},
            ]}],
            "properties": ["hubspot_owner_id"],   # jamais nom/email : RGPD
            "limit": 200,
        }
        if after:
            body["after"] = after
        d = post("/crm/v3/objects/contacts/search", body)
        for r in d.get("results", []):
            ids.append((r["id"], r["properties"].get("hubspot_owner_id")))
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    return ids


def emails_by_step(sequence_id):
    """Envois, ouvertures, clics, réponses d'une séquence, regroupés par objet
    d'email (proxy d'étape : les engagements ne portent pas le n° de step)."""
    steps, totals = {}, dict(sent=0, opens=0, clicks=0, replies=0, bounced=0)
    for e in search_emails(sequence_id):
        p = e["properties"]
        status = (p.get("hs_email_status") or "").upper()
        if status not in ("SENT", "BOUNCED"):
            continue
        totals["sent"] += 1
        subj = p.get("hs_email_subject") or "(sans objet)"
        st = steps.setdefault(subj, dict(sent=0, opens=0, clicks=0, replies=0))
        st["sent"] += 1
        if status == "BOUNCED":
            totals["bounced"] += 1
            continue
        for src, dst in (("hs_email_open_count", "opens"),
                         ("hs_email_click_count", "clicks"),
                         ("hs_email_reply_count", "replies")):
            hit = 1 if num(p.get(src)) > 0 else 0
            st[dst] += hit
            totals[dst] += hit
    ordered = sorted(steps.items(), key=lambda x: -x[1]["sent"])
    return totals, [dict(order=i + 1, subject=k, **v) for i, (k, v) in enumerate(ordered)]


def meetings_for(contact_ids, since_ms):
    """RDV par propriétaire de contact. Fenêtre = date d'envoi de la cohorte."""
    owner_of = dict(contact_ids)
    ids = [c for c, _ in contact_ids]
    per_owner, total = {}, 0
    for i in range(0, len(ids), 100):
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "associations.contact", "operator": "IN", "values": ids[i:i + 100]},
                {"propertyName": "hs_createdate", "operator": "GTE", "value": str(since_ms)},
            ]}],
            "properties": ["hs_outcome_completed_count", "hs_outcome_no_show_count"],
            "limit": 200,
        }
        for m in post("/crm/v3/objects/meetings/search", body).get("results", []):
            total += 1
            for cid in (m.get("associations", {}).get("contacts", {}).get("results") or []):
                o = owner_of.get(cid.get("id"))
                per_owner[o] = per_owner.get(o, 0) + 1
    return total, per_owner


def deals_ae_for(contact_ids, pipeline_id, since_ms):
    """Transactions du pipe courtage AE associées aux contacts de la cohorte."""
    owner_of = dict(contact_ids)
    ids = [c for c, _ in contact_ids]
    per_owner, total = {}, 0
    for i in range(0, len(ids), 100):
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "pipeline", "operator": "EQ", "value": pipeline_id},
                {"propertyName": "associations.contact", "operator": "IN", "values": ids[i:i + 100]},
                {"propertyName": "createdate", "operator": "GTE", "value": str(since_ms)},
            ]}],
            "properties": ["dealstage"],       # jamais dealname : contient nom + email
            "limit": 200,
        }
        for d in post("/crm/v3/objects/deals/search", body).get("results", []):
            total += 1
            for cid in (d.get("associations", {}).get("contacts", {}).get("results") or []):
                o = owner_of.get(cid.get("id"))
                per_owner[o] = per_owner.get(o, 0) + 1
    return total, per_owner


def build_cohorts():
    cfg = load_cohorts_config()
    owners = owners_map()
    pipeline_id = cfg["deal_pipeline"]
    out = []

    for co in cfg["cohorts"]:
        since_ms = int(dt.datetime.fromisoformat(
            co["sent_at"].replace("Z", "+00:00")).timestamp() * 1000)
        cells = []
        for c in co["cells"]:
            totals, steps = emails_by_step(c["sequence_id"])
            members = list_members(c["list_id"]) if c.get("list_id") else []
            n_meet, m_owner = meetings_for(members, since_ms) if members else (0, {})
            n_deal, d_owner = deals_ae_for(members, pipeline_id, since_ms) if members else (0, {})

            oids = set(m_owner) | set(d_owner)
            cells.append(dict(
                list_id=c.get("list_id"), list_name=c.get("list_name"),
                sequence_id=c["sequence_id"], audience=c["audience"], version=c["version"],
                list_size=len(members) or None,
                # enrôlés = contacts ayant réellement reçu l'étape la plus servie
                enrolled=max((s["sent"] for s in steps), default=0),
                sent=totals["sent"], bounced=totals["bounced"],
                opens=totals["opens"], clicks=totals["clicks"], replies=totals["replies"],
                meetings=n_meet, deals_ae=n_deal, steps=steps,
                by_owner=[dict(owner_id=o, owner=owners.get(o, "Non attribué"),
                               meetings=m_owner.get(o, 0), deals_ae=d_owner.get(o, 0))
                          for o in sorted(oids, key=lambda x: str(x))],
            ))
        out.append(dict(id=co["id"], label=co["label"], sent_at=co["sent_at"],
                        campaign=co["campaign"], confirmed=co["confirmed"],
                        confirm_note=co.get("confirm_note"), cells=cells))

    out.sort(key=lambda x: x["sent_at"])
    return out, cfg


def build_all():
    """Point d'entrée complet : cohortes + pipeline AE, écrits dans data.json."""
    cohorts, cfg = build_cohorts()
    data = dict(
        meta=dict(generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  period=f"{len(cohorts)} cohorte(s)",
                  source="HubSpot · listes statiques + EMAIL.hs_sequence_id + MEETING_EVENT + DEAL",
                  mode="LIVE"),
        cohorts=cohorts,
        audience_labels=cfg["audience_labels"],
        stats_config=cfg["stats"],
        pipeline_ae=fetch_ae_pipeline(),
    )
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    n = sum(len(c["cells"]) for c in cohorts)
    print(f"OK · {len(cohorts)} cohortes · {n} cellules · "
          f"{data['pipeline_ae']['open_total']} dossiers AE en cours")


if __name__ == "__main__":
    build_all()
