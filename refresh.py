#!/usr/bin/env python3
"""
Suivi campagne Assurance Emprunteur · refresh.py
Alimente data.json depuis HubSpot.

CORRECTIF CENTRAL DE CETTE VERSION
----------------------------------
Les séquences sont RÉUTILISÉES d'un batch à l'autre : 841303267 a servi le RP
du 5 août puis celui du 13 août. La version précédente comptait les e-mails par
hs_sequence_id, sans filtrer les contacts : les envois du 13 août étaient donc
imputés au batch du 5 août, qui se retrouvait surévalué.

Ici toute métrique est attribuée par APPARTENANCE À LA LISTE de la cellule.
La séquence ne sert plus qu'à restreindre le périmètre des e-mails collectés.

Prérequis
  export HUBSPOT_TOKEN="pat-eu1-..."
  pip install requests

Portées de l'application privée — LECTURE SEULE
  crm.objects.contacts.read     membres des listes
  crm.lists.read                filtre d'appartenance
  sales-email-read              objets EMAIL
  crm.objects.meetings.read     RDV  (couvert par sales-email-read sur ce portail)
  crm.objects.deals.read        simulations
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


# ------------------------------------------------------- RDV et simulations
def _count_assoc(path, contact_ids, extra_filters):
    """Objets associés à ces contacts, pagination incluse."""
    n = 0
    for i in range(0, len(contact_ids), CHUNK):
        chunk = contact_ids[i:i + CHUNK]
        after = None
        while True:
            filters = [{"propertyName": "associations.contact",
                        "operator": "IN", "values": chunk}] + extra_filters
            body = {"filterGroups": [{"filters": filters}],
                    "properties": ["hs_object_id"], "limit": 200}
            if after:
                body["after"] = after
            d = post(path, body)
            n += len(d.get("results", []))
            after = (d.get("paging") or {}).get("next", {}).get("after")
            if not after:
                break
    return n


def split_by_owner(members, counter):
    """Répartition par propriétaire de contact.

    On NE lit PAS le champ `associations` des résultats de recherche : l'API
    CRM Search ne le renvoie pas, ce qui donnait un décompte toujours vide.
    On partitionne les contacts par propriétaire et on compte par partition.
    """
    groups = {}
    for cid, owner, *_ in members:
        groups.setdefault(owner, []).append(cid)
    return {owner: counter(ids) for owner, ids in groups.items()}


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
                       "value": str(since_ms)}]
            mf = cfg.get("meeting_filter")
            if mf:
                meet_f.append({"propertyName": mf["property"],
                               "operator": mf["operator"], "value": mf["value"]})
            deal_f = [{"propertyName": "pipeline", "operator": "EQ", "value": pipeline},
                      {"propertyName": "createdate", "operator": "GTE",
                       "value": str(since_ms)}]
            n_meet = _count_assoc(MEETINGS, ids, meet_f)
            n_deal = _count_assoc(DEALS, ids, deal_f)
            m_own = split_by_owner(members, lambda x: _count_assoc(MEETINGS, x, meet_f))
            d_own = split_by_owner(members, lambda x: _count_assoc(DEALS, x, deal_f))

            active = count_active(c["list_id"], [c["sequence_id"]])
            oids = set(m_own) | set(d_own) | {m[1] for m in members}

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
                steps=steps,
                by_owner=[dict(owner_id=o, owner=owners.get(o, "Non attribué"),
                               meetings=m_own.get(o, 0), deals_ae=d_own.get(o, 0))
                          for o in sorted(oids, key=lambda x: str(x))],
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
                    "· MEETING_EVENT · DEAL pipeline " + pipeline),
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
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    n_cells = sum(len(c["cells"]) for c in cohorts)
    tot = {k["key"]: sum(c[k["key"]] for co in cohorts for c in co["cells"])
           for k in cfg["kpis"]}
    print(f"OK · {len(cohorts)} cohortes · {n_cells} cellules · "
          f"{dedup['contacts']} contacts ciblés")
    print("   " + " · ".join(f"{k['label']} {tot[k['key']]}" for k in cfg["kpis"]))
    if ecart > 0:
        print(f"   recoupement : somme des cellules {somme} vs union {dedup['contacts']}")


if __name__ == "__main__":
    build()
