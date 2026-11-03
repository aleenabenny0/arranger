# Kubernetes

`k8s/` deploys the API with two replicas and a single Postgres behind it. CI
proves the manifests on every push: a `kind` cluster is created, the image is
built and loaded, `kubectl apply -f k8s/` is run, the rollouts are waited on,
and `scripts/smoke.sh` is run through a port-forward (the `kubernetes` job in
`.github/workflows/ci.yml`).

## What is in `k8s/`

| File | What it declares |
|---|---|
| `00-namespace.yaml` | The `arranger` namespace everything else lives in; numbered first so `kubectl apply -f k8s/` creates it before the rest |
| `20-postgres.yaml` | A one-replica Postgres 16 StatefulSet with a 1 GiB PersistentVolumeClaim, readiness and liveness probes, and its Service |
| `10-configmap.yaml` | Non-secret settings: development mode, files and rate limits in Postgres, migrations at startup, one job worker per pod |
| `11-secret.example.yaml` | Placeholder secrets for a throwaway cluster; copy to `secret.yaml` for anything real |
| `30-deployment.yaml` | The API: 2 replicas, resource requests and limits, readiness on `/ready`, liveness on `/health`, `envFrom` the ConfigMap and Secret, an init container that waits for Postgres, non-root |
| `31-service.yaml` | ClusterIP Service on port 80 in front of the pods |

Files are stored as rows (`ARTIFACT_BACKEND=database`) and rate-limit
counters in Postgres (`RATE_LIMIT_BACKEND=database`), so the replicas share
nothing but the database. Each replica runs the migrations at startup under a
Postgres advisory lock, so two pods starting at once cannot race.

## Local cluster with kind

You need Docker, `kind` and `kubectl`.

PowerShell (Windows):

```powershell
winget install Kubernetes.kind Kubernetes.kubectl
kind create cluster --name arranger
docker build -t arranger:local .
kind load docker-image arranger:local --name arranger
kubectl apply -f k8s/
kubectl -n arranger rollout status statefulset/postgres --timeout=120s
kubectl -n arranger rollout status deployment/arranger --timeout=120s
kubectl -n arranger port-forward service/arranger 8000:80
```

Bash (Git Bash, Linux, macOS): the same commands; install kind and kubectl with
your package manager. Then, in a second shell:

```bash
SMOKE_CAPABILITIES=export_pdf,import_audio scripts/smoke.sh http://127.0.0.1:8000
```

Open <http://127.0.0.1:8000> in a browser to use the app. Tear down with
`kind delete cluster --name arranger`.

### Real secrets

`11-secret.example.yaml` is applied with the rest of the directory so that a
fresh cluster works; its values are placeholders. For any cluster that is not
thrown away:

```bash
cp k8s/11-secret.example.yaml k8s/secret.yaml    # git-ignored
# edit k8s/secret.yaml: a real POSTGRES_PASSWORD, the same password inside DATABASE_URL, RESEND_API_KEY, METRICS_TOKEN
kubectl apply -f k8s/ && kubectl apply -f k8s/secret.yaml
kubectl -n arranger rollout restart statefulset/postgres deployment/arranger
```

Change the password before Postgres first starts: it is written into the
volume by `initdb` and a later change to the Secret does not update it.

### Production settings

`10-configmap.yaml` runs the app in development mode so the CI smoke test can
sign in over plain http. For production set `APP_ENV=production`,
`COOKIE_SECURE=true`, `PUBLIC_BASE_URL` and `FRONTEND_ORIGINS` to the https
address, `EMAIL_PROVIDER=resend` with `RESEND_API_KEY` in the Secret, and
`TRUSTED_PROXY_COUNT` to the number of proxies in front of the pods; the app
refuses to start in production until those are right (`docs/deployment.md`,
"Production configuration"). Put an Ingress or a cloud load balancer in front
of the `arranger` Service; TLS terminates there.

## kubectl cheat sheet

Everything below assumes `-n arranger`; `kubectl config set-context --current
--namespace=arranger` saves typing it.

| Task | Command |
|---|---|
| What is running | `kubectl -n arranger get pods -o wide` |
| Everything in the namespace | `kubectl -n arranger get all,pvc,configmap,secret` |
| Why a pod is not ready | `kubectl -n arranger describe pod <pod>` (read Events at the bottom) |
| Application log | `kubectl -n arranger logs deployment/arranger --all-containers --tail=200` |
| Follow one pod | `kubectl -n arranger logs -f <pod>` |
| Log of the previous crash | `kubectl -n arranger logs <pod> --previous` |
| Database log | `kubectl -n arranger logs statefulset/postgres` |
| A shell in a pod | `kubectl -n arranger exec -it <pod> -- sh` |
| A SQL prompt | `kubectl -n arranger exec -it postgres-0 -- psql -U arranger -d arranger` |
| Was the deploy successful | `kubectl -n arranger rollout status deployment/arranger --timeout=120s` |
| Rollout history | `kubectl -n arranger rollout history deployment/arranger` |
| Roll back the last deploy | `kubectl -n arranger rollout undo deployment/arranger` |
| Restart the pods | `kubectl -n arranger rollout restart deployment/arranger` |
| Reach the service locally | `kubectl -n arranger port-forward service/arranger 8000:80` |
| Scale | `kubectl -n arranger scale deployment/arranger --replicas=3` |
| Live resource use | `kubectl -n arranger top pods` (needs metrics-server) |
| Apply a change | `kubectl apply -f k8s/` |
| Remove everything | `kubectl delete namespace arranger` (the PVC and its data go with it) |

Things worth practising on the kind cluster: delete a pod and watch the
Deployment replace it (`kubectl delete pod <pod>`), break the image name in
`30-deployment.yaml` and read `describe` to find `ImagePullBackOff`, then
`rollout undo` to recover.
