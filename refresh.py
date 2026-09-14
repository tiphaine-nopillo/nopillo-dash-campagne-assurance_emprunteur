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
BACKFILL = [("2026-08-06T15:25:00Z", "2026-08-06T15:26:00Z")]

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


def deal_engages(props, wins):
    """Étiquette de vague si la transaction entre dans une fenêtre, sinon None.

    AUCUN filtre sur dealstage, volontairement : les transactions remontées
    par n8n sautent des étapes, une étape absente ne prouve rien.

    AUCUN filtre sur la source non plus, mais c'est un arbitrage non tranché :
    une part importante des transactions est créée à la main et atteste qu'un
    commercial a ouvert une fiche, pas qu'un client a simulé. La répartition
    par source est collectée pour rendre l'arbitrage visible.

    Deux bornes, en OU :
      - createdate : dossier ouvert pendant la fenêtre ;
      - hs_v2_date_entered_current_stage : dernier mouvement d'étape, ce qui
        rattrape les dossiers ouverts AVANT la campagne mais réactivés.

    Réserve : la propriété ne garde que le DERNIER mouvement. Un dossier
    déplacé le 15/08 puis le 25/08 n'expose que le 25/08.
    """
    created = to_dt(props.get("createdate"))
    if created and any(to_dt(a) <= created < to_dt(b) for a, b in BACKFILL):
        return None
    for key in ("createdate", "hs_v2_date_entered_current_stage"):
        tag = in_windows(to_dt(props.get(key)), wins)
        if tag:
            return tag
    return None


def engagement_sets(ids, cohort_send, pipeline, meet_f, rmap):
    """Contacts ayant un dossier, contacts ayant un RDV, qui a posé le RDV,
    et l'étiquette de vague de chaque activation.

    Retourne des ENSEMBLES de contacts, jamais des compteurs d'objets : un
    même contact peut avoir un RDV courtage puis un RDV devis. L'unité de
    mesure est le contact.

    CHANGEMENT DE CETTE VERSION : l'appartenance à la fenêtre est évaluée
    CONTACT PAR CONTACT, parce qu'un contact relancé a deux fenêtres. Avant,
    une seule fenêtre valait pour toute la cellule — et les RDV n'en avaient
    aucune en borne haute.
    """
    keep = set(ids)
    wins = {c: windows_for(c, cohort_send, rmap) for c in keep}

    # ---- dossiers courtage
    deals = _objects_assoc(
        DEALS, ids,
        [{"propertyName": "pipeline", "operator": "EQ", "value": pipeline}],
        ["createdate", "dealstage", "hs_v2_date_entered_current_stage",
         "hs_object_source_label"])
    dmap = _contacts_of("deals", list(deals.keys()))
    dset, d_auto, d_wave = set(), set(), {}
    for did, props in deals.items():
        for c in dmap.get(did, []):
            if c not in keep:
                continue
            tag = deal_engages(props, wins[c])
            if not tag:
                continue
            dset.add(c)
            if d_wave.get(c) != "v2":
                d_wave[c] = tag
            # Un contact est classé « via n8n » dès qu'AU MOINS UNE de ses
            # transactions est automatique : c'est le signal le plus fort
            # dont on dispose sur un parcours réellement produit.
            if props.get("hs_object_source_label") != "CRM_UI":
                d_auto.add(c)

    # ---- rendez-vous
    # La borne haute est appliquée ICI, côté client, parce qu'elle dépend du
    # contact. Le filtre serveur meet_f ne porte que la borne basse, le
    # propriétaire et l'intitulé.
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

    return dset, mset, m_owner, d_auto, d_wave, m_wave


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
            mf = cfg.get("meeting_filter")
            if mf:
                meet_f.append({"propertyName": mf["property"],
                               "operator": mf["operator"], "value": mf["value"]})

            dset, mset, m_owner, d_auto, d_wave, m_wave = engagement_sets(
                ids, send, pipeline, meet_f, rmap)

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
                         engaged_v1=len((dset | mset) - v2))
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
                          deal_only=sorted(dset - mset), v2=sorted(v2)),
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


if __name__ == "__main__":
    build()
