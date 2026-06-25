# Contributing to BIM-to-BEM

## Setup (once)

```
git clone https://github.com/DaBje/BIM-to-BEM.git
cd BIM-to-BEM
```

## Workflow for every change

### 1. Start from an up-to-date master
```
git checkout master
git pull
```

### 2. Create a branch
```
git checkout -b feature/your-feature-name   # new feature        → minor bump (2.2.x → 2.3.0)
git checkout -b fix/what-you-are-fixing     # bug fix or UI change → patch bump (2.3.0 → 2.3.1)
```

### 3. Make your changes and test in Blender

### 4. Bump the version in `bl_info` and commit
```
git add BIM-to-BEM.py
git commit -m "Short description of what changed and why"
```

### 5. Push the branch
First push on a new branch requires setting the upstream:
```
git push --set-upstream origin your-branch-name
```
After that, plain `git push` works for all subsequent pushes on the same branch.

### 6. Merge to master
```
git checkout master
git merge your-branch-name
git push
```
Or open a Pull Request on github.com/DaBje/BIM-to-BEM if you want a review before merging.

### 7. Tag the release (repo owner only)
Both commands use the same version number — `git tag` creates the tag locally, `git push origin` sends it to GitHub:
```
git tag v2.3.0
git push origin v2.3.0
```

---

## Version numbers (inside `bl_info`)

| Change type | Example | When |
|---|---|---|
| Bug fix | 2.3.0 → 2.3.1 | Correcting wrong values, crashes |
| UI change | 2.3.0 → 2.3.1 | Layout, labels, dropdown width — no new behaviour |
| New feature | 2.3.x → 2.4.0 | New behaviour, new UI element |
| Breaking change | 2.x.x → 3.0.0 | Incompatible with previous version |

## Useful commands

| Command | What it does |
|---|---|
| `git branch` | Show all branches, `*` = current |
| `git status` | Show what has changed since last commit |
| `git log --oneline` | Show commit history |
| `git diff` | Show exact line-by-line changes not yet committed |
| `git checkout master` | Switch back to master |
| `git pull` | Download latest changes from GitHub |
