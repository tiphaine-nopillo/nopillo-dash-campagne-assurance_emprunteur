#!/usr/bin/env python3
"""
Suivi campagne CGP · refresh.py
Alimente data.json depuis HubSpot.

Principe
--------
Tout repose sur l'appartenance à des listes. Une cellule de cohorte = une liste
statique. Un KPI documentaire = une liste de documents reçus. Le chiffre cherché
est l'INTERSECTION des deux, et HubSpot sait la calculer côté serveur : deux
filtres sur ilsListIds dans le même filterGroup sont combinés en ET.

On ne récupère donc jamais les enregistrements, seulement le `total` de la
recherche. Aucune donnée personnelle ne transite ni n'est écrite.

Les cohortes se chevauchent : un contact a pu être ciblé dans plusieurs batchs.
Le total campagne est mesuré par UNION dédupliquée (opérateur IN sur toutes les
listes de cohorte), jamais par addition des cellules.

Prérequis
  export HUBSPOT_TOKEN="pat-eu1-..."
  pip install requests

Portées de l'application privée — LECTURE SEULE
  crm.objects.contacts.read
  crm.lists.read
"""
import os
import json
import time
import datetime as dt

import requests

TOKEN = os.environ["HUBSPOT_TOKEN"]
BASE = "https://api.hubapi.com"
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
SEARCH = "/crm/v3/objects/contacts/search"


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


def count_active(list_id):
    """Contacts d'une liste encore activement enrôlés dans une séquence.

    C'est ce compteur qui donne le statut : une cellule dont plus aucun contact
    n'est enrôlé a fini d'envoyer. Le statut est donc DÉRIVÉ de HubSpot, plus
    saisi à la main — la saisie manuelle avait déjà produit une erreur.

    Réserve : hs_sequences_is_enrolled vaut vrai pour n'importe quelle séquence,
    pas seulement celles de la campagne. Un contact réenrôlé ailleurs maintient
    donc artificiellement la cellule en cours. Sur des listes de campagne
    dédiées, le cas reste marginal.
    """
    body = {"filterGroups": [{"filters": [
                {"propertyName": "hs_crm_search.ilsListIds", "operator": "IN",
                 "values": [str(list_id)]},
                {"propertyName": "hs_sequences_is_enrolled", "operator": "EQ",
                 "value": "true"},
            ]}],
            "properties": ["hs_object_id"],
            "limit": 1}
    return post(SEARCH, body).get("total", 0)


def count_lists(list_ids, also_in=None):
    """Nombre de contacts appartenant à l'UNE des list_ids, et le cas échéant
    présents aussi dans `also_in`.

    Deux filtres sur la même propriété dans un seul filterGroup = ET.
    `limit: 1` suffit : seul `total` nous intéresse, et ça évite de faire
    transiter des fiches contact.
    """
    filters = [{"propertyName": "hs_crm_search.ilsListIds",
                "operator": "IN",
                "values": [str(x) for x in list_ids]}]
    if also_in:
        filters.append({"propertyName": "hs_crm_search.ilsListIds",
                        "operator": "IN",
                        "values": [str(also_in)]})
    body = {"filterGroups": [{"filters": filters}],
            "properties": ["hs_object_id"],
            "limit": 1}
    return post(SEARCH, body).get("total", 0)


def reply_stats(list_id, send_iso):
    """Réponses d'une cellule, total et délai après l'envoi du batch.

    Source : contact.hs_sales_email_last_replied, filtré à partir de la date
    d'envoi du batch pour ne pas compter un échange antérieur à la campagne.

    LIMITE ASSUMÉE : cette propriété porte la date de la DERNIÈRE réponse, pas
    de la première. Un contact ayant répondu à J+1 puis à J+6 est compté à J+6.
    La courbe est donc biaisée vers le tard : elle décrit la période d'activité
    de la conversation, pas le délai de première réaction. HubSpot n'expose pas
    d'équivalent « première réponse » sur les e-mails commerciaux.

    On ne demande QUE la date : aucun nom, aucun e-mail ne transite.
    """
    send = dt.datetime.fromisoformat(send_iso.replace("Z", "+00:00"))
    delays, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "hs_crm_search.ilsListIds", "operator": "IN",
                 "values": [str(list_id)]},
                # HubSpot attend des MILLISECONDES epoch pour filtrer une propriété
                # de type date. Passer "2026-07-25" ne matche rien, silencieusement :
                # c'est ce qui affichait 0 réponse partout.
                {"propertyName": "hs_sales_email_last_replied", "operator": "GTE",
                 "value": str(int(send.timestamp() * 1000))},
            ]}],
            "properties": ["hs_sales_email_last_replied"],
            "limit": 100,
        }
        if after:
            body["after"] = after
        d = post(SEARCH, body)
        for r in d.get("results", []):
            v = r["properties"].get("hs_sales_email_last_replied")
            when = to_dt(v)
            if when:
                delays.append(max(0, (when - send).days))
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    return len(delays), delays


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


def cumulative_curve(delays, horizon=21):
    """Courbe cumulée : part des répondants ayant répondu au plus tard à J+n."""
    if not delays:
        return []
    n = len(delays)
    return [dict(day=j, count=sum(1 for x in delays if x <= j),
                 share=round(100 * sum(1 for x in delays if x <= j) / n, 1))
            for j in range(0, horizon + 1)]


def load_config():
    with open("campaign.json", encoding="utf-8") as f:
        return json.load(f)


def build():
    cfg = load_config()
    doc_kpis = [k for k in cfg["kpis"] if k.get("list")]
    all_cohort_lists = [c["list_id"] for co in cfg["cohorts"] for c in co["cells"]]

    cohorts = []
    for co in cfg["cohorts"]:
        cells = []
        for c in co["cells"]:
            cell = dict(list_id=c["list_id"], list_name=c.get("list_name"),
                        audience=c["audience"], version=c.get("version"),
                        enrolled=count_lists([c["list_id"]]),
                        active=count_active(c["list_id"]))
            n_rep, delays = reply_stats(c["list_id"], co["sent_at"])
            cell["replies"] = n_rep
            cell["reply_delays"] = delays
            for k in doc_kpis:
                cell[k["key"]] = count_lists([c["list_id"]], also_in=k["list"])
            cell["docs"] = sum(cell[k["key"]] for k in doc_kpis)
            # Statut au niveau CELLULE : une cohorte peut avoir une cellule finie
            # et une autre encore en envoi, comme le batch du 1er août.
            forced = (co.get("status") or "AUTO").upper()
            cell["status"] = (forced if forced in ("TERMINE", "EN_COURS")
                              else ("EN_COURS" if cell["active"] > 0 else "TERMINE"))
            cells.append(cell)
        n_act = sum(c["active"] for c in cells)
        done = [c for c in cells if c["status"] == "TERMINE"]
        status = ("TERMINE" if n_act == 0 else ("PARTIEL" if done else "EN_COURS"))
        note = None if n_act == 0 else (
            f"{n_act} contact(s) encore en séquence sur cette cohorte"
            + (f", mais la cellule {done[0]['audience']} a fini d'envoyer "
               f"et alimente déjà la référence."
               if done else ". Les chiffres vont encore monter."))
        all_delays = [x for c in cells for x in c["reply_delays"]]
        for c in cells:
            c.pop("reply_delays", None)          # détail inutile côté dashboard
        cohorts.append(dict(id=co["id"], label=co["label"], status=status,
                            active=n_act, status_note=note,
                            sequences=co.get("sequences", []), cells=cells,
                            replies=sum(c["replies"] for c in cells),
                            reply_curve=cumulative_curve(all_delays)))
    cohorts.sort(key=lambda x: x["id"])

    # Union dédupliquée sur l'ensemble des listes de cohorte
    dedup = dict(contacts=count_lists(all_cohort_lists),
                 replies=sum(co["replies"] for co in cohorts))
    for k in doc_kpis:
        dedup[k["key"]] = count_lists(all_cohort_lists, also_in=k["list"])

    # Totaux portefeuille, tous canaux confondus
    portfolio = {k["key"]: count_lists([k["list"]]) for k in doc_kpis}

    docs = sum(dedup[k["key"]] for k in doc_kpis)
    somme_cellules = sum(c["docs"] for co in cohorts for c in co["cells"])
    ecart = somme_cellules - docs

    target = next((k.get("target") for k in cfg["kpis"] if k.get("target")), None)

    data = dict(
        meta=dict(
            campaign=cfg["campaign"],
            generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            collected=True,
            source="HubSpot · appartenance aux listes statiques, intersections calculées côté portail",
            primary_axis=cfg.get("primary_axis", "cohort"),
            status_source=("Statut dérivé automatiquement de contact.hs_sequences_is_enrolled, "
                           "par cellule. Aucune saisie manuelle."),
            attribution_note=cfg["notes"]["attribution"],
            overlap_note=(
                f"Les cohortes se chevauchent : un même contact a pu être ciblé dans "
                f"plusieurs batchs. La somme des cellules dépasse l'union réelle de "
                f"{ecart} documents, soit {100 * ecart / docs:.1f} %. "
                f"Le niveau 1 utilise l'union dédupliquée, jamais la somme."
                if docs and ecart > 0 else
                "Aucun chevauchement détecté entre les cohortes."),
        ),
        kpis=cfg["kpis"],
        dedup=dedup,
        portfolio=portfolio,
        target=target,
        target_initial=cfg.get("target_initial"),
        cohorts=cohorts,
    )
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    n_cells = sum(len(c["cells"]) for c in cohorts)
    print(f"OK · {len(cohorts)} cohortes · {n_cells} cellules · "
          f"{dedup['contacts']} contacts ciblés · {docs} documents"
          + (f" · {100 * docs / target:.0f} % de l'objectif" if target else ""))
    if ecart > 0:
        print(f"   chevauchement : somme des cellules {somme_cellules} vs union {docs}")


if __name__ == "__main__":
    build()
