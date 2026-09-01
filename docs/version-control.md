# Version Control

Arranger uses `main` as the production branch. Railway deploys from `main`, so
all feature work should go through short-lived branches and GitHub pull
requests.

## Branches

Use branch names that describe the work:

```text
feature/saved-work-list
feature/object-storage
feature/musicxml-export
fix/csrf-header
docs/deployment-notes
```

## Pull Request Flow

1. Create a branch from `main`.
2. Make a focused change.
3. Run local checks.
4. Push the branch.
5. Open a pull request.
6. Wait for CI to pass.
7. Merge into `main`.
8. Railway deploys the new `main` commit.

## Local Checks

Run these before pushing:

```powershell
py tests\test_api.py
py tests\test_storage.py
py tests\test_settings.py
py tests\test_security.py
py tests\test_architecture.py
py tests\test_constraints.py
py tests\test_render.py
py tests\test_agent.py
node --check frontend\app.js
```

Use the real Postgres integration test when you have a public database URL:

```powershell
$env:POSTGRES_TEST_DATABASE_URL="postgresql://user:password@host:port/database"
py tests\test_postgres_integration.py
```

## Commits

Keep commit messages direct:

```text
Add Postgres migrations
Improve saved-work frontend workflow
Fix CSRF header handling
Document Railway deployment
```

## Tags

Use semantic version tags for deployed milestones:

```text
v0.1.0  first Railway deployment
v0.2.0  auth, Postgres, migrations, and saved-work UI
v0.3.0  generated MIDI and object storage
```

Create a tag after `main` is stable:

```powershell
git tag v0.2.0
git push origin v0.2.0
```

## Deployment Rule

Do not manually edit production code in Railway. Push code changes to GitHub,
let CI run, then let Railway deploy from `main`.
