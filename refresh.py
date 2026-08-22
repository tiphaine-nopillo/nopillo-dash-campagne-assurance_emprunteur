#!/usr/bin/env python3
"""
Suivi campagne Assurance Emprunteur · refresh.py
Alimente data.json depuis HubSpot.

CORRECTIF CENTRAL DE CETTE VERSION
----------------------------------
Le KPI principal devient l'ENGAGEMENT : union dédupliquée des contacts ayant
un dossier dans le pipe courtage AE et de ceux ayant pris un RDV. Quatre
changements de fond par rapport à la version précédente :

1. Aucun filtre sur les étapes du pipe. Les transactions remontées par n8n
   portent le statut rapporté par le partenaire et sautent des étapes : une
   étape absente ne prouve rien.
   ATTENTION : le pipe n'est PAS alimenté uniquement par le partenaire.
   Au 22/08, sur 77 transactions : 40 créées à la main dans HubSpot (CRM_UI),
   34 par n8n (INTEGRATION), 3 par automatisation. Une transaction manuelle
   atteste qu'un commercial a créé une fiche, pas qu'un client a simulé.
   Le KPI ne filtre pas sur la source — arbitrage volontairement non tranché
   — mais la répartition est collectée et affichée pour le rendre visible.

2. Les dossiers ouverts AVANT la campagne mais déplacés d'étape après l'envoi
   sont comptés. Sur la seule date de création, une réactivation est invisible
   — et cet angle mort grandit à mesure que la base mûrit.

3. RDV et dossiers se comptent en CONTACTS UNIQUES, plus en objets. 62 objets
   MEETING_EVENT correspondent à 51 contacts : certains ont un RDV courtage
   puis un RDV devis. Même correction d'unité que celle déjà appliquée aux
   ouvertures et aux clics.

4. Les RDV sont attribués au propriétaire de la RÉUNION, plus à celui du
   CONTACT. L'ancienne répartition faisait apparaître des commerciaux qui
   n'avaient posé aucun rendez-vous, simplement parce qu'ils possédaient les
   fiches. Les dossiers ne sont plus répartis du tout : créés par n8n, ils
   n'ont pas de propriétaire, et passer par celui du contact reproduirait la
   même confusion.

CORRECTIF DE LA VERSION PRÉCÉDENTE, TOUJOURS VALABLE
----------------------------------------------------
Les séquences sont RÉUTILISÉES d'un batch à l'autre : 841303267 a servi le RP
du 5 août puis celui du 13 août. Toute métrique est attribuée par APPARTENANCE
À LA LISTE de la cellule. La séquence ne sert qu'à restreindre le périmètre
des e-mails collectés.

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
ATTRIB_DAYS = 21

# Batch de rattrapage : 10 transactions créées en 20 secondes le 06/08, mêlant
# contacts enrôlés et contacts jamais touchés par une séquence. Import
# d'antériorité, pas de l'activité. 4 concernent des contacts de cohorte.
BACKFILL = [("2026-08-06T15:25:00Z", "2026-08-06T15:26:00Z")]

# Owner IDs des commerciaux habilités sur la campagne. Ce sont des Owner IDs,
# PAS des User IDs — HubSpot maintient les deux et ils ne sont pas
# interchangeables. Filtre sans effet au 22/08 (62 RDV sur 62 leur
# appartiennent) : c'est un garde-fou pour les cohortes suivantes.
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


def count_active(list_id, sequence_ids):
    """Contacts encore activement enrôlés dans UNE SÉQUENCE DE LA CAMPAGNE.

    Le filtre sur hs_sequences_is_enrolled seul ne suffit pas : cette propriété
    vaut vrai pour n'importe quelle séquence du portail. Mesuré sur le batch du
    5 août, elle renvoyait 4 contacts « encore en séquence » qui étaient en
    réalité dans la séquence audit patrimonial de mai, et l'un depuis 2024.
    Le batch était donc marqué PARTIEL à tort, ce qui masquait son test A/B.

    On croise donc avec hs_latest_sequence_enrolled. Réserve : cette propriété
    ne garde que la DERNIÈRE séquence — un contact encore dans la séquence de
    campagne mais réenrôlé ailleurs depuis échappera au décompte. Le biais va
    dans le sens de la prudence : on sous-estime les envois restants.
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


# ------------------------------------------------------------------- e-mails
def emails_for(contact_ids, sequence_ids, since_ms):
    """Envois, ouvertures, clics et réponses des e-mails de séquence reçus par
    ces contacts précisément.

    Le double filtre est ce qui corrige le bug : hs_sequence_id restreint aux
    séquences de la campagne, associations.contact restreint aux contacts de la
    cellule. Sans le second, un batch ultérieur partageant la séquence viendrait
    gonfler les chiffres.

    Compteurs bornés à 1 par e-mail pour obtenir de l'unique plutôt que le
    cumul brut renvoyé par HubSpot.
    """
    agg = dict(sent=0, bounced=0, opens=0, clicks=0)
    steps = {}
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
    """Objets associés à ces contacts, AVEC leurs propriétés.

    On a besoin des propriétés pour appliquer la règle d'engagement, et des
    IDs pour remonter au contact — d'où le retour d'objets plutôt qu'un
    simple compteur. Déduplication par ID : un même objet peut ressortir
    dans deux lots de contacts.
    """
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
    """Contacts associés à chaque objet, via l'API associations v4.

    L'API CRM Search ne renvoie pas les associations : il faut ce second
    appel. C'est le même constat qui avait vidé le décompte par propriétaire
    dans la version précédente.
    """
    m = {}
    ids = list(object_ids)
    for i in range(0, len(ids), CHUNK):
        d = post(f"/crm/v4/associations/{object_type}/contacts/batch/read",
                 {"inputs": [{"id": o} for o in ids[i:i + CHUNK]]})
        for r in d.get("results", []):
            m[r["from"]["id"]] = [str(t["toObjectId"]) for t in r["to"]]
    return m


def deal_engages(props, send):
    """La transaction matérialise-t-elle une entrée dans le flow après l'envoi ?

    AUCUN filtre sur dealstage, volontairement : les transactions remontées
    par n8n sautent des étapes, une étape absente ne prouve donc rien.

    AUCUN filtre sur la source non plus, mais pour une autre raison — c'est un
    arbitrage non tranché, pas une certitude. 40 transactions sur 77 sont
    créées à la main : elles attestent qu'un commercial a ouvert une fiche,
    pas qu'un client a parcouru le simulateur. La répartition par source est
    collectée pour rendre l'arbitrage visible (cf. engagement_sets).

    Deux bornes, en OU :
      - createdate : dossier ouvert pendant la fenêtre ;
      - hs_v2_date_entered_current_stage : dernier mouvement d'étape, ce qui
        rattrape les dossiers ouverts AVANT la campagne mais réactivés par
        elle. Un dossier de janvier réentré en simulation le 21/08 est ainsi
        correctement attribué au batch du 13 — sur createdate seul, il était
        invisible.

    Réserve : la propriété ne garde que le DERNIER mouvement. Un dossier
    déplacé le 15/08 puis le 25/08 n'expose que le 25/08.
    """
    created = to_dt(props.get("createdate"))
    if created and any(to_dt(a) <= created < to_dt(b) for a, b in BACKFILL):
        return False
    end = send + dt.timedelta(days=ATTRIB_DAYS)
    for key in ("createdate", "hs_v2_date_entered_current_stage"):
        d = to_dt(props.get(key))
        if d and send <= d <= end:
            return True
    return False


def engagement_sets(ids, send, pipeline, meet_f):
    """Contacts ayant un dossier, contacts ayant un RDV, et qui a posé ce RDV.

    Retourne deux ENSEMBLES de contacts, jamais des compteurs d'objets :
    62 réunions correspondent à 51 contacts, certains ayant un RDV courtage
    puis un RDV devis. L'unité de mesure est le contact.

    Le troisième retour associe chaque contact au propriétaire de la RÉUNION,
    pour la répartition par commercial. Le propriétaire du CONTACT ne convient
    pas : il fait apparaître des commerciaux qui n'ont posé aucun rendez-vous,
    simplement parce qu'ils possèdent les fiches.
    """
    keep = set(ids)

    deals = _objects_assoc(
        DEALS, ids,
        [{"propertyName": "pipeline", "operator": "EQ", "value": pipeline}],
        ["createdate", "dealstage", "hs_v2_date_entered_current_stage",
         "hs_object_source_label"])
    ok = [i for i, p in deals.items() if deal_engages(p, send)]
    dmap = _contacts_of("deals", ok)
    dset, d_auto = set(), set()
    for i in ok:
        for c in dmap.get(i, []):
            if c in keep:
                dset.add(c)
                # Un contact est classé « via n8n » dès qu'AU MOINS UNE de ses
                # transactions est automatique : c'est le signal le plus fort
                # dont on dispose sur un parcours réellement produit.
                if deals[i].get("hs_object_source_label") != "CRM_UI":
                    d_auto.add(c)

    meets = _objects_assoc(MEETINGS, ids, meet_f,
                           ["hubspot_owner_id", "hs_timestamp"])
    mmap = _contacts_of("meetings", meets)
    mset, m_owner = set(), {}
    # Tri chronologique : un contact ayant plusieurs RDV est attribué au
    # propriétaire du PREMIER, celui qui a converti.
    for mid, p in sorted(meets.items(), key=lambda x: x[1].get("hs_timestamp") or ""):
        for c in mmap.get(mid, []):
            if c in keep:
                mset.add(c)
                m_owner.setdefault(c, p.get("hubspot_owner_id"))

    return dset, mset, m_owner, d_auto


# -------------------------------------------------------------------- build
def load_config():
    with open("cohorts.json", encoding="utf-8") as f:
        return json.load(f)


def cumulative_curve(delays, enrolled, send, horizon_max=21):
    """Part des CONTACTS ayant répondu au plus tard à J+n.

    Le dénominateur est l'effectif ciblé, pas le nombre de répondants : une
    courbe rapportée aux répondants finit toujours à 100 %, ce qui se lit comme
    « tout le monde a répondu » alors que c'est une tautologie. Ici la courbe
    plafonne sur le vrai taux de réponse — on lit le rythme ET le niveau.

    L'horizon est borné aux jours réellement écoulés depuis l'envoi : afficher
    J+21 pour un batch parti il y a 5 jours dessinait un futur inexistant.
    """
    if not delays or not enrolled:
        return []
    elapsed = (dt.datetime.now(dt.timezone.utc) - send).days
    horizon = max(0, min(horizon_max, elapsed))
    return [dict(day=j, count=sum(1 for x in delays if x <= j),
                 share=round(100 * sum(1 for x in delays if x <= j) / enrolled, 2))
            for j in range(horizon + 1)]


def build():
    cfg = load_config()
    owners = owners_map()
    pipeline = cfg["deal_pipeline"]
    all_lists = [c["list_id"] for co in cfg["cohorts"] for c in co["cells"]]

    cohorts = []
    for co in cfg["cohorts"]:
        send = dt.datetime.fromisoformat(co["sent_at"].replace("Z", "+00:00"))
        since_ms = int(send.timestamp() * 1000)
        forced = (co.get("status") or "AUTO").upper()
        cells, all_delays = [], []

        for c in co["cells"]:
            members = list_members(c["list_id"])
            ids = [m[0] for m in members]

            agg, steps = emails_for(ids, [c["sequence_id"]], since_ms)

            # réponses : date de dernière réponse postérieure à l'envoi du batch
            # Ouvertures, clics et réponses au CONTACT, pas à l'e-mail : comptés
            # par e-mail sur une séquence à 3 étapes, ils dépassaient 100 % des
            # contacts (352 ouvertures pour 254 contacts). Vérifié contre HubSpot :
            # 176 contre 172 réels, l'écart résiduel venant du fait que ces
            # propriétés enregistrent tout e-mail commercial, pas seulement la campagne.
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

            meet_f = [{"propertyName": "hs_createdate", "operator": "GTE",
                       "value": str(since_ms)},
                      {"propertyName": "hubspot_owner_id", "operator": "IN",
                       "values": AE_MEETING_OWNERS}]
            mf = cfg.get("meeting_filter")
            if mf:
                meet_f.append({"propertyName": mf["property"],
                               "operator": mf["operator"], "value": mf["value"]})

            # Le filtre createdate côté transactions est retiré : il excluait
            # les dossiers ouverts AVANT la campagne mais réactivés par elle.
            # La règle temporelle est appliquée dans deal_engages().
            dset, mset, m_owner, d_auto = engagement_sets(
                ids, send, pipeline, meet_f)
            # Origine des transactions. Le pipe n'est PAS alimenté uniquement
            # par le partenaire : une transaction créée à la main atteste
            # qu'un commercial a ouvert une fiche, pas qu'un client a simulé.
            # deal_only_manual est la ligne à surveiller : elle prétend
            # mesurer du self-service alors qu'elle peut ne mesurer que de la
            # saisie commerciale sans RDV tracé.
            split = dict(both=len(dset & mset), meet_only=len(mset - dset),
                         deal_only=len(dset - mset), engaged=len(dset | mset),
                         deal_n8n=len(d_auto), deal_manual=len(dset - d_auto),
                         deal_only_manual=len((dset - mset) - d_auto))
            n_meet, n_deal = len(mset), len(dset)

            # RDV attribués au propriétaire de la RÉUNION : c'est le seul champ
            # qui dit qui a réellement pris le rendez-vous. Le propriétaire du
            # CONTACT faisait apparaître des commerciaux sans aucun RDV posé,
            # simplement parce qu'ils possédaient les fiches.
            # Les dossiers ne sont PAS répartis : créés par n8n, ils n'ont pas
            # de propriétaire, et passer par celui du contact reproduirait
            # exactement la confusion qu'on vient de corriger.
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
                replies=len(delays), meetings=n_meet, deals_ae=n_deal,
                engaged=split["engaged"], split=split,
                # Détail par contact, RETIRÉ avant l'écriture de data.json :
                # ce fichier est servi publiquement par GitHub Pages.
                _ids=dict(both=sorted(dset & mset), meet_only=sorted(mset - dset),
                          deal_only=sorted(dset - mset)),
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
        cohorts.append(dict(id=co["id"], label=co["label"], sent_at=co["sent_at"],
                            status=status, active=n_act, status_note=note,
                            ab_test=co.get("ab_test", True), ab_note=co.get("ab_note"),
                            cells=cells,
                            reply_curve=cumulative_curve(
                                all_delays, sum(c["enrolled"] for c in cells), send)))
    cohorts.sort(key=lambda x: x["id"])

    # Union dédupliquée : les cohortes peuvent se recouper
    dedup = dict(contacts=count_lists(all_lists))
    somme = sum(c["enrolled"] for co in cohorts for c in co["cells"])
    ecart = somme - dedup["contacts"]

    data = dict(
        meta=dict(
            campaign=cfg["campaign"],
            generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            collected=True,
            primary_axis=cfg.get("primary_axis", "cohort"),
            primary_kpi=cfg.get("primary_kpi"),
            source=("HubSpot · listes statiques ∩ EMAIL.hs_sequence_id "
                    "· MEETING_EVENT ∪ DEAL pipeline " + pipeline
                    + f" · contacts uniques, fenêtre J+{ATTRIB_DAYS}"),
            attribution_note=cfg["notes"]["attribution"],
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
        dedup=dedup,
        cohorts=cohorts,
    )
    # Détail nominatif : dans les logs du run, JAMAIS dans data.json.
    # Le fichier est servi publiquement ; les logs supposent un accès au dépôt.
    # IDs de contact uniquement, aucun nom ni e-mail : sans accès au portail,
    # un ID ne désigne personne.
    # Le pop() ci-dessous est ce qui garantit que _ids ne fuite pas dans le
    # JSON — ne pas le déplacer après json.dump.
    print("\n--- détail des contacts engagés ---")
    for co in cohorts:
        for c in co["cells"]:
            ids = c.pop("_ids")
            print(f"\n{co['id']} · {c['audience']}-{c['version']} "
                  f"· {c['split']['engaged']} engagés sur {c['enrolled']} ciblés")
            for cat, label in (("both", "RDV + dossier"),
                               ("meet_only", "RDV seul"),
                               ("deal_only", "dossier seul")):
                if ids[cat]:
                    print(f"  {label} ({len(ids[cat])})")
                    for cid in ids[cat]:
                        print(f"    https://app-eu1.hubspot.com/contacts/"
                              f"{PORTAL}/contact/{cid}")
    print("--- fin du détail ---\n")

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    n_cells = sum(len(c["cells"]) for c in cohorts)
    tot = {k["key"]: sum(c[k["key"]] for co in cohorts for c in co["cells"])
           for k in cfg["kpis"]}
    print(f"OK · {len(cohorts)} cohortes · {n_cells} cellules · "
          f"{dedup['contacts']} contacts ciblés")
    print("   " + " · ".join(f"{k['label']} {tot[k['key']]}" for k in cfg["kpis"]))
    for co in cohorts:
        for c in co["cells"]:
            s = c["split"]
            print(f"   {co['id']} {c['audience']}-{c['version']} "
                  f"n={c['enrolled']} · both {s['both']} · rdv seul {s['meet_only']} "
                  f"· deal seul {s['deal_only']} · engagés {s['engaged']} "
                  f"| deals n8n {s['deal_n8n']} · manuels {s['deal_manual']} "
                  f"· deal seul manuel {s['deal_only_manual']}")
    if ecart > 0:
        print(f"   recoupement : somme des cellules {somme} vs union {dedup['contacts']}")


if __name__ == "__main__":
    build()
