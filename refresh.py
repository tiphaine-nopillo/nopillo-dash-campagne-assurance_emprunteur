#!/usr/bin/env python3
"""
Signal · refresh.py
Alimente data.json depuis HubSpot, puis (option) commit + push sur GitHub Pages.

Principe : l'API Sequences ne renvoie AUCUNE métrique de performance.
On reconstruit donc tout depuis l'objet d'engagement EMAIL, qui porte hs_sequence_id.

Prérequis
  export HUBSPOT_TOKEN="pat-eu1-..."   # private app token
  export HUBSPOT_USER_ID="12345678"    # requis par l'API Sequences
  pip install requests

Scopes de la private app
  automation.sequences.read
  crm.objects.contacts.read
  sales-email-read            (objets EMAIL)
  crm.objects.owners.read
  crm.objects.meetings.read   (si suivi RDV)
"""
import os, json, time, datetime as dt
import requests

TOKEN = os.environ["HUBSPOT_TOKEN"]
USER_ID = os.environ.get("HUBSPOT_USER_ID", "")
BASE = "https://api.hubapi.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# Fenêtre d'analyse
WEEKS_BACK = 13
SINCE = dt.datetime.now(dt.timezone.utc) - dt.timedelta(weeks=WEEKS_BACK)
SINCE_MS = int(SINCE.timestamp() * 1000)

# Whitelist optionnelle : ne suivre que ces séquences (par nom exact ou id).
# Laisser vide = toutes les séquences du portail.
ONLY_SEQUENCES = []


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


# ---------------------------------------------------------------- séquences
def list_sequences():
    out, after = [], None
    while True:
        p = {"limit": 100, "userId": USER_ID}
        if after:
            p["after"] = after
        d = get("/automation/sequences/2026-03", **p)
        out += d.get("results", [])
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    if ONLY_SEQUENCES:
        keep = set(str(x) for x in ONLY_SEQUENCES)
        out = [s for s in out if s["id"] in keep or s["name"] in keep]
    return out


def sequence_detail(sid):
    return get(f"/automation/sequences/2026-03/{sid}", userId=USER_ID)


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


# ------------------------------------------------------------------ agrégats
def iso_week(ts_ms):
    d = dt.datetime.fromtimestamp(int(ts_ms) / 1000, dt.timezone.utc)
    return f"W{d.isocalendar().week}"


def build():
    owners = owners_map()
    seqs_out, weekly = [], {}
    senders = {}

    for s in list_sequences():
        detail = sequence_detail(s["id"])
        step_defs = {st["stepOrder"]: st for st in detail.get("steps", [])
                     if st.get("actionType", "").startswith("EMAIL")}
        emails = search_emails(s["id"])
        if not emails:
            continue

        agg = dict(sent=0, bounced=0, opens=0, clicks=0, replies=0)
        steps = {}
        for e in emails:
            p = e["properties"]
            status = (p.get("hs_email_status") or "").upper()
            if status not in ("SENT", "BOUNCED"):
                continue
            agg["sent"] += 1
            if status == "BOUNCED":
                agg["bounced"] += 1
                # on ne compte pas d'engagement sur un bounce
            else:
                # bornage à 1 pour obtenir de l'unique par email
                op = 1 if int(p.get("hs_email_open_count") or 0) > 0 else 0
                cl = 1 if int(p.get("hs_email_click_count") or 0) > 0 else 0
                rp = 1 if int(p.get("hs_email_reply_count") or 0) > 0 else 0
                agg["opens"] += op
                agg["clicks"] += cl
                agg["replies"] += rp

            # regroupement par objet d'email = proxy de step
            subj = p.get("hs_email_subject") or "(sans objet)"
            st = steps.setdefault(subj, dict(sent=0, opens=0, clicks=0, replies=0))
            st["sent"] += 1
            if status == "SENT":
                st["opens"] += 1 if int(p.get("hs_email_open_count") or 0) > 0 else 0
                st["clicks"] += 1 if int(p.get("hs_email_click_count") or 0) > 0 else 0
                st["replies"] += 1 if int(p.get("hs_email_reply_count") or 0) > 0 else 0

            # hebdo
            w = weekly.setdefault(iso_week(p.get("hs_timestamp") or p.get("hs_createdate")),
                                  dict(sent=0, delivered=0, opens=0, clicks=0, replies=0,
                                       meetings=0, enrolled=0))
            w["sent"] += 1
            if status == "SENT":
                w["delivered"] += 1
                w["opens"] += 1 if int(p.get("hs_email_open_count") or 0) > 0 else 0
                w["clicks"] += 1 if int(p.get("hs_email_click_count") or 0) > 0 else 0
                w["replies"] += 1 if int(p.get("hs_email_reply_count") or 0) > 0 else 0

            # sender
            oid = p.get("hubspot_owner_id")
            snd = senders.setdefault(owners.get(oid, "Inconnu"),
                                     dict(sent=0, bounced=0, replies=0))
            snd["sent"] += 1
            if status == "BOUNCED":
                snd["bounced"] += 1
            else:
                snd["replies"] += 1 if int(p.get("hs_email_reply_count") or 0) > 0 else 0

        owner_name = owners.get(str(detail.get("userId")), "—")
        seqs_out.append(dict(
            id=s["id"], name=s["name"], owner=owner_name, status="ACTIVE",
            # Approximation assumée : enrôlés ≈ envois du 1er step.
            # Pour de l'exact, il faut logger les enrôlements via un workflow
            # HubSpot qui horodate une propriété contact à l'enrôlement.
            enrolled=max((v["sent"] for v in steps.values()), default=0),
            sent=agg["sent"], delivered=agg["sent"] - agg["bounced"], bounced=agg["bounced"],
            opens_unique=agg["opens"], clicks_unique=agg["clicks"],
            replies=agg["replies"], meetings=0, unsubs=0,
            ended_no_reply=0,
            steps=[dict(order=i + 1, type="EMAIL", subject=k, **v)
                   for i, (k, v) in enumerate(sorted(steps.items(), key=lambda x: -x[1]["sent"]))],
        ))

    data = dict(
        meta=dict(generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  period=f"{WEEKS_BACK} dernières semaines",
                  source="HubSpot CRM Search · objet EMAIL · hs_sequence_id",
                  mode="LIVE"),
        sequences=seqs_out,
        weekly=[dict(week=k, **v) for k, v in sorted(
            weekly.items(), key=lambda x: int(x[0][1:]))],
        senders=[dict(name=k, sent=v["sent"], bounced=v["bounced"], replies=v["replies"],
                      bounce_rate=v["bounced"] / v["sent"] if v["sent"] else 0,
                      reply_rate=v["replies"] / v["sent"] if v["sent"] else 0)
                 for k, v in senders.items()],
    )
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"OK · {len(seqs_out)} séquences · {sum(s['sent'] for s in seqs_out)} emails")




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


def load_objectives():
    try:
        with open("objectives.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def count_link_clicks(contact_property):
    """Clics sur le lien suivi, dédupliqués par contact.

    Ne compte PAS des clics : compte les contacts dont la propriété a été
    horodatée par le workflow déclenché sur la vue de la page relais.
    C'est volontaire — un contact qui clique trois fois reste un prospect.

    Prérequis côté HubSpot (à faire une fois) :
      1. page nopillo.com/go/<campagne> avec le script de tracking, puis
         redirection JS vers la destination (obligatoire si la destination
         est un domaine partenaire : le tracking HubSpot n'y tourne pas) ;
      2. workflow contact, déclencheur "a visité une page dont l'URL contient
         /go/<campagne>", action : définir <contact_property> = date du jour.
    """
    body = {
        "filterGroups": [{"filters": [
            {"propertyName": contact_property, "operator": "HAS_PROPERTY"},
            {"propertyName": contact_property, "operator": "GTE", "value": str(SINCE_MS)},
        ]}],
        "properties": [contact_property],
        "limit": 1,
    }
    return post("/crm/v3/objects/contacts/search", body).get("total", 0)


def count_meetings(contact_ids):
    """RDV pris / honorés / no-show / annulés sur les contacts enrôlés.

    hs_outcome_*_count permet de séparer le RDV posé au calendrier du RDV
    réellement tenu. L'écart est le vrai sujet de pilotage : un no-show
    compte dans « RDV pris » mais ne génère aucun revenu.
    """
    agg = dict(booked=0, completed=0, noshow=0, canceled=0)
    ids = list(contact_ids)
    for i in range(0, len(ids), 100):
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "associations.contact", "operator": "IN",
                 "values": ids[i:i + 100]},
                {"propertyName": "hs_createdate", "operator": "GTE", "value": str(SINCE_MS)},
            ]}],
            "properties": ["hs_meeting_outcome", "hs_outcome_completed_count",
                           "hs_outcome_no_show_count", "hs_outcome_canceled_count",
                           "hs_meeting_start_time", "hs_object_source_label"],
            "limit": 200,
        }
        for m in post("/crm/v3/objects/meetings/search", body).get("results", []):
            p = m["properties"]
            agg["booked"] += 1
            agg["completed"] += int(p.get("hs_outcome_completed_count") or 0)
            agg["noshow"] += int(p.get("hs_outcome_no_show_count") or 0)
            agg["canceled"] += int(p.get("hs_outcome_canceled_count") or 0)
    return agg


def enrich(data):
    """Fusionne les objectifs et les métriques qui ne viennent pas des emails."""
    objectives = load_objectives()
    for s in data["sequences"]:
        cfg = objectives.get(s["id"])
        if not cfg:
            continue
        s["objective"] = {k: v for k, v in cfg.items() if k != "tracked_link"}

        link = cfg.get("tracked_link")
        if link:
            s["tracked_link"] = link
            if link.get("contact_property"):
                s["link_clicks_unique"] = count_link_clicks(link["contact_property"])

        # RDV : nécessite la liste des contacts enrôlés.
        # À brancher quand la propriété d'enrôlement existe (cf. README).
        # m = count_meetings(enrolled_contact_ids(s["id"]))
        # s.update(meetings_booked=m["booked"], meetings_completed=m["completed"],
        #          meetings_noshow=m["noshow"], meetings_canceled=m["canceled"])
    return data


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
        if p.get("hs_is_closed") == "true":
            continue

        entered = p.get("hs_v2_date_entered_current_stage") or p.get("createdate")
        days = None
        if entered:
            e = dt.datetime.fromtimestamp(int(entered) / 1000, dt.timezone.utc)
            days = (now - e).days
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
            hit = 1 if int(p.get(src) or 0) > 0 else 0
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
