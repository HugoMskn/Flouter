# Flouter

Floute automatiquement les visages dans une vidéo. Pensé pour des vidéos de manifestations : on peut faire tourner ça sur un serveur qui se fait saisir sans que ça serve à grand-chose, parce qu'il n'y a jamais rien sur le disque.

## Comment ça marche

1. La vidéo est envoyée en HTTP sur `/api/process`, écrite directement dans un dossier monté en `tmpfs` (donc en RAM, jamais sur un disque physique).
2. Le serveur lance `deface` (détection CenterFace + flou gaussien) en sous-processus sur ce fichier, et renvoie tout de suite un `job_id` sans attendre la fin du traitement.
3. Le front interroge `/api/progress/{job_id}` toutes les 500ms. Le vrai pourcentage vient de la barre `tqdm` que `deface` écrit sur sa sortie d'erreur — le serveur la lit et en extrait juste le nombre, sans jamais logger la ligne complète (qui contiendrait parfois des infos sur le fichier).
4. Une fois fini, `/api/result/{job_id}` stream le fichier flouté directement depuis la RAM vers le navigateur, et le supprime dès que l'envoi est terminé (ou si la connexion est coupée en cours de route).
5. Le fichier original est supprimé juste après que `deface` l'ait traité, qu'il ait réussi ou planté.

Pas de base de données, pas de file d'attente persistante, pas de queue Redis. Juste un dict Python en mémoire pour suivre les jobs en cours, vidé en continu.

## Ce qui prouve qu'il ne reste rien

Les promesses de confidentialité ne valent rien si on ne peut pas les vérifier. Donc :

**Le code est lisible.** Tout tient dans `main.py` (~200 lignes). Cherche les `unlink()` : il y en a un juste après l'appel à `deface` (fichier d'entrée) et un dans le `finally` du générateur qui stream la réponse (fichier de sortie). Il n'y a nulle part un `open(..., "w")` qui écrirait ailleurs que dans `DATA_DIR`.

**Le système de fichiers est verrouillé au niveau Docker, pas juste au niveau applicatif.** Dans `docker-compose.yml`, le conteneur tourne avec `read_only: true` : même si le code applicatif avait un bug et essayait d'écrire sur le disque du conteneur, l'OS refuserait l'écriture. Seul `/app/data` et `/tmp` sont montés, et ce sont des `tmpfs` — de la RAM, pas du disque. Tu peux vérifier toi-même une fois le conteneur lancé :

```
docker compose exec app mount | grep /app/data
# /app/data should show type tmpfs, not ext4/overlay

docker inspect flouter-app-1 --format '{{.HostConfig.ReadonlyRootfs}}'
# true
```

Éteins le conteneur, redémarre la machine hôte : le contenu de `/app/data` a disparu, parce qu'il n'a jamais existé que dans la RAM allouée au conteneur.

**Aucun log n'écrit de nom de fichier ni d'IP.** `logging.disable(logging.CRITICAL)` au démarrage de `main.py`, Uvicorn lancé avec `--no-access-log --log-level critical`, et la sortie du sous-processus `deface` est capturée (pour en extraire le pourcentage) mais jamais écrite où que ce soit — elle est jetée après lecture.

Rien de tout ça n'est une boîte noire : c'est open source, lis le code, ou lance `docker compose up` et inspecte le conteneur toi-même pendant qu'une vidéo est en train d'être traitée.

## Limites, pour être honnête

- Le floutage dépend de la détection de visages de CenterFace : un visage de profil, mal éclairé ou petit dans l'image peut passer au travers. Ne te fie pas aveuglément au résultat, regarde la vidéo avant de la publier.
- Les plaques d'immatriculation, tatouages ou autres signes distinctifs ne sont pas traités.
- Tourne sur CPU par défaut. Une vidéo longue ou en haute résolution peut prendre plusieurs minutes.
- Le serveur garde en RAM un fichier à la fois par requête en cours : plusieurs gros uploads simultanés consomment la RAM allouée au conteneur (`tmpfs` a une taille fixe, voir `docker-compose.yml`).

## Lancer ça

```
docker compose up --build
```

Puis direction `http://localhost`. Variables d'environnement utiles (dans `docker-compose.yml`) :

- `MAX_UPLOAD_MB` : taille max d'upload acceptée (500 par défaut).
- `DEFACE_TIMEOUT_SECONDS` : au-delà, le traitement est tué et le job passe en erreur (900 par défaut).
- `JOB_TTL_SECONDS` : un job jamais récupéré par le client est nettoyé après ce délai (1800 par défaut).

Pense à ajouter du HTTPS en amont de Caddy si tu exposes ça publiquement — tel quel, `Caddyfile` ne fait que du HTTP brut sur le port 80.
