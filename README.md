# Suivi de performance — campagne Assurance Emprunteur

Dashboard statique hébergé sur GitHub Pages, sans backend.

```
index.html      ← la vue (statique, jamais régénérée)
cohorts.json    ← la config métier : batchs, audiences, versions, KPI
data.json       ← la donnée (seul fichier réécrit par le workflow)
refresh.py      ← le collecteur HubSpot
```

## Le correctif majeur de la V2

**Les séquences sont réutilisées d'un batch à l'autre.** `841303267` a servi le RP du 5 août, puis celui du 13 août.

La V1 comptait les e-mails par `hs_sequence_id` sans filtrer les contacts : les envois du 13 août étaient imputés au batch du 5 août, qui se retrouvait surévalué.

La V2 applique un **double filtre** : la séquence restreint le périmètre des e-mails, l'appartenance à la liste statique isole le batch. C'est vérifié par un test hors ligne — sans le second filtre, le 5 août afficherait 810 envois au lieu de 250.

**Conséquence pratique :** ne compare pas les chiffres de la V1 à ceux de la V2. Les premiers étaient contaminés.

## Ajouter un batch

Un bloc dans `cohorts.json`, rien d'autre :

```json
{
 "id": "2026-09-02",
 "label": "Batch du 2 septembre 2026",
 "sent_at": "2026-09-02T09:00:00Z",
 "status": "AUTO",
 "ab_test": false,
 "cells": [
  {"list_id": "14XXX", "sequence_id": "8XXXXXXXX", "audience": "RP", "version": "A",
   "list_name": "..."}
 ]
}
```

Trois règles impératives :

- **`list_id` obligatoire** sur chaque cellule. Sans liste, aucune attribution possible.
- **La liste doit être statique.** Une liste active se recalcule : le dénominateur dériverait.
- **`sent_at` exact.** Il sert à dater les RDV, les simulations et la courbe de réponse. Un décalage fausse tout.

`status: "AUTO"` suffit : le statut est dérivé de `hs_sequences_is_enrolled`, cellule par cellule. Tu n'as jamais à passer un batch en terminé à la main.

## Changer les KPI

Le bloc `kpis` déclare ce qui est mesuré, avec la source et le dénominateur :

| `source` | Origine |
|---|---|
| `email` | objets EMAIL ∩ appartenance à la liste |
| `reply` | `contact.hs_sales_email_last_replied` |
| `meeting` | MEETING_EVENT associé au contact |
| `deal` | DEAL du pipeline déclaré dans `deal_pipeline` |

`primary_kpi` désigne la métrique qui pilote le classement des batchs — ici les simulations.

## Mise en ligne

1. Dépôt GitHub **public** — Pages n'est pas disponible sur dépôt privé en plan gratuit, et le site publié reste public dans tous les cas
2. Envoyer les fichiers, en créant `.github/workflows/refresh.yml` via **Add file → Create new file** pour préserver le chemin
3. `Settings` → `Pages` → branche `main`, dossier `/ (root)`
4. `Settings` → `Secrets and variables` → `Actions` → secret `HUBSPOT_TOKEN`

## Portées de l'application privée

`crm.objects.contacts.read` · `crm.lists.read` · `sales-email-read` · `crm.objects.deals.read`

**Lecture seule, aucune écriture.**

## Limites assumées

- **Attribution temporelle, non causale.** Un contact qui aurait pris rendez-vous de toute façon est compté.
- **Réponses : dernière, pas première.** `hs_sales_email_last_replied` décale la courbe de délai vers le tard.
- **Ouvertures non fiables** depuis Apple Mail Privacy Protection.
- **Étapes regroupées par objet d'e-mail** : les engagements ne portent pas le numéro d'étape.
- **Pas de clic par lien** : `hs_email_click_count` est un total tous liens confondus.
