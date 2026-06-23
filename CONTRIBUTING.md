# Contributing to BIM-to-BEM

## Setup (once)

```
git clone https://github.com/DaBje/BIM-to-BEM.git
cd BIM-to-BEM
```

## Workflow for every change

### 1. Make sure you're on master and up to date
```
git checkout master
git pull
```

### 2. Create a branch for your change
```
git checkout -b feature/your-feature-name   # new feature        → version minor bump (i.e., 2.1.x → 2.2.0)
git checkout -b fix/what-you-are-fixing     # bug fix or UI change → version patch bump (i.e., 2.1.1 → 2.1.2)
```

### 3. Make your changes, test in Blender

### 4. Commit
```
git add BIM-to-BEM.py
git commit -m "Short description of what changed and why"
```

### 5. First push on a new branch
The first time you push a new branch, Git doesn't know where to send it:
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
Update `bl_info version` in `BIM-to-BEM.py`, commit, then:
```
git tag vX.X.X
git push origin vX.X.X
```
`origin` is the name Git gives your GitHub remote. Tags are not pushed automatically — always push them explicitly.

### 6. After merging - tag the release (repo owner only)
Update `bl_info version` in `BIM-to-BEM`, commit, then:

git tag vX.X.X
git push origin vX.X.X

---

## Version numbers (inside `bl_info`)

| Change type | Example | When |
|---|---|---|
| Bug fix | 2.1.1 → 2.1.2 | Correcting wrong values, crashes |
| UI change | 2.1.1 → 2.1.2 | Layout, labels, dropdown width — no new behaviour |
| New feature | 2.1.x → 2.2.0 | New behaviour, new UI element |
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
