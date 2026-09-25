#!/usr/bin/env python3
"""
Suivi campagne Assurance Emprunteur · refresh.py
Alimente data.json depuis HubSpot.

CORRECTIF CENTRAL DE CETTE VERSION — VAGUES DE RELANCE
------------------------------------------------------
Le 11/09/2026, 1 070 contacts déjà shootés en août ont été relancés sur 4 listes
et 4 nouvelles séquences. Trois défauts du collecteur sont apparus, mesurés en
direct entre deux runs espacés de 95 minutes :

  * Les RDV n'avaient AUCUNE borne haute. Un RDV pris des mois après l'envoi
    restait imputé au batch d'origine. Dans les 2 h suivant l'envoi des
    relances, le batch du 13 août a gagné 7 RDV alors que sa fenêtre était
    close depuis le 03/09. CORRIGÉ : borne haute J+21, comme les dossiers.

  * Les ouvertures et les clics reposent sur hs_sales_email_last_opened et
    hs_sales_email_last_clicked, des propriétés « dernière fois » sans
    mémoire. Une ouverture de relance écrase la date d'août et reste comptée
    comme une ouverture d'août. Le total campagne est passé de 1 214 à 1 236
    ouvertures en 95 minutes. CORRIGÉ : les valeurs d'août sont FIGÉES dans
    cohorts.json (frozen_metrics) et ne sont plus recalculées.

  * Les dossiers de la relance étaient invisibles : seul deal_engages()
    appliquait ATTRIB_DAYS, et les fenêtres d'août étaient fermées.
    CORRIGÉ : fenêtre par contact, cf. ci-dessous.

MODÈLE DE FENÊTRE PAR CONTACT
------------------------------
Un contact relancé dispose de DEUX fenêtres de 21 jours : celle de son batch
initial (v1) et celle de sa relance (v2). Un événement compte s'il tombe dans
l'une OU l'autre, et il est étiqueté. Deux raisons de ne pas simplement
décaler la fenêtre :

  * une activation réelle d'août ne doit pas disparaître parce que le contact
    a été relancé un mois plus tard ;
  * fusionner les deux en une fenêtre unique de 58 jours détruirait la
    comparabilité entre cohortes que ATTRIB_DAYS sert à garantir.

Le rattachement d'un contact relancé à sa cohorte est CALCULÉ, pas déclaré :
les listes de relance sont construites sur un statut de séquence, pas sur
l'appartenance à un batch, et un mapping en dur serait faux pour une partie
des contacts.

Une relance n'est JAMAIS une cohorte. Ses contacts sont déjà dans le
dénominateur de leur batch ; les compter deux fois ferait baisser tous les
taux mécaniquement.

BIAIS DE SÉLECTION À NE PAS OUBLIER
------------------------------------
Les 4 listes de relance ne contiennent QUE des contacts non activés. Le taux
d'activation d'une vague 2 n'est donc pas comparable à celui d'un batch
initial, dont le dénominateur incluait tout le monde. Toute activation de
vague 2 est un gain marginal pur.

CORRECTIF DE LA VERSION PRÉCÉDENTE, TOUJOURS VALABLE
----------------------------------------------------
Les séquences sont RÉUTILISÉES d'un batch à l'autre : 841303267 a servi le RP
du 5 août, celui du 13 août puis le batch du 10 septembre. Toute métrique est
attribuée par APPARTENANCE À LA LISTE de la cellule. La séquence ne sert qu'à
restreindre le périmètre des e-mails collectés.

Prérequis
  export HUBSPOT_TOKEN="pat-eu1-..."
  pip install requests

Portées de l'application privée — LECTURE SEULE
  crm.objects.contacts.read     membres des listes
  crm.lists.read                filtre d'appartenance
  sales-email-read              objets EMAIL
  crm.objects.meetings.read     RDV  (couvert par sales-email-read sur ce portail)
  crm.objects.deals.read        dossiers courtage
"""
import os
import json
import time
import datetime as dt

import requests

TOKEN = os.environ["HUBSPOT_TOKEN"]
BASE = "https://api.hubapi.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
CONTACTS = "/crm/v3/objects/contacts/search"
EMAILS = "/crm/v3/objects/emails/search"
MEETINGS = "/crm/v3/objects/meetings/search"
CALLS = "/crm/v3/objects/calls/search"
DEALS = "/crm/v3/objects/deals/search"
CHUNK = 100          # taille de lot pour les filtres associations.contact

# ---------------------------------------------------------------- engagement
# Fenêtre d'attribution. Sans borne de fin, un cumul ouvert monte à chaque
# rafraîchissement et deux cohortes d'âge différent cessent d'être comparables.
#
# NON CALIBRÉ. Les courbes de réponse cumulées relevées le 11/09 montrent que
# les deux batchs d'août MONTAIENT ENCORE à J+21 (5 août : 9,28 → 9,44 → 9,60 % ;
# 13 août : 7,14 → 7,43 → 8,00 %). La fenêtre coupe en pleine pente et
# sous-estime les conversions : 30 à 45 jours serait plus juste. Non modifié
# pour l'instant, car changer ce chiffre réécrit rétroactivement tout
# l'historique déjà communiqué.
ATTRIB_DAYS = 21

# Batch de rattrapage : 10 transactions créées en 20 secondes le 06/08, mêlant
# contacts enrôlés et contacts jamais touchés par une séquence. Import
# d'antériorité, pas de l'activité. 4 concernent des contacts de cohorte.
BACKFILL = [("2026-08-06T15:25:00Z", "2026-08-06T15:26:00Z"),
            ("2026-09-16T09:57:00Z", "2026-09-16T09:58:00Z"),
            ("2026-09-16T11:00:00Z", "2026-09-16T11:03:00Z")]

# Marqueur de simulation réelle. Écrit par n8n depuis last_event_at, À LA
# CRÉATION COMME À LA MISE À JOUR : un dossier ouvert à la main pendant une
# panne puis repris par le flux le porte quand même. C'est exactement ce que
# hs_object_source_label ne sait pas faire — un deal CRM_UI peut cacher une
# vraie simulation.
# NE PAS confondre avec last_step_id, qui vient du step_id du DERNIER event et
# n'est renseignée que si cet event est une complétion d'étape : 3 transactions
# sur 394 au 17/09, contre 245 pour last_step_date.
# ÉTAPES QUI PROUVENT QUE LE CLIENT A AGI.
# resolveDealStageId, dans le flow n8n « Sync Courtage AE x HubSpot » :
#   étape 'ajout-emprunteurs' franchie  -> simulation_completed
#   étape 'intro' franchie              -> simulation_started
#   aucune étape franchie               -> optimization_activated
# Donc optimization_activated signifie EXACTEMENT « le client a accès au
# simulateur et n'a rien rempli ». Ce n'est pas une activation.
# simulation_started correspond littéralement à « a rempli au minimum le
# premier champ » de la définition du 17/09.
SIMU_STAGES = {
    "5363445963",   # simulation_started
    "5363445964",   # simulation_completed
    "5783848147",   # simulation_ready
    "5363445965",   # offer_viewed
    "5363445966",   # offer_accepted
    "5363445967",   # process_started
    "5378179265",   # process_completed
}
STAGE_ACTIVATED = "5363445962"   # accès au simulateur, aucune étape
STAGE_DECLINED = "5363445968"    # étape terminale pilotée par le CS

# NE PAS UTILISER COMME MARQUEUR DE SIMULATION. last_step_date vient de
# last_event_at, et un event existe dès l'activation de l'optimisation :
# 153 optimisations courtage AE ont des events sans aucune étape complétée.
# La propriété prouve qu'il s'est passé quelque chose, pas que le client a
# rempli un champ. Conservée pour information seulement.
SIMU_PROP = "last_step_date"

# Réservation en self-service via un lien public. Distingue un RDV que le
# client a posé lui-même d'un RDV calé par un commercial au téléphone.
MEETING_PUBLIC = "MEETINGS_PUBLIC"

# Transactions créées par un workflow HubSpot PARCE QU'un RDV existe, et non
# parce qu'un client a fait quelque chose. Elles ne prouvent rien : le RDV est
# déjà compté via l'objet MEETING. Les compter comme dossier reviendrait à
# compter deux fois le même signal, et pire, à activer un contact dont le RDV
# est hors fenêtre au motif qu'une carte a été créée depuis.
# 43 transactions créées le 24/09 à 17h28, en moins d'une seconde.
ORIGINE_RDV_SANS_SIMU = "rdv_sans_simu"

# Owner IDs des commerciaux habilités sur la campagne. Ce sont des Owner IDs,
# PAS des User IDs — HubSpot maintient les deux et ils ne sont pas
# interchangeables.
AE_MEETING_OWNERS = ["1722214870",  # Clara Baekelandt
                     "75453551",    # Lilian Maudet
                     "650299108"]   # Mathieu d'Ornellas

# Portail HubSpot, pour les liens de vérification imprimés dans les logs.
PORTAL = "26173790"


# ---------------------------------------------------------------- utilitaires
def post(path, body):
    """POST avec retente exponentielle sur les limites de débit HubSpot."""
    for attempt in range(5):
        r = requests.post(BASE + path, headers=H, json=body, timeout=45)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def pcts(n, d):
    return f"{100 * n / d:.1f} %".replace(".", ",") if d else "—"


def num(v):
    """Entier tolérant : HubSpot renvoie parfois '1.0' là où on attend 1."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def to_dt(v):
    """Date tolérante : millisecondes epoch ou chaîne ISO. None si illisible."""
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


def iso(v):
    """Chaîne ISO d'un champ sent_at de config."""
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def count_active(list_id, sequence_ids):
    """Contacts encore activement enrôlés dans UNE SÉQUENCE DE LA CAMPAGNE.

    Le filtre sur hs_sequences_is_enrolled seul ne suffit pas : cette propriété
    vaut vrai pour n'importe quelle séquence du portail.

    On croise donc avec hs_latest_sequence_enrolled. RÉSERVE AGGRAVÉE PAR LES
    RELANCES : cette propriété ne garde que la DERNIÈRE séquence. Depuis le
    11/09, les 1 070 contacts relancés pointent vers une séquence de relance,
    donc leurs cellules d'origine basculent toutes en TERMINE. Pour août c'est
    juste — les envois sont finis — mais le mécanisme est aveugle et non un
    constat de fin réelle.
    """
    return count_lists([list_id], [
        {"propertyName": "hs_sequences_is_enrolled", "operator": "EQ", "value": "true"},
        {"propertyName": "hs_latest_sequence_enrolled", "operator": "IN",
         "values": [str(s) for s in sequence_ids]},
    ])


def count_lists(list_ids, extra=None):
    """Contacts appartenant à l'une des listes, avec un filtre additionnel."""
    filters = [{"propertyName": "hs_crm_search.ilsListIds", "operator": "IN",
                "values": [str(x) for x in list_ids]}]
    if extra:
        filters += (extra if isinstance(extra, list) else [extra])
    body = {"filterGroups": [{"filters": filters}],
            "properties": ["hs_object_id"], "limit": 1}
    return post(CONTACTS, body).get("total", 0)


def list_members(list_id):
    """IDs des contacts d'une liste, avec leur propriétaire.

    On ne demande que l'ID et le propriétaire : aucun nom, aucun e-mail,
    aucun téléphone ne transite ni n'est écrit dans data.json.
    """
    out, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "hs_crm_search.ilsListIds", "operator": "IN",
                 "values": [str(list_id)]},
            ]}],
            "properties": ["hubspot_owner_id", "hs_sales_email_last_replied",
                           "hs_sales_email_last_opened", "hs_sales_email_last_clicked"],
            "limit": CHUNK,
        }
        if after:
            body["after"] = after
        d = post(CONTACTS, body)
        for r in d.get("results", []):
            p = r["properties"]
            out.append((r["id"], p.get("hubspot_owner_id"),
                        p.get("hs_sales_email_last_replied"),
                        p.get("hs_sales_email_last_opened"),
                        p.get("hs_sales_email_last_clicked")))
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after:
            return out


def owners_map():
    r = requests.get(BASE + "/crm/v3/owners/", headers=H,
                     params={"limit": 200}, timeout=30)
    r.raise_for_status()
    return {o["id"]: (f'{o.get("firstName","")} {o.get("lastName","")}'.strip()
                      or o.get("email", "?"))
            for o in r.json().get("results", [])}


# ------------------------------------------------------------- vagues
def build_relance_map(cfg):
    """contact_id -> date du DERNIER envoi de relance qui l'a touché.

    Rattachement CALCULÉ : on ne déclare nulle part qu'une liste de relance
    appartient à telle cohorte. On collecte les contacts, et chacun est
    retrouvé plus bas dans la cellule à laquelle il appartient déjà. Les
    listes de relance sont construites sur un statut de séquence — « a fini
    sa campagne », « a été interrompu » — et non sur l'appartenance à un
    batch : un mapping en dur serait faux pour une partie des contacts.

    Le max() protège le cas d'un contact présent dans deux listes de relance :
    c'est le dernier envoi qui ouvre sa fenêtre.
    """
    out = {}
    for r in cfg.get("relances", []):
        when = iso(r["sent_at"])
        for m in list_members(r["list_id"]):
            cid = m[0]
            if cid not in out or when > out[cid]:
                out[cid] = when
    return out


def windows_for(cid, cohort_send, rmap):
    """Fenêtres d'attribution d'un contact, de la plus ancienne à la plus récente.

    [("v1", envoi du batch, +21j)] et, si le contact a été relancé,
    [("v2", envoi de la relance, +21j)] en plus.

    Deux fenêtres disjointes plutôt qu'une seule élargie : une activation
    réelle d'août ne doit pas disparaître parce que le contact a été relancé,
    et une fenêtre unique de 58 jours détruirait la comparabilité entre
    cohortes.
    """
    w = [("v1", cohort_send, cohort_send + dt.timedelta(days=ATTRIB_DAYS))]
    r = rmap.get(cid)
    if r and r > cohort_send:
        w.append(("v2", r, r + dt.timedelta(days=ATTRIB_DAYS)))
    return w


def in_windows(when, wins):
    """Étiquette de la fenêtre contenant cette date, la plus récente d'abord."""
    if not when:
        return None
    for tag, start, end in reversed(wins):
        if start <= when <= end:
            return tag
    return None


# ------------------------------------------------------------------- e-mails
def emails_for(contact_ids, sequence_ids, since_ms):
    """Envois, ouvertures, clics et réponses des e-mails de séquence reçus par
    ces contacts précisément.

    Le double filtre est ce qui corrige le bug : hs_sequence_id restreint aux
    séquences de la campagne, associations.contact restreint aux contacts de la
    cellule. Sans le second, un batch ultérieur partageant la séquence viendrait
    gonfler les chiffres.
    """
    agg = dict(sent=0, bounced=0, opens=0, clicks=0)
    steps = {}
    if not contact_ids:
        return agg, []
    for i in range(0, len(contact_ids), CHUNK):
        chunk = contact_ids[i:i + CHUNK]
        after = None
        while True:
            body = {
                "filterGroups": [{"filters": [
                    {"propertyName": "hs_sequence_id", "operator": "IN",
                     "values": [str(s) for s in sequence_ids]},
                    {"propertyName": "associations.contact", "operator": "IN",
                     "values": chunk},
                    {"propertyName": "hs_timestamp", "operator": "GTE",
                     "value": str(since_ms)},
                ]}],
                "properties": ["hs_email_status", "hs_email_subject",
                               "hs_email_open_count", "hs_email_click_count",
                               "hs_timestamp"],
                "limit": 200,
            }
            if after:
                body["after"] = after
            d = post(EMAILS, body)
            for e in d.get("results", []):
                p = e["properties"]
                status = (p.get("hs_email_status") or "").upper()
                if status not in ("SENT", "BOUNCED"):
                    continue
                agg["sent"] += 1
                subj = p.get("hs_email_subject") or "(sans objet)"
                st = steps.setdefault(subj, dict(sent=0, opens=0, clicks=0))
                st["sent"] += 1
                if status == "BOUNCED":
                    agg["bounced"] += 1
                    continue
                op = 1 if num(p.get("hs_email_open_count")) > 0 else 0
                cl = 1 if num(p.get("hs_email_click_count")) > 0 else 0
                agg["opens"] += op
                agg["clicks"] += cl
                st["opens"] += op
                st["clicks"] += cl
            after = (d.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break
    ordered = sorted(steps.items(), key=lambda x: -x[1]["sent"])
    return agg, [dict(order=i + 1, subject=k, **v)
                 for i, (k, v) in enumerate(ordered)]


# ------------------------------------------------------- RDV et engagement
def _objects_assoc(path, contact_ids, extra_filters, props):
    """Objets associés à ces contacts, AVEC leurs propriétés."""
    out = {}
    for i in range(0, len(contact_ids), CHUNK):
        chunk = contact_ids[i:i + CHUNK]
        after = None
        while True:
            filters = [{"propertyName": "associations.contact",
                        "operator": "IN", "values": chunk}] + extra_filters
            body = {"filterGroups": [{"filters": filters}],
                    "properties": props, "limit": 200}
            if after:
                body["after"] = after
            d = post(path, body)
            for r in d.get("results", []):
                out[r["id"]] = r["properties"]
            after = (d.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break
    return out


def _contacts_of(object_type, object_ids):
    """Contacts associés à chaque objet, via l'API associations v4."""
    m = {}
    ids = list(object_ids)
    for i in range(0, len(ids), CHUNK):
        d = post(f"/crm/v4/associations/{object_type}/contacts/batch/read",
                 {"inputs": [{"id": o} for o in ids[i:i + CHUNK]]})
        for r in d.get("results", []):
            m[r["from"]["id"]] = [str(t["toObjectId"]) for t in r["to"]]
    return m


def in_backfill(d):
    """La date tombe-t-elle dans une rafale de rattrapage n8n ?

    Un rattrapage importe de l'ANTÉRIORITÉ, pas de l'activité. Deux rafales
    connues : 06/08 15h25-15h26 (10 transactions) et 16/09 11h00-11h03
    (~190 créations et ~248 mises à jour, reprise du flux après 33 jours
    d'arrêt). La borne haute du 16/09 est à 11h03 et non 11h02 : la rafale
    s'est prolongée jusqu'à 11:02:04, et deux transactions ouvertes le 5 juin
    y recevaient leur mouvement d'étape — comptées à tort comme activations
    de septembre. Vérifié le 17/09 par requête directe sur le pipe.

    L'exclusion porte sur CHAQUE date prise isolément, plus sur la transaction
    entière comme avant : un dossier créé pendant la rafale mais réellement
    déplacé d'étape trois jours plus tard reste une activation légitime. Sans
    ce changement, la clause « déplacé d'étape dans la fenêtre » comptait les
    248 mouvements du rattrapage du 16/09 comme autant d'activations.
    """
    if not d:
        return False
    return any(to_dt(a) <= d < to_dt(b) for a, b in BACKFILL)


def deal_engages(props, wins):
    """Étiquette de vague si la transaction entre dans une fenêtre, sinon None.

    AUCUN filtre sur dealstage, volontairement : les transactions remontées
    par n8n sautent des étapes, une étape absente ne prouve rien.

    Deux bornes, en OU :
      - createdate : dossier ouvert pendant la fenêtre ;
      - hs_v2_date_entered_current_stage : dernier mouvement d'étape, ce qui
        rattrape les dossiers ouverts AVANT la campagne mais réactivés.

    Réserve : la propriété ne garde que le DERNIER mouvement. Un dossier
    déplacé le 15/08 puis le 25/08 n'expose que le 25/08.
    """
    for key in ("createdate", "hs_v2_date_entered_current_stage"):
        d = to_dt(props.get(key))
        if in_backfill(d):
            continue
        tag = in_windows(d, wins)
        if tag:
            return tag
    return None


def first_outbound_call(contact_ids, since_ms):
    """Premier appel SORTANT loggé pour chaque contact, après l'envoi du batch.

    Marqueur de prise en main commerciale. On garde le PREMIER : la question
    d'attribution est de savoir si un signal marketing a précédé le premier
    contact sortant, pas le dernier.
    """
    out = {}
    if not contact_ids:
        return out
    calls = _objects_assoc(
        CALLS, contact_ids,
        [{"propertyName": "hs_call_direction", "operator": "EQ", "value": "OUTBOUND"},
         {"propertyName": "hs_timestamp", "operator": "GTE", "value": str(since_ms)}],
        ["hs_timestamp", "hs_call_direction"])
    cmap = _contacts_of("calls", list(calls.keys()))
    for kid, p in calls.items():
        when = to_dt(p.get("hs_timestamp"))
        if not when:
            continue
        for c in cmap.get(kid, []):
            if c not in out or when < out[c]:
                out[c] = when
    return out


def attribute(cid, simulated, replies, rdv_pub, calls):
    """Origine de l'activation d'un contact : marketing, sales, ou ni l'un ni l'autre.

    Ordre volontaire, la simulation prime sur tout :
      1. une simulation réelle (last_step_date) → marketing, même si un appel
         a suivi : le client était déjà entré dans le parcours produit ;
      2. une réponse à une séquence OU un RDV réservé en self-service,
         ANTÉRIEUR au premier appel sortant → marketing ;
      3. un appel sortant loggé → sales ;
      4. rien de tout ça → non attribuable.

    PIÈGE CENTRAL sur le RDV : on compare la date de RÉSERVATION
    (hs_createdate) au premier appel, JAMAIS la date de tenue
    (hs_meeting_start_time). Cas réel : réservation le 11/09, rendez-vous le
    15/09, appel commercial le 15/09 à l'heure du rendez-vous. Avec la date de
    tenue, le contact bascule à tort en sales — 5 erreurs sur 57 venaient de là.
    """
    if cid in simulated:
        return ("marketing", "simulation")
    call = calls.get(cid)
    sigs = []
    if replies.get(cid):
        sigs.append((replies[cid], "reponse"))
    if rdv_pub.get(cid):
        sigs.append((rdv_pub[cid], "rdv_public"))
    if sigs:
        sigs.sort(key=lambda x: x[0])
        when, kind = sigs[0]
        if call is None or when < call:
            return ("marketing", kind)
    if call:
        return ("sales", None)
    return ("non_attribuable", None)


def qualify(cid, bucket, sub, mset):
    """Statut de qualification d'un contact activé : certain, ou en attente.

    Définition arrêtée le 17/09/2026 avec Clémence. Un client est un lead dans
    trois cas, et uniquement dans ces trois cas :
      1. il prend lui-même un créneau, suite à nos e-mails ou depuis l'app ;
      2. il démarre son parcours de son propre chef ;
      3. outbound : on l'a eu au téléphone, ça peut l'intéresser, ET il veut
         qu'on organise un RDV pour en parler.

    LECTURE DU CAS 3 : le résultat attendu d'un outbound qualifié est un RDV
    organisé. Un contact qui a un rendez-vous posé par un commercial a donc,
    par construction, franchi les deux conditions — il a été joint, et il a
    voulu qu'on lui cale un créneau. Il est CERTAIN.
    Réserve : rien dans HubSpot ne dit si le créneau a été honoré. La règle
    porte sur l'intention exprimée au téléphone, pas sur la tenue du RDV.

    RESTE EN ATTENTE : les contacts qui n'ont qu'une fiche ouverte dans
    HubSpot, sans aucun rendez-vous. On ne sait pas si l'appel a produit un
    accord ou un refus. C'est la population que Clémence pointe : sur
    151 contacts appelés, 37 avaient une carte dont 21 en optimization_declined.

    POURQUOI UNE RÉPONSE NE SUFFIT PAS : hs_sales_email_last_replied enregistre
    n'importe quelle réponse, y compris « ça ne m'intéresse pas », un message
    d'absence ou une demande de désinscription. Rien ne distingue un refus d'un
    signal d'intérêt. Une réponse sans RDV ni parcours reste en attente.
    """
    if sub == "rdv_public":
        return "certain", "rdv_client"      # cas 1
    if sub == "simulation":
        return "certain", "parcours"        # cas 2
    if cid in mset:
        return "certain", "rdv_sales"       # cas 3, RDV organisé
    return "attente", "carte_seule"


def empty_qual():
    return dict(certain=0, attente=0, certain_rdv_client=0, certain_parcours=0,
                certain_rdv_sales=0, attente_carte_seule=0)


def empty_attr():
    return dict(marketing=dict(total=0, simulation=0, reponse=0, rdv_public=0),
                sales=dict(total=0), non_attribuable=dict(total=0))


def add_attr(acc, bucket, sub):
    acc[bucket]["total"] += 1
    if bucket == "marketing" and sub:
        acc["marketing"][sub] += 1


def business_hours_between(a, b):
    """Heures ouvrées (lundi-vendredi) entre deux instants. Jours fériés ignorés."""
    if not a or b <= a:
        return 0
    h, cur = 0, a.replace(minute=0, second=0, microsecond=0)
    while cur < b and h < 24 * 30:
        if cur.weekday() < 5:
            h += 1
        cur += dt.timedelta(hours=1)
    return h


def last_integration_move(pipeline):
    """Date du dernier signe de vie de n8n sur ce pipe.

    Sert au drapeau sync_stale. Sans lui, une panne du flux produit des faux
    « sales » en silence : un client qui a simulé n'a pas de dossier remonté,
    donc aucun signal marketing, donc il bascule sur l'appel du commercial.
    C'est exactement ce qui s'est produit du 13/08 au 16/09.
    """
    body = {"filterGroups": [{"filters": [
        {"propertyName": "pipeline", "operator": "EQ", "value": pipeline},
        {"propertyName": "hs_object_source_label", "operator": "EQ",
         "value": "INTEGRATION"}]}],
        "properties": ["createdate", "hs_v2_date_entered_current_stage"],
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
        "limit": 1}
    r = post(DEALS, body).get("results") or []
    if not r:
        return None
    p = r[0]["properties"]
    ds = [x for x in (to_dt(p.get("createdate")),
                      to_dt(p.get("hs_v2_date_entered_current_stage"))) if x]
    return max(ds) if ds else None


def engagement_sets(ids, cohort_send, pipeline, meet_f, meet_f_attr, rmap):
    """Ensembles d'activation, plus les signaux nécessaires à l'attribution.

    Retourne des ENSEMBLES de contacts, jamais des compteurs d'objets : un
    même contact peut avoir un RDV courtage puis un RDV devis.

    DEUX collectes de réunions, volontairement :
      - meet_f porte le filtre d'intitulé et sert la définition d'ACTIVATION,
        inchangée depuis le 22/08 ;
      - meet_f_attr ne le porte pas et sert l'ATTRIBUTION, via
        hs_meeting_source. Le filtre d'intitulé rate les rendez-vous pris par
        le lien générique « Rendez-vous téléphonique Nopillo » : acceptable
        pour l'activation, faux pour l'attribution. Toucher au premier
        changerait des chiffres déjà publiés, on ne le fait pas.
    """
    keep = set(ids)
    wins = {c: windows_for(c, cohort_send, rmap) for c in keep}

    # ---- dossiers courtage
    deals = _objects_assoc(
        DEALS, ids,
        [{"propertyName": "pipeline", "operator": "EQ", "value": pipeline}],
        ["createdate", "dealstage", "hs_v2_date_entered_current_stage",
         "hs_object_source_label", "origine_creation_deal_ae", SIMU_PROP])
    dmap = _contacts_of("deals", list(deals.keys()))
    dset, d_auto, d_wave, simulated = set(), set(), {}, set()
    for did, props in deals.items():
        stage = str(props.get("dealstage") or "")
        # SEULE une étape de parcours prouve une activation.
        #  - optimization_activated : accès au simulateur, aucune étape. Non compté.
        #  - carte créée par le workflow « RDV sans simu » : aucune information
        #    propre, elle existe PARCE QU'un RDV existe, et le RDV est déjà
        #    compté via MEETING. Non comptée.
        #  - optimization_declined : étape terminale pilotée par le CS. Le flow
        #    n8n ne l'écrit plus et la protège, donc elle efface l'information
        #    de parcours. Non comptée : le contact reste activable par un RDV,
        #    sinon il part en attente de qualification.
        has_simu = stage in SIMU_STAGES
        for c in dmap.get(did, []):
            if c not in keep:
                continue
            # La simulation est un FAIT sur le contact, pas sur la fenêtre :
            # elle vaut même si la transaction n'entre pas dans l'attribution.
            if has_simu:
                simulated.add(c)
            if not has_simu:
                continue
            tag = deal_engages(props, wins[c])
            if not tag:
                continue
            dset.add(c)
            if d_wave.get(c) != "v2":
                d_wave[c] = tag
            # Un contact est classé « via n8n » dès qu'AU MOINS UNE de ses
            # transactions vient de l'INTÉGRATION : c'est le signal le plus fort
            # dont on dispose sur un parcours réellement produit.
            # Le test portait avant sur « différent de CRM_UI », ce qui rangeait
            # les transactions créées par un workflow HubSpot
            # (AUTOMATION_PLATFORM) du côté n8n — alors qu'aucun client n'avait
            # simulé. Corrigé le 25/09 après la mise en place du workflow
            # « RDV sans simu ».
            if props.get("hs_object_source_label") == "INTEGRATION":
                d_auto.add(c)

    # ---- rendez-vous, périmètre ACTIVATION (filtre d'intitulé)
    meets = _objects_assoc(MEETINGS, ids, meet_f,
                           ["hubspot_owner_id", "hs_timestamp", "hs_createdate"])
    mmap = _contacts_of("meetings", list(meets.keys()))
    mset, m_owner, m_wave = set(), {}, {}
    # Tri chronologique : un contact ayant plusieurs RDV est attribué au
    # propriétaire du PREMIER, celui qui a converti.
    for mid, p in sorted(meets.items(),
                         key=lambda x: x[1].get("hs_createdate") or ""):
        booked = to_dt(p.get("hs_createdate")) or to_dt(p.get("hs_timestamp"))
        for c in mmap.get(mid, []):
            if c not in keep:
                continue
            tag = in_windows(booked, wins[c])
            if not tag:
                continue
            mset.add(c)
            m_owner.setdefault(c, p.get("hubspot_owner_id"))
            if m_wave.get(c) != "v2":
                m_wave[c] = tag

    # ---- rendez-vous, périmètre ATTRIBUTION (sans filtre d'intitulé)
    # On retient la date de RÉSERVATION du premier RDV self-service.
    meets_a = _objects_assoc(MEETINGS, ids, meet_f_attr,
                             ["hs_meeting_source", "hs_createdate"])
    amap = _contacts_of("meetings", list(meets_a.keys()))
    rdv_pub = {}
    for mid, p in meets_a.items():
        if (p.get("hs_meeting_source") or "").upper() != MEETING_PUBLIC:
            continue
        booked = to_dt(p.get("hs_createdate"))
        if not booked:
            continue
        for c in amap.get(mid, []):
            if c in keep and (c not in rdv_pub or booked < rdv_pub[c]):
                rdv_pub[c] = booked

    return dset, mset, m_owner, d_auto, d_wave, m_wave, simulated, rdv_pub


# -------------------------------------------------------------------- build
def load_config():
    with open("cohorts.json", encoding="utf-8") as f:
        return json.load(f)


def cumulative_curve(delays, enrolled, send, horizon_max=21):
    """Part des CONTACTS ayant répondu au plus tard à J+n.

    Le dénominateur est l'effectif ciblé, pas le nombre de répondants : une
    courbe rapportée aux répondants finit toujours à 100 %, ce qui se lit comme
    « tout le monde a répondu » alors que c'est une tautologie.
    """
    if not delays or not enrolled:
        return []
    elapsed = (dt.datetime.now(dt.timezone.utc) - send).days
    horizon = max(0, min(horizon_max, elapsed))
    return [dict(day=j, count=sum(1 for x in delays if x <= j),
                 share=round(100 * sum(1 for x in delays if x <= j) / enrolled, 2))
            for j in range(horizon + 1)]


def activation_split(mset, dset, auto):
    """Décomposition de l'activation, en CONTACTS uniques.

    - les deux totaux qui se recoupent : `meet` et `deal` ;
    - les trois sous-ensembles disjoints : `both`, `meet_only`, `deal_only` ;
    - le total : `activated` = meet + deal − both, JAMAIS meet + deal.
    """
    return dict(
        meet=len(mset), deal=len(dset), both=len(mset & dset),
        meet_only=len(mset - dset), deal_only=len(dset - mset),
        activated=len(mset | dset),
        deal_auto=len(auto), deal_manual=len(dset - auto),
        deal_only_manual=len((dset - mset) - auto),
    )


def build():
    cfg = load_config()
    owners = owners_map()
    pipeline = cfg["deal_pipeline"]
    frozen = cfg.get("frozen_metrics", {})
    all_lists = [c["list_id"] for co in cfg["cohorts"] for c in co["cells"]]

    # Vagues de relance : contact -> date du dernier envoi qui l'a touché.
    rmap = build_relance_map(cfg)
    if rmap:
        print(f"relances : {len(rmap)} contact(s) relancé(s) sur "
              f"{len(cfg.get('relances', []))} liste(s)")

    camp_meet, camp_deal, camp_deal_auto = set(), set(), set()
    camp_v2 = set()

    # Attribution sales / marketing. Trois cas DISJOINTS dont la somme fait le
    # total activé : le total ne change pas, seule sa décomposition est ajoutée.
    camp_attr = empty_attr()
    attr_cells, attr_v1, attr_v2 = {}, empty_attr(), empty_attr()
    camp_qual, qual_cells = empty_qual(), {}
    last_sync = last_integration_move(pipeline)
    sync_stale = business_hours_between(last_sync,
                                        dt.datetime.now(dt.timezone.utc)) > 24
    if sync_stale:
        print(f"\n   /!\\ SYNC OBSOLETE : dernier mouvement n8n sur le pipe "
              f"{last_sync.isoformat() if last_sync else 'jamais'}. "
              f"Les contacts ayant simulé depuis n'ont pas de dossier remonté : "
              f"ils basculent a tort en sales.\n")

    cohorts = []
    for co in cfg["cohorts"]:
        send = iso(co["sent_at"])
        since_ms = int(send.timestamp() * 1000)
        forced = (co.get("status") or "AUTO").upper()
        cells, all_delays = [], []
        coh_meet, coh_deal, coh_deal_auto, coh_v2 = set(), set(), set(), set()

        for c in co["cells"]:
            members = list_members(c["list_id"])
            ids = [m[0] for m in members]

            agg, steps = emails_for(ids, [c["sequence_id"]], since_ms)

            # Réponses, ouvertures et clics au CONTACT, pas à l'e-mail.
            delays = []
            n_open = n_click = 0
            for _, _, rep, op, cl in members:
                w = to_dt(rep)
                if w and w >= send:
                    delays.append((w - send).days)
                wo = to_dt(op)
                if wo and wo >= send:
                    n_open += 1
                wc = to_dt(cl)
                if wc and wc >= send:
                    n_click += 1
            all_delays += delays

            # MÉTRIQUES GELÉES. hs_sales_email_last_opened et
            # hs_sales_email_last_clicked sont des propriétés « dernière fois »
            # sans mémoire : une ouverture de relance écrase la date d'août et
            # reste comptée comme une ouverture d'août, puisque la comparaison
            # est >= date d'envoi. Borner en haut ne réglerait rien — un vrai
            # ouvreur d'août sortirait alors du compte. La donnée d'origine
            # n'existe plus : on fige le dernier relevé propre.
            fz = frozen.get(str(c["list_id"]))
            if fz:
                n_open = fz.get("opens", n_open)
                n_click = fz.get("clicks", n_click)
                agg["opens"] = fz.get("opens_emails", agg["opens"])
                agg["clicks"] = fz.get("clicks_emails", agg["clicks"])

            meet_f = [{"propertyName": "hs_createdate", "operator": "GTE",
                       "value": str(since_ms)},
                      {"propertyName": "hubspot_owner_id", "operator": "IN",
                       "values": AE_MEETING_OWNERS}]
            # Périmètre ATTRIBUTION : même bornes, SANS filtre d'intitulé.
            meet_f_attr = list(meet_f)
            mf = cfg.get("meeting_filter")
            if mf:
                meet_f.append({"propertyName": mf["property"],
                               "operator": mf["operator"], "value": mf["value"]})

            (dset, mset, m_owner, d_auto, d_wave, m_wave,
             simulated, rdv_pub) = engagement_sets(
                ids, send, pipeline, meet_f, meet_f_attr, rmap)

            # Signaux d'attribution restants : réponses et premier appel sortant.
            reply_at = {}
            for cid, _, rep, _, _ in members:
                w = to_dt(rep)
                if w and w >= send:
                    reply_at[cid] = w
            calls_at = first_outbound_call(ids, since_ms)

            cell_attr, cell_qual = empty_attr(), empty_qual()
            attr_ids = {"marketing_simulation": [], "marketing_reponse": [],
                        "marketing_rdv_public": [], "sales": [], "non_attribuable": []}
            qual_ids = {"certain_rdv_client": [], "certain_parcours": [],
                        "certain_rdv_sales": [], "attente_carte_seule": []}
            for cid in (dset | mset):
                bucket, sub = attribute(cid, simulated, reply_at, rdv_pub, calls_at)
                attr_ids[f"{bucket}_{sub}" if sub else bucket].append(cid)
                add_attr(cell_attr, bucket, sub)
                add_attr(camp_attr, bucket, sub)
                q, qsub = qualify(cid, bucket, sub, mset)
                for acc in (cell_qual, camp_qual):
                    acc[q] += 1
                    acc[f"{q}_{qsub}"] += 1
                qual_ids[f"{q}_{qsub}"].append(cid)
                add_attr(attr_v2 if cid in ({x for x, t in d_wave.items() if t == "v2"} |
                                            {x for x, t in m_wave.items() if t == "v2"})
                         else attr_v1, bucket, sub)
            attr_cells[c["list_id"]] = cell_attr
            qual_cells[c["list_id"]] = cell_qual

            # Vague 2 : contacts dont l'activation est tombée dans la fenêtre
            # de relance, donc attribuable à la relance et non au batch.
            v2 = {x for x, t in d_wave.items() if t == "v2"} | \
                 {x for x, t in m_wave.items() if t == "v2"}
            relanced = {x for x in ids if x in rmap}

            split = dict(both=len(dset & mset), meet_only=len(mset - dset),
                         deal_only=len(dset - mset), engaged=len(dset | mset),
                         deal_n8n=len(d_auto), deal_manual=len(dset - d_auto),
                         deal_only_manual=len((dset - mset) - d_auto),
                         relanced=len(relanced), engaged_v2=len(v2),
                         engaged_v1=len((dset | mset) - v2),
                         marketing=cell_attr["marketing"]["total"],
                         sales=cell_attr["sales"]["total"],
                         non_attribuable=cell_attr["non_attribuable"]["total"],
                         simule=cell_attr["marketing"]["simulation"],
                         certain=cell_qual["certain"],
                         attente=cell_qual["attente"])
            n_meet, n_deal = len(mset), len(dset)

            coh_meet |= mset
            coh_deal |= dset
            coh_deal_auto |= d_auto
            coh_v2 |= v2

            m_own = {}
            for cid in mset:
                k = m_owner.get(cid)
                m_own[k] = m_own.get(k, 0) + 1

            active = count_active(c["list_id"], [c["sequence_id"]])

            cells.append(dict(
                list_id=c["list_id"], list_name=c.get("list_name"),
                sequence_id=c["sequence_id"], audience=c["audience"],
                version=c.get("version"),
                enrolled=len(members), active=active,
                status=(forced if forced in ("TERMINE", "EN_COURS")
                        else ("EN_COURS" if active > 0 else "TERMINE")),
                sent=agg["sent"], bounced=agg["bounced"],
                opens=n_open, clicks=n_click,
                opens_emails=agg["opens"], clicks_emails=agg["clicks"],
                opens_frozen=bool(fz),
                replies=len(delays), meetings=n_meet, deals_ae=n_deal,
                engaged=split["engaged"], split=split,
                # Détail par contact, RETIRÉ avant l'écriture de data.json :
                # ce fichier est servi publiquement par GitHub Pages.
                _ids=dict(both=sorted(dset & mset), meet_only=sorted(mset - dset),
                          deal_only=sorted(dset - mset), v2=sorted(v2),
                          attr={k: sorted(v) for k, v in attr_ids.items()},
                          qual={k: sorted(v) for k, v in qual_ids.items()}),
                steps=steps,
                by_owner=[dict(owner_id=o, owner=owners.get(o, "Non attribué"),
                               meetings=n)
                          for o, n in sorted(m_own.items(), key=lambda x: str(x[0]))],
            ))

        n_act = sum(c["active"] for c in cells)
        done = [c for c in cells if c["status"] == "TERMINE"]
        status = ("TERMINE" if n_act == 0 else ("PARTIEL" if done else "EN_COURS"))
        note = None if n_act == 0 else (
            f"{n_act} contact(s) encore en séquence sur cette cohorte"
            + (f", mais la cellule {done[0]['audience']} a fini d'envoyer "
               f"et alimente déjà la référence." if done
               else ". Les chiffres vont encore monter."))
        camp_meet |= coh_meet
        camp_deal |= coh_deal
        camp_deal_auto |= coh_deal_auto
        camp_v2 |= coh_v2

        act = activation_split(coh_meet, coh_deal, coh_deal_auto)
        act["activated_v2"] = len(coh_v2)
        act["activated_v1"] = act["activated"] - len(coh_v2)
        act["relanced"] = sum(c["split"]["relanced"] for c in cells)

        cohorts.append(dict(id=co["id"], label=co["label"], sent_at=co["sent_at"],
                            status=status, active=n_act, status_note=note,
                            ab_test=co.get("ab_test", True), ab_note=co.get("ab_note"),
                            targeting=co.get("targeting"),
                            cells=cells, activation=act,
                            reply_curve=cumulative_curve(
                                all_delays, sum(c["enrolled"] for c in cells), send)))
    cohorts.sort(key=lambda x: x["id"])

    # Union dédupliquée : les cohortes peuvent se recouper
    dedup = dict(contacts=count_lists(all_lists))
    somme = sum(c["enrolled"] for co in cohorts for c in co["cells"])
    ecart = somme - dedup["contacts"]

    dedup["activation"] = activation_split(camp_meet, camp_deal, camp_deal_auto)
    somme_act = sum(c["split"]["engaged"] for co in cohorts for c in co["cells"])
    dedup["activation"]["sum_cells"] = somme_act
    dedup["activation"]["overlap"] = somme_act - dedup["activation"]["activated"]
    dedup["activation"]["activated_v2"] = len(camp_v2)
    dedup["activation"]["activated_v1"] = (dedup["activation"]["activated"]
                                           - len(camp_v2))
    dedup["relanced"] = len(rmap)

    # Décomposition sales / marketing. Nouvel axe, sans rupture : le total
    # activé est inchangé, marketing + sales + non attribuable = activés.
    dedup["attribution"] = dict(
        marketing=camp_attr["marketing"], sales=camp_attr["sales"],
        non_attribuable=camp_attr["non_attribuable"],
        par_cellule=attr_cells, par_vague=dict(v1=attr_v1, v2=attr_v2),
        qualification=dict(camp_qual, par_cellule=qual_cells),
        sync_stale=sync_stale,
        last_sync=last_sync.isoformat() if last_sync else None)

    data = dict(
        meta=dict(
            campaign=cfg["campaign"],
            generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            collected=True,
            primary_axis=cfg.get("primary_axis", "cohort"),
            primary_kpi=cfg.get("primary_kpi"),
            source=("HubSpot · listes statiques ∩ EMAIL.hs_sequence_id "
                    "· MEETING_EVENT ∪ DEAL pipeline " + pipeline
                    + f" · contacts uniques, fenêtre J+{ATTRIB_DAYS} par contact"),
            attribution_note=cfg["notes"]["attribution"],
            relance_note=cfg["notes"].get("relances"),
            meeting_window_note=cfg["notes"].get("meeting_window"),
            attribution_window_note=cfg["notes"].get("attribution_window"),
            frozen_note=cfg.get("frozen_metrics", {}).get("_doc"),
            attribution_sm_note=cfg["notes"].get("attribution_sales_marketing"),
            overlap_note=(
                f"Recoupement entre cohortes : {ecart} contact(s) ciblés dans "
                f"plusieurs batchs. Le niveau 1 utilise l'union dédupliquée."
                if ecart > 0 else "Aucun recoupement entre les cohortes."),
            fix_note=("Attribution par appartenance aux listes. Les séquences étant "
                      "réutilisées d'un batch à l'autre, une attribution par "
                      "hs_sequence_id imputerait les envois d'un batch au précédent."),
        ),
        kpis=cfg["kpis"],
        audience_labels=cfg["audience_labels"],
        stats_config=cfg["stats"],
        relances=[dict(r) for r in cfg.get("relances", [])],
        dedup=dedup,
        cohorts=cohorts,
    )

    # Détail nominatif : dans les logs du run, JAMAIS dans data.json.
    # Le pop() ci-dessous est ce qui garantit que _ids ne fuite pas dans le
    # JSON — ne pas le déplacer après json.dump.
    print("\n--- détail des contacts engagés ---")
    for co in cohorts:
        for c in co["cells"]:
            ids = c.pop("_ids")
            v2 = set(ids["v2"])
            print(f"\n{co['id']} · {c['audience']}-{c['version']} "
                  f"· {c['split']['engaged']} engagés sur {c['enrolled']} ciblés")
            for cat, label in (("both", "RDV + dossier"),
                               ("meet_only", "RDV seul"),
                               ("deal_only", "dossier seul")):
                if ids[cat]:
                    print(f"  {label} ({len(ids[cat])})")
                    for cid in ids[cat]:
                        flag = "  [vague 2]" if cid in v2 else ""
                        print(f"    https://app-eu1.hubspot.com/contacts/"
                              f"{PORTAL}/contact/{cid}{flag}")
            # Mêmes contacts, relus par ORIGINE de l'activation. Les cinq
            # catégories sont disjointes : un contact apparaît une seule fois.
            # C'est ici qu'on vérifie un arbitrage douteux, fiche par fiche.
            qua = ids.get("qual") or {}
            if any(qua.values()):
                print(f"  — qualification —")
                for cat, label in (("certain_rdv_client", "confirmé · cas 1, RDV pris par le client"),
                                   ("certain_parcours", "confirmé · cas 2, parcours démarré"),
                                   ("certain_rdv_sales", "confirmé · cas 3, RDV organisé après appel"),
                                   ("attente_carte_seule", "EN ATTENTE · carte seule, À ARBITRER")):
                    if qua.get(cat):
                        print(f"  {label} ({len(qua[cat])})")
                        for cid in qua[cat]:
                            print(f"    https://app-eu1.hubspot.com/contacts/"
                                  f"{PORTAL}/contact/{cid}")
            att = ids.get("attr") or {}
            if any(att.values()):
                print(f"  — origine de l'activation —")
                for cat, label in (("marketing_simulation", "marketing · a simulé"),
                                   ("marketing_reponse", "marketing · a répondu"),
                                   ("marketing_rdv_public", "marketing · RDV self-service"),
                                   ("sales", "sales · appel sortant d'abord"),
                                   ("non_attribuable", "non attribuable")):
                    if att.get(cat):
                        print(f"  {label} ({len(att[cat])})")
                        for cid in att[cat]:
                            flag = "  [vague 2]" if cid in v2 else ""
                            print(f"    https://app-eu1.hubspot.com/contacts/"
                                  f"{PORTAL}/contact/{cid}{flag}")
    print("--- fin du détail ---\n")

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    n_cells = sum(len(c["cells"]) for c in cohorts)
    tot = {k["key"]: sum(c[k["key"]] for co in cohorts for c in co["cells"])
           for k in cfg["kpis"]}
    print(f"OK · {len(cohorts)} cohortes · {n_cells} cellules · "
          f"{dedup['contacts']} contacts ciblés · {len(rmap)} relancés")
    print("   " + " · ".join(f"{k['label']} {tot[k['key']]}" for k in cfg["kpis"]))
    for co in cohorts:
        for c in co["cells"]:
            s = c["split"]
            fz = " [ouvertures gelées]" if c.get("opens_frozen") else ""
            print(f"   {co['id']} {c['audience']}-{c['version']} "
                  f"n={c['enrolled']} · both {s['both']} · rdv seul {s['meet_only']} "
                  f"· deal seul {s['deal_only']} · engagés {s['engaged']} "
                  f"(v1 {s['engaged_v1']} · v2 {s['engaged_v2']} "
                  f"sur {s['relanced']} relancés)"
                  f" | deals n8n {s['deal_n8n']} · manuels {s['deal_manual']}"
                  f"{fz}")
    if ecart > 0:
        print(f"   recoupement : somme des cellules {somme} vs union {dedup['contacts']}")

    a = dedup["activation"]
    print("\n--- clients activés · campagne, dédupliqué ---")
    print(f"   RDV pris (uniques)        {a['meet']:5d}")
    print(f"   Dossiers AE (uniques)     {a['deal']:5d}"
          f"   dont n8n {a['deal_auto']} · à la main {a['deal_manual']}")
    print(f"   Les deux                  {a['both']:5d}")
    print(f"   = TOTAL ACTIVÉS           {a['activated']:5d}"
          f"   soit {100 * a['activated'] / dedup['contacts']:.2f} % des ciblés")
    print(f"   dont RDV seul {a['meet_only']} · dossier seul {a['deal_only']}"
          f" (dont {a['deal_only_manual']} créé(s) à la main)")
    if rmap:
        print(f"\n   vague 1 (envoi initial)   {a['activated_v1']:5d}")
        print(f"   vague 2 (relance 11/09)   {a['activated_v2']:5d}"
              f"   sur {len(rmap)} relancés"
              f" · {100 * a['activated_v2'] / len(rmap):.2f} %")
        print("   ATTENTION : les listes de relance ne contiennent QUE des")
        print("   contacts non activés. Ce taux n'est PAS comparable à celui")
        print("   d'un batch initial — c'est un gain marginal pur.")
    if a["overlap"]:
        print(f"   ⚠ somme des cellules {a['sum_cells']} vs union {a['activated']} :"
              f" {a['overlap']} contact(s) activé(s) ciblé(s) dans deux batchs")

    at = dedup["attribution"]
    mk, sl, na = at["marketing"], at["sales"], at["non_attribuable"]
    tot = mk["total"] + sl["total"] + na["total"]
    print("\n--- origine de l'activation · sales contre marketing ---")
    print(f"   MARKETING                 {mk['total']:5d}   {pcts(mk['total'], tot)}")
    print(f"     dont simulation         {mk['simulation']:5d}")
    print(f"     dont réponse séquence   {mk['reponse']:5d}")
    print(f"     dont RDV self-service   {mk['rdv_public']:5d}")
    print(f"   SALES                     {sl['total']:5d}   {pcts(sl['total'], tot)}")
    print(f"   NON ATTRIBUABLE           {na['total']:5d}   {pcts(na['total'], tot)}")
    print(f"   = TOTAL                   {tot:5d}   doit égaler {a['activated']} activés"
          f" · {'OK' if tot == a['activated'] else 'ÉCART'}")
    q = at["qualification"]
    print("\n--- qualification · définition du 17/09 ---")
    print(f"   ACTIVÉS CONFIRMÉS          {q['certain']:5d}   {pcts(q['certain'], tot)}")
    print(f"     cas 1 · RDV pris par le client   {q['certain_rdv_client']:5d}")
    print(f"     cas 2 · parcours démarré         {q['certain_parcours']:5d}")
    print(f"     cas 3 · RDV organisé après appel {q['certain_rdv_sales']:5d}")
    print(f"   EN ATTENTE DE QUALIFICATION   {q['attente']:5d}   {pcts(q['attente'], tot)}")
    print(f"     carte ouverte, aucun RDV         {q['attente_carte_seule']:5d}"
          f"   accord ou refus : à arbitrer")
    print(f"   = TOTAL POTENTIEL         {q['certain'] + q['attente']:5d}"
          f"   doit égaler {a['activated']} activés"
          f" · {'OK' if q['certain'] + q['attente'] == a['activated'] else 'ÉCART'}")
    print("   Une réponse à un mail NE SUFFIT PAS : la propriété HubSpot ne")
    print("   distingue pas « ça m'intéresse » d'un refus ou d'un message")
    print("   d'absence. Ces contacts vont en attente, pas en activés.")
    v1a, v2a = at["par_vague"]["v1"], at["par_vague"]["v2"]
    print(f"   vague 1 : marketing {v1a['marketing']['total']} · "
          f"sales {v1a['sales']['total']} · na {v1a['non_attribuable']['total']}")
    print(f"   vague 2 : marketing {v2a['marketing']['total']} · "
          f"sales {v2a['sales']['total']} · na {v2a['non_attribuable']['total']}")
    if at["sync_stale"]:
        print("   ⚠ sync_stale : aucun mouvement n8n depuis plus de 24 h ouvrées.")
        print("     Les contacts ayant simulé depuis n'ont pas de dossier remonté")
        print("     et basculent à tort en sales. Chiffres à ne pas publier.")


if __name__ == "__main__":
    build()
