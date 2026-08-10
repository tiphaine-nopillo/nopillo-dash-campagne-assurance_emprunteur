# [OPS] Suivi performance campagne Assurance Emprunteur (Août 2026)

Dashboard statique de suivi de la performance des campagnes d'emails envoyées via les séquences HubSpot. Hébergé sur GitHub Pages, sans backend.

## Architecture

```
index.html       ← la vue (statique, jamais régénérée)
cohorts.json     ← définition des cohortes (audience, version, listes, séquences)
objectives.json  ← les objectifs métier par campagne (édité à la main)
data.json        ← la donnée (seul fichier réécrit à chaque refresh)
refresh.py       ← le collecteur HubSpot → data.json
```

Les campagnes n'ont pas le même objectif. Une seule vue globale serait donc fausse : chaque campagne est jugée sur **sa** métrique, déclarée dans `objectives.json`. Changer une cible = une ligne de JSON, sans toucher au collecteur ni au dashboard.

Métriques disponibles comme objectif primaire : `meetings`, `link_clicks`, `replies`, `completions`.

Une campagne dont l'objectif n'est pas encore instrumenté s'affiche en **carte grise pointillée**, pas à zéro. La distinction est volontaire : « pas mesuré » et « mesuré à zéro » ne déclenchent pas la même action.


## Cohortes

Une **cohorte** = une date d'envoi. Une **cellule** = une liste statique + la séquence qui lui a été envoyée. Les cellules portent l'audience et la version.

```
cohorts.json     ← définition des cohortes (mapping explicite listId + sequenceId)
```

Mapping explicite, pas de regex sur les titres : les 4 listes de la cohorte du 05/08 utilisent 4 ordres de tokens différents, avec « AssEmp » vs « Assurance Emprunteur » et « Batch 5 août » vs « Batch du 5 août ». Un parseur casserait à la cohorte suivante.

Pour les prochaines cohortes, adopte un format fixe — `AE | 2026-09-02 | LMNP | A` — et le mapping devient trivial.

### Séparation des rôles

| Question | Source |
|---|---|
| Qui était ciblé (dénominateur) | Liste **statique** — figée, donc stable dans le temps |
| Ce qui a réellement été envoyé et engagé | Objets `EMAIL` via `hs_sequence_id` |
| RDV | `MEETING_EVENT` associé aux contacts de la liste, après la date d'envoi |
| Transactions | `DEAL` où `pipeline = 3817233652`, associé aux contacts de la liste |
| Répartition | `contact.hubspot_owner_id` — celui de la transaction est vide, n8n les crée sans propriétaire |

`hs_latest_sequence_enrolled` n'est **jamais** utilisé : cette propriété ne conserve que la dernière séquence. Mesuré sur un cas réel du portail : 160 enrôlés réels contre 124 visibles, soit 22 % de perte.

## Garde-fous statistiques

Le dashboard refuse de conclure au-delà de ce que les effectifs permettent.

- **Aucun taux affiché** sous 100 contacts par cellule — seuls les volumes absolus.
- **Aucun gagnant désigné** sans test de proportions bilatéral significatif à 5 %.
- La colonne « seuil détectable » affiche l'écart minimum décelable à 80 % de puissance. Sur l'audience LMNP (n≈48 par bras), ce seuil vaut ±11 à ±18 pt : autrement dit, aucun apprentissage possible.
- La comparaison entre cohortes est étiquetée comme confondue avec le temps et la composition d'audience. Ce n'est pas un test causal.

### Recommandation de design d'expérience

**Arrête de splitter LMNP.** 96 contacts coupés en deux ne produisent rien. Envoie une version unique à toute l'audience LMNP et compare d'une cohorte à l'autre. Le split A/B garde du sens sur RP (~250 par bras), et seulement sur les métriques de haut de funnel.

## Confidentialité

`dealname` contient nom et email du client (« Courtage AE – prenom.nom@domaine.fr NOM »). Les requêtes contacts renvoient noms, emails et téléphones.

**`refresh.py` n'écrit dans `data.json` que des agrégats et des identifiants.** Aucun nom, aucun email, aucun téléphone. C'est le garde-fou qui autorise le repo public — si quelqu'un ajoute un champ nominatif pour améliorer la lisibilité, le repo doit passer en privé le jour même.

## Compter les clics sur un lien précis

L'objet `EMAIL` ne porte aucune propriété d'URL cliquée — `hs_email_click_count` est un total, tous liens confondus. Le comptage par lien passe donc par une **page relais** :

1. Créer `nopillo.com/go/<campagne>` : page portant le script de tracking HubSpot, qui redirige en JS vers la destination.
2. Workflow contact, déclencheur « a visité une page dont l'URL contient `/go/<campagne>` », action : définir une propriété date (ex. `ae_clic_lien_date`).
3. Déclarer cette propriété dans `objectives.json` → `tracked_link.contact_property`.

La page relais est **obligatoire si la destination est un domaine partenaire** : le tracking HubSpot ne tourne pas sur un site tiers, donc sans relais aucun clic n'est enregistré. Elle a un bénéfice secondaire : le dashboard compte des **contacts** et non des clics, donc du dédupliqué et de l'attribuable.

## RDV pris vs RDV honoré

`MEETING_EVENT` porte `hs_outcome_completed_count`, `hs_outcome_no_show_count` et `hs_outcome_canceled_count`. Les deux volumes sont affichés séparément : un no-show compte dans « RDV pris » mais ne génère aucun revenu. Au-delà de 15 % de no-show, le problème est le message de confirmation ou le délai de prise de RDV — pas la séquence.

Choix assumé : **la donnée est découplée du HTML.** Le dashboard Lighthouse régénère son HTML à chaque refresh ; ici `index.html` fetch `data.json`. Conséquence : un refresh = un fichier de 30 ko modifié au lieu de 270 ko, l'historique git reste lisible, et le refresh peut tourner dans une GitHub Action sans jamais toucher au code.

## Mise en route

```bash
# 1. repo
git init && git add . && git commit -m "init Signal"
gh repo create nopillo-signal --public --source=. --push

# 2. GitHub Pages : Settings → Pages → Source = branch main, dossier /(root)
#    → https://<ton-user>.github.io/nopillo-signal/

# 3. données réelles
export HUBSPOT_TOKEN="pat-eu1-..."
pip install requests
python3 refresh.py && git commit -am "refresh" && git push
```

Tant que `data.json` porte `"mode": "DEMO"`, un bandeau jaune l'affiche explicitement. Aucun risque de présenter des chiffres de démo en comité par erreur.

## Private app HubSpot · scopes

| Scope | Pourquoi |
|---|---|
| `sales-email-read` | objets EMAIL = toutes les métriques |
| `crm.objects.owners.read` | résoudre les senders |
| `crm.objects.contacts.read` | rapprochement contact ↔ enrôlement |
| `crm.objects.meetings.read` | suivi des RDV (optionnel) |

Toutes en **lecture seule** : n'accorde aucune écriture. Le script ne fait que lire ; un token en lecture ne peut ni modifier ni supprimer de données CRM.

L'API Sequences n'est pas utilisée — `cohorts.json` déclare les identifiants — donc aucun siège Sales Hub n'est requis, et aucun `HUBSPOT_USER_ID`.

## Refresh automatique

Deux options.

**A · GitHub Actions** (recommandé : pas de machine à laisser allumée)

`.github/workflows/refresh.yml`
```yaml
name: refresh
on:
  schedule: [{cron: "0 6 * * 1-5"}]   # 6h UTC, lundi→vendredi
  workflow_dispatch:
permissions: {contents: write}
jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install requests
      - run: python3 refresh.py
        env:
          HUBSPOT_TOKEN: ${{ secrets.HUBSPOT_TOKEN }}

      - run: |
          git config user.name "signal-bot"
          git config user.email "bot@nopillo.com"
          git commit -am "refresh $(date -u +%F)" || exit 0
          git push
```
Le token vit dans Settings → Secrets → Actions. Il ne passe jamais par le repo.

**B · cron local** — `0 7 * * 1-5 cd ~/signal && python3 refresh.py && git commit -am refresh && git push`

## Limites connues

- **Enrôlés** : l'API ne permet pas de lister les enrôlements d'une séquence (seulement l'état d'un contact donné). `refresh.py` approxime les enrôlés par le volume du 1er step. Pour de l'exact : un workflow HubSpot qui horodate une propriété contact à l'enrôlement, puis lecture de cette propriété.
- **Steps** : les emails d'engagement ne portent pas le numéro de step. Le regroupement se fait par objet d'email. Deux steps au même objet fusionnent — à éviter côté template.
- **CRM Search** plafonne à 10 000 résultats par requête. Au-delà, découper la fenêtre par tranches de dates.
- **Open rate** dégradé par Apple Mail Privacy Protection. Ne pas en faire un indicateur de pilotage.
- **Repo public** : `data.json` ne contient que des agrégats, aucun email ni nom de contact. Vérifier que ça reste vrai à chaque évolution du script — sinon passer le repo en privé (Pages privé requiert GitHub Enterprise) ou basculer sur Cloudflare Pages avec Access.
